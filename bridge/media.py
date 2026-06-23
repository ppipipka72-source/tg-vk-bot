"""Перенос текста и вложений между Telegram и VK.

Telegram -> VK: файлы скачиваются у Telegram и заливаются в VK через uploader'ы.
VK -> Telegram: вложения отдаются Telegram прямыми ссылками VK.
Каждое вложение обрабатывается изолированно: ошибка одного не роняет остальные.
"""

import html
import io
import json
import logging
import random

from aiogram import Bot as TgBot
from aiogram.enums import ParseMode
from aiogram.types import BufferedInputFile, ReplyParameters
from PIL import Image

from .formatting import format_for_tg, format_for_vk
from .state import LinkStore
from .video_dl import download_video, find_video_url
from .vk_upload import upload_doc, upload_photo, upload_voice
from .vk_user import download_vk_video, upload_vk_video

log = logging.getLogger(__name__)


def _random_id() -> int:
    return random.getrandbits(31)


async def _download_tg(bot: TgBot, file_id: str) -> bytes:
    """Скачать файл Telegram в память. Лимит Bot API на скачивание — 20 МБ."""
    buf = io.BytesIO()
    await bot.download(file_id, destination=buf)
    return buf.getvalue()


def _to_png(data: bytes) -> bytes:
    img = Image.open(io.BytesIO(data)).convert("RGBA")
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _truncate(text: str, limit: int = 80) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------- #
#  Telegram  ->  VK
# --------------------------------------------------------------------------- #

async def send_tg_message_to_vk(tg_bot, vk_api, vk_token, vk_user_token, vk_group_id,
                                peer_id, name, message, links: LinkStore) -> None:
    body = message.text or message.caption or ""
    attachments: list[str] = []
    notes: list[str] = []

    async def add_doc(file_id, fname):
        data = await _download_tg(tg_bot, file_id)
        attachments.append(await upload_doc(vk_token, peer_id, fname, data))

    async def add_video(file_id, fname):
        # С user-токеном — нативное видео; иначе документом.
        data = await _download_tg(tg_bot, file_id)
        if vk_user_token and vk_group_id:
            try:
                attachments.append(await upload_vk_video(vk_user_token, vk_group_id, fname, data))
                return
            except Exception:  # noqa: BLE001 — при сбое откатываемся на документ
                log.exception("TG->VK: нативная заливка видео не удалась, шлю документом")
        attachments.append(await upload_doc(vk_token, peer_id, fname, data))

    try:
        if message.photo:
            data = await _download_tg(tg_bot, message.photo[-1].file_id)
            attachments.append(await upload_photo(vk_token, peer_id, data))

        if message.sticker:
            await _tg_sticker_to_vk(tg_bot, vk_token, peer_id, message.sticker,
                                    attachments, notes)

        if message.animation:  # GIF (в Telegram это mp4) — всегда документом
            await add_doc(message.animation.file_id,
                          message.animation.file_name or "animation.mp4")
        elif message.video:
            await add_video(message.video.file_id, message.video.file_name or "video.mp4")

        if message.document:
            await add_doc(message.document.file_id, message.document.file_name or "file")

        if message.audio:
            await add_doc(message.audio.file_id, message.audio.file_name or "audio.mp3")

        if message.video_note:  # кружок — нативным видео (в VK нет круглого формата)
            await add_video(message.video_note.file_id, "video_note.mp4")

        if message.voice:
            data = await _download_tg(tg_bot, message.voice.file_id)
            attachments.append(await upload_voice(vk_token, peer_id, data))

        if message.location:
            loc = message.location
            notes.append(f"[гео] https://maps.google.com/?q={loc.latitude},{loc.longitude}")

        if message.contact:
            c = message.contact
            notes.append(f"[контакт] {c.first_name or ''} {c.phone_number or ''}".strip())

    except Exception:  # noqa: BLE001 — вложение не должно ронять мост
        log.exception("TG->VK: не удалось перенести вложение")
        notes.append("[вложение не удалось перенести]")

    full_body = "\n".join(p for p in [body, *notes] if p)
    text = format_for_vk("TG", name, full_body)

    # Нативный ответ через cmid (forward+is_reply), либо цитата-фолбэк.
    reply_cmid = None
    if message.reply_to_message:
        reply_cmid = links.vk_for_tg(message.chat.id, message.reply_to_message.message_id)
        log.debug("TG->VK reply: tg_id=%s -> vk_cmid=%s",
                  message.reply_to_message.message_id, reply_cmid)
        if reply_cmid is None:
            text = _quote_tg(message.reply_to_message) + "\n" + text

    # Отправляем через peer_ids (множественная форма): только так VK возвращает
    # conversation_message_id нашего сообщения — он нужен, чтобы ответ в VK на
    # сообщение бота прилетал в TG нативным reply.
    params = dict(peer_ids=[peer_id], message=text, random_id=_random_id())
    if attachments:
        params["attachment"] = ",".join(attachments)

    sent_cmid = None
    if reply_cmid:
        forward = json.dumps({"peer_id": peer_id,
                              "conversation_message_ids": [reply_cmid], "is_reply": True})
        try:
            sent_cmid = _cmid_from_send(await vk_api.messages.send(forward=forward, **params))
        except Exception:  # noqa: BLE001 — исходное могло быть удалено -> цитата
            log.exception("TG->VK: нативный reply (cmid=%s) не прошёл, шлю с цитатой",
                          reply_cmid)
            params["message"] = _quote_tg(message.reply_to_message) + "\n" + params["message"]
            sent_cmid = _cmid_from_send(await vk_api.messages.send(**params))
    else:
        sent_cmid = _cmid_from_send(await vk_api.messages.send(**params))

    log.debug("TG->VK: отправлено, наш cmid=%s (tg_id=%s)", sent_cmid, message.message_id)
    links.link(message.chat.id, message.message_id, peer_id, sent_cmid)


def _cmid_from_send(resp):
    """conversation_message_id из ответа messages.send(peer_ids=[...]).

    При одиночном peer_id VK отдаёт лишь message_id; форма peer_ids -> список
    объектов с conversation_message_id. Берём первый (получатель один).
    """
    try:
        if isinstance(resp, list) and resp:
            return getattr(resp[0], "conversation_message_id", None)
    except Exception:  # noqa: BLE001
        pass
    return None


async def edit_vk_from_tg(vk_api, peer_id, vk_cmid, name, body) -> None:
    """Применить правку TG-сообщения к связанному VK-сообщению (только текст).

    Обратное направление (VK→TG) невозможно: community-токену VK не присылает
    события правок чужих сообщений (только своих), а getHistory ему запрещён.
    """
    text = format_for_vk("TG", name, body or "")
    try:
        await vk_api.messages.edit(peer_id=peer_id, cmid=vk_cmid,
                                   message=text, keep_forward_messages=1)
    except Exception:  # noqa: BLE001 — лимит VK 24ч / сообщение удалено
        log.warning("TG->VK: правку не удалось применить (cmid=%s)", vk_cmid)


async def _tg_sticker_to_vk(tg_bot, vk_token, peer_id, sticker, attachments, notes) -> None:
    emoji = sticker.emoji or "🆒"
    # У анимированных/видео-стикеров берём статичную миниатюру.
    src = sticker.file_id
    if sticker.is_animated or sticker.is_video:
        if sticker.thumbnail:
            src = sticker.thumbnail.file_id
        else:
            notes.append(f"[стикер] {emoji}")
            return
    try:
        data = await _download_tg(tg_bot, src)
        attachments.append(await upload_photo(vk_token, peer_id, _to_png(data)))
    except Exception:  # noqa: BLE001
        log.exception("TG->VK: стикер не сконвертировался")
        notes.append(f"[стикер] {emoji}")


def _quote_tg(replied) -> str:
    author = replied.from_user.full_name if replied.from_user else "?"
    body = replied.text or replied.caption or "[вложение]"
    return f"┌ {author}: {_truncate(body)}"


# --------------------------------------------------------------------------- #
#  VK  ->  Telegram
# --------------------------------------------------------------------------- #

def _max_size_url(sizes) -> str | None:
    if not sizes:
        return None
    best = max(sizes, key=lambda s: (getattr(s, "width", 0) or 0))
    return best.url


async def send_vk_message_to_tg(tg_bot, vk_user_token, chat_id, name, message,
                                links: LinkStore) -> None:
    body = message.text or ""
    header = format_for_tg("VK", name, body)

    # Ключ связи — conversation_message_id (стабилен для сообществ, в отличие от id).
    vk_cmid = message.conversation_message_id or message.id

    reply_to_tg = None
    if message.reply_message:
        reply_cmid = (getattr(message.reply_message, "conversation_message_id", None)
                      or message.reply_message.id)
        reply_to_tg = links.tg_for_vk(message.peer_id, reply_cmid)
        log.debug("VK->TG reply: vk_cmid=%s -> tg_id=%s", reply_cmid, reply_to_tg)
        if reply_to_tg is None:
            quote = _truncate(message.reply_message.text or "[вложение]")
            header = f"<i>┌ {quote}</i>\n{header}"

    sent = await _tg_send(tg_bot, "send_message", chat_id, reply_to_tg,
                          header, parse_mode=ParseMode.HTML)
    if sent:
        links.link(chat_id, sent.message_id, message.peer_id, vk_cmid)

    await _send_vk_attachments(tg_bot, vk_user_token, chat_id,
                               message.attachments, message.peer_id, vk_cmid, links)

    # Пересланные пачки сообщений (fwd_messages) — их текст и вложения лежат
    # вложенно и в attachments не попадают; разворачиваем рекурсивно.
    if message.fwd_messages:
        await _send_vk_fwd_messages(tg_bot, vk_user_token, chat_id,
                                    message.fwd_messages, message.peer_id, vk_cmid, links)


async def _send_vk_attachments(tg_bot, vk_user_token, chat_id, attachments,
                               peer_id, vk_cmid, links: LinkStore) -> None:
    """Переносит список VK-вложений в TG; ошибка одного не роняет остальные."""
    for att in (attachments or []):
        try:
            sent_att = await _send_one_vk_attachment(tg_bot, vk_user_token, chat_id, att)
            # Линкуем и медиа: ответить в TG можно хоть на фото/видео.
            if sent_att:
                links.link(chat_id, sent_att.message_id, peer_id, vk_cmid)
        except Exception:  # noqa: BLE001
            log.exception("VK->TG: не удалось перенести вложение")
            await tg_bot.send_message(chat_id, "⚠️ вложение не удалось перенести")


async def _send_vk_fwd_messages(tg_bot, vk_user_token, chat_id, fwd_messages,
                                peer_id, vk_cmid, links: LinkStore, depth: int = 1) -> None:
    """Рекурсивно переносит пересланные сообщения VK (fwd_messages) в TG.

    Каждое пересланное — отдельным TG-сообщением с маркером пересылки; его
    вложения и вложенные пересылки разворачиваются следом.
    """
    marker = "↪️" + "▸" * (depth - 1)
    for fwd in fwd_messages or []:
        text = (getattr(fwd, "text", None) or "").strip()
        block = f"<i>{marker} переслано:</i>"
        if text:
            block += f"\n{html.escape(text)}"
        sent = await _tg_send(tg_bot, "send_message", chat_id, None,
                              block, parse_mode=ParseMode.HTML)
        if sent:
            links.link(chat_id, sent.message_id, peer_id, vk_cmid)

        await _send_vk_attachments(tg_bot, vk_user_token, chat_id,
                                   getattr(fwd, "attachments", None),
                                   peer_id, vk_cmid, links)

        nested = getattr(fwd, "fwd_messages", None)
        if nested:
            await _send_vk_fwd_messages(tg_bot, vk_user_token, chat_id, nested,
                                        peer_id, vk_cmid, links, depth + 1)


async def _tg_send(tg_bot, method_name, chat_id, reply_to, *args, **kwargs):
    """Вызвать метод отправки TG с reply; при ошибке reply — без него."""
    method = getattr(tg_bot, method_name)
    if reply_to:
        try:
            return await method(chat_id, *args,
                                reply_parameters=ReplyParameters(
                                    message_id=reply_to, allow_sending_without_reply=True),
                                **kwargs)
        except Exception:  # noqa: BLE001
            log.warning("TG: не удалось ответить на %s, шлю без reply", reply_to)
    return await method(chat_id, *args, **kwargs)


async def _send_one_vk_attachment(tg_bot, vk_user_token, chat_id, att):
    """Отправляет одно вложение и возвращает отправленное TG-сообщение (для reply-связи)."""
    if att.photo:
        url = _max_size_url(att.photo.sizes)
        return await tg_bot.send_photo(chat_id, url) if url else None

    if att.sticker:
        url = _max_size_url(att.sticker.images)
        return await tg_bot.send_photo(chat_id, url) if url else None

    if att.video:
        return await _send_vk_video(tg_bot, vk_user_token, chat_id, att.video)

    if att.doc:
        ext = (att.doc.ext or "").lower()
        if ext == "gif" and att.doc.url:
            return await tg_bot.send_animation(chat_id, att.doc.url)
        if att.doc.url:
            return await tg_bot.send_document(chat_id, att.doc.url)
        return None

    if att.audio_message:
        url = att.audio_message.link_ogg or att.audio_message.link_mp3
        return await tg_bot.send_voice(chat_id, url) if url else None

    if att.graffiti:
        return await tg_bot.send_photo(chat_id, att.graffiti.url) if att.graffiti.url else None

    if att.audio:
        a = att.audio
        return await tg_bot.send_message(chat_id, f"🎵 {a.artist or ''} — {a.title or ''}".strip())

    if att.wall:
        w = att.wall
        return await tg_bot.send_message(
            chat_id, f"📌 пост: https://vk.com/wall{w.from_id}_{w.id}")

    if att.link:
        return await tg_bot.send_message(chat_id, att.link.url)

    return await tg_bot.send_message(chat_id, "📎 [вложение не поддерживается]")


async def _send_vk_video(tg_bot, vk_user_token, chat_id, v):
    title = v.title or "видео"

    # 1) С user-токеном тянем mp4 через video.get и шлём настоящим видео.
    if vk_user_token:
        data = await download_vk_video(
            vk_user_token, v.owner_id, v.id, getattr(v, "access_key", None))
        if data:
            return await tg_bot.send_video(
                chat_id, BufferedInputFile(data, filename="video.mp4"), caption=f"🎬 {title}")

    # 2) Фолбэк: превью-кадр + рабочая ссылка (с access_key для приватных).
    link = f"https://vk.com/video{v.owner_id}_{v.id}"
    access_key = getattr(v, "access_key", None)
    if access_key:
        link += f"?access_key={access_key}"
    caption = f"🎬 {title}\n{link}"

    # Превью грузим через send_photo по URL VK. Telegram качает URL сам и для
    # внешних/недоступных видео не может его достать ("failed to get HTTP URL
    # content") — тогда откатываемся на текст со ссылкой, чтобы не терять видео.
    thumb = _max_size_url(getattr(v, "image", None))
    if thumb:
        try:
            return await tg_bot.send_photo(chat_id, thumb, caption=caption)
        except Exception:  # noqa: BLE001
            log.warning("VK->TG: не удалось отправить превью видео, шлю ссылкой")
    return await tg_bot.send_message(chat_id, caption)


# --------------------------------------------------------------------------- #
#  Видео по ссылке (TikTok / Shorts / Reels) -> ответом в оба чата
# --------------------------------------------------------------------------- #

async def relay_link_video(tg_bot, vk_api, tg_chat_id, vk_peer_id,
                           vk_token, vk_user_token, vk_group_id,
                           text, tg_reply_to, vk_reply_cmid) -> None:
    """Скачать видео по ссылке из text и отправить ответом в TG и в VK.

    tg_reply_to — id сообщения-ссылки в TG; vk_reply_cmid — cmid того же
    сообщения в VK. Любой из них может быть None (тогда туда не шлём).
    Запускается фоновой задачей: ошибки гасим, мост не роняем.
    """
    url = find_video_url(text)
    if not url:
        return
    log.info("link-video: качаю %s", url)
    res = await download_video(url)
    if not res:
        return
    data, filename, title = res
    caption = _truncate(title, 180)

    if tg_reply_to:
        try:
            await _tg_send(tg_bot, "send_video", tg_chat_id, tg_reply_to,
                           BufferedInputFile(data, filename=filename),
                           caption=caption or None)
        except Exception:  # noqa: BLE001
            log.exception("link-video: не удалось отправить видео в TG")

    if vk_reply_cmid:
        try:
            await _vk_send_video_reply(vk_api, vk_token, vk_user_token, vk_group_id,
                                       vk_peer_id, vk_reply_cmid, data, filename, caption)
        except Exception:  # noqa: BLE001
            log.exception("link-video: не удалось отправить видео в VK")


async def _vk_send_video_reply(vk_api, vk_token, vk_user_token, vk_group_id,
                               peer_id, reply_cmid, data, filename, caption) -> None:
    # С user-токеном — нативное видео, иначе документом.
    attachment = None
    if vk_user_token and vk_group_id:
        try:
            attachment = await upload_vk_video(vk_user_token, vk_group_id, filename, data)
        except Exception:  # noqa: BLE001
            log.exception("link-video VK: нативная заливка не удалась, шлю документом")
    if not attachment:
        attachment = await upload_doc(vk_token, peer_id, filename, data)

    params = dict(peer_id=peer_id, message=caption or "",
                  random_id=_random_id(), attachment=attachment)
    forward = json.dumps({"peer_id": peer_id,
                          "conversation_message_ids": [reply_cmid], "is_reply": True})
    try:
        await vk_api.messages.send(forward=forward, **params)
    except Exception:  # noqa: BLE001 — reply мог не пройти -> шлём без него
        log.exception("link-video VK: reply (cmid=%s) не прошёл, шлю без reply", reply_cmid)
        await vk_api.messages.send(**params)
