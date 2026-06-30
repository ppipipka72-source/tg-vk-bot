"""Перенос текста и вложений между Telegram и VK.

Telegram -> VK: файлы скачиваются у Telegram и заливаются в VK через uploader'ы.
VK -> Telegram: вложения отдаются Telegram прямыми ссылками VK.
Каждое вложение обрабатывается изолированно: ошибка одного не роняет остальные.
"""

import html
import io
import json
import logging
import os
import random

import aiohttp
from aiogram import Bot as TgBot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import BufferedInputFile, LinkPreviewOptions, ReplyParameters
from PIL import Image

from .formatting import format_for_tg, format_for_vk
from .state import LinkStore
from .stt import transcribe_voice
from .video_dl import download_video, find_video_url
from .vk_upload import upload_doc, upload_photo, upload_voice
from .vk_user import download_vk_video, resolve_message_link, upload_vk_video

log = logging.getLogger(__name__)

# Таймаут на скачивание файла у Telegram. У локального Bot API сервера getFile
# сначала целиком тянет файл с серверов Telegram и только потом отвечает — для
# крупных видео (сотни МБ) это занимает минуты, поэтому дефолтных ~60 с мало.
_TG_FILE_TIMEOUT = 600


def _random_id() -> int:
    return random.getrandbits(31)


async def _download_tg(bot: TgBot, file_id: str) -> bytes:
    """Скачать файл Telegram в память.

    Лимит скачивания у облачного Bot API — 20 МБ; с локальным Bot API сервером
    (TG_API_URL) он поднимается до ~2 ГБ, так что крупные видео переносятся.
    getFile и скачивание идут с увеличенным таймаутом (см. _TG_FILE_TIMEOUT).
    """
    file = await bot.get_file(file_id, request_timeout=_TG_FILE_TIMEOUT)
    buf = io.BytesIO()
    try:
        await bot.download_file(file.file_path, destination=buf, timeout=_TG_FILE_TIMEOUT)
    finally:
        _cleanup_local_file(bot, file.file_path)
    return buf.getvalue()


def _cleanup_local_file(bot: TgBot, file_path: str | None) -> None:
    """Удалить файл, скачанный локальным Bot API сервером.

    В режиме --local getFile выкачивает файл на диск сервера и сам его НЕ
    чистит — иначе диск быстро забивается крупными видео. Мы уже прочитали
    файл в память, так что после этого удаляем его с диска. В облачном режиме
    файла на нашем диске нет — пропускаем.
    """
    if not file_path:
        return
    if not getattr(getattr(bot.session, "api", None), "is_local", False):
        return
    try:
        os.remove(file_path)
    except OSError:
        log.debug("Не удалось удалить локальный файл %s", file_path, exc_info=True)


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
                                peer_id, name, message, links: LinkStore,
                                prefix: str = "") -> None:
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
    text = format_for_vk("TG", name, full_body, prefix)

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


async def edit_vk_from_tg(vk_api, peer_id, vk_cmid, name, body, prefix: str = "") -> None:
    """Применить правку TG-сообщения к связанному VK-сообщению (только текст).

    Обратное направление (VK→TG) невозможно: community-токену VK не присылает
    события правок чужих сообщений (только своих), а getHistory ему запрещён.
    """
    text = format_for_vk("TG", name, body or "", prefix)
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
                                links: LinkStore, prefix: str = "") -> int | None:
    """Переносит сообщение VK в TG. Возвращает message_id пересланного голосового
    (audio_message) в TG, если оно было — нужно, чтобы ответить на него расшифровкой."""
    body = message.text or ""
    header = format_for_tg("VK", name, body, prefix)

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

    voice_tg_id = await _send_vk_attachments(
        tg_bot, vk_user_token, chat_id,
        message.attachments, message.peer_id, vk_cmid, links)

    # Пересланные пачки сообщений (fwd_messages) — их текст и вложения лежат
    # вложенно и в attachments не попадают; разворачиваем рекурсивно.
    if message.fwd_messages:
        await _send_vk_fwd_messages(tg_bot, vk_user_token, chat_id,
                                    message.fwd_messages, message.peer_id, vk_cmid, links)

    return voice_tg_id


def find_vk_voice(attachments):
    """Первое голосовое (audio_message) среди вложений VK-сообщения, иначе None."""
    for att in attachments or []:
        am = getattr(att, "audio_message", None)
        if am is not None:
            return am
    return None


async def _send_vk_attachments(tg_bot, vk_user_token, chat_id, attachments,
                               peer_id, vk_cmid, links: LinkStore) -> int | None:
    """Переносит список VK-вложений в TG; ошибка одного не роняет остальные.

    Возвращает message_id отправленного в TG голосового (audio_message), если оно
    было — чтобы ответить на него расшифровкой."""
    voice_tg_id = None
    for att in (attachments or []):
        try:
            sent_att = await _send_one_vk_attachment(
                tg_bot, vk_user_token, chat_id, att, peer_id, vk_cmid, links)
            # Линкуем и медиа: ответить в TG можно хоть на фото/видео.
            if sent_att:
                links.link(chat_id, sent_att.message_id, peer_id, vk_cmid)
                if getattr(att, "audio_message", None) is not None:
                    voice_tg_id = sent_att.message_id
        except Exception:  # noqa: BLE001
            log.exception("VK->TG: не удалось перенести вложение")
            await tg_bot.send_message(chat_id, "⚠️ вложение не удалось перенести")
    return voice_tg_id


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
    """Вызвать метод отправки TG с reply.

    Повтор без reply делаем ТОЛЬКО при TelegramBadRequest — это отказ самого
    Telegram (400), значит сообщение точно не ушло, повтор не создаст дубль.
    На сетевой ошибке (таймаут/обрыв) НЕ перезапускаем отправку: при заливке
    крупного видео (YouTube) канал забит, ответ TG может прийти позже таймаута,
    хотя сообщение уже доставлено — повтор тогда дал бы дубль (у TG нет
    идемпотентности, в отличие от random_id у VK). Возвращаем None: вызывающий
    код трактует это как «не отправилось» и просто не строит reply-связь.
    """
    method = getattr(tg_bot, method_name)
    if reply_to:
        try:
            return await method(chat_id, *args,
                                reply_parameters=ReplyParameters(
                                    message_id=reply_to, allow_sending_without_reply=True),
                                **kwargs)
        except TelegramBadRequest:
            log.warning("TG: reply на %s отвергнут (400), шлю без reply", reply_to)
            # упадём в обычную отправку ниже — первая попытка точно не доставлена
        except TelegramNetworkError:
            log.warning("TG: сетевой сбой при ответе на %s — не дублирую отправку",
                        reply_to)
            return None
    return await method(chat_id, *args, **kwargs)


async def _send_one_vk_attachment(tg_bot, vk_user_token, chat_id, att,
                                  peer_id=None, cmid=None, links=None):
    """Отправляет одно вложение и возвращает отправленное TG-сообщение (для reply-связи).

    Поля берём через getattr: модели вложений сообщения и вложений поста (wall)
    различаются (у поста нет sticker/audio_message/wall и т.п.).
    """
    photo = getattr(att, "photo", None)
    if photo:
        url = _max_size_url(photo.sizes)
        return await tg_bot.send_photo(chat_id, url) if url else None

    sticker = getattr(att, "sticker", None)
    if sticker:
        url = _max_size_url(sticker.images)
        return await tg_bot.send_photo(chat_id, url) if url else None

    video = getattr(att, "video", None)
    if video:
        # Кружок (video_message): сам файл VK по API не отдаёт (приватное видео,
        # вшитое в беседу) — шлём ссылку на сообщение, откроется своей сессией.
        if getattr(video, "type", None) == "video_message":
            return await _send_vk_circle(tg_bot, vk_user_token, chat_id, video, peer_id, cmid)
        return await _send_vk_video(tg_bot, vk_user_token, chat_id, video, peer_id, cmid)

    doc = getattr(att, "doc", None)
    if doc:
        ext = (doc.ext or "").lower()
        if ext == "gif" and doc.url:
            return await tg_bot.send_animation(chat_id, doc.url)
        if doc.url:
            return await tg_bot.send_document(chat_id, doc.url)
        return None

    audio_message = getattr(att, "audio_message", None)
    if audio_message:
        url = audio_message.link_ogg or audio_message.link_mp3
        return await tg_bot.send_voice(chat_id, url) if url else None

    graffiti = getattr(att, "graffiti", None)
    if graffiti:
        return await tg_bot.send_photo(chat_id, graffiti.url) if graffiti.url else None

    audio = getattr(att, "audio", None)
    if audio:
        return await tg_bot.send_message(
            chat_id, f"🎵 {audio.artist or ''} — {audio.title or ''}".strip())

    wall = getattr(att, "wall", None)
    if wall:
        return await _send_vk_wall(tg_bot, vk_user_token, chat_id, wall,
                                   peer_id, cmid, links)

    link = getattr(att, "link", None)
    if link:
        return await tg_bot.send_message(chat_id, link.url)

    return await tg_bot.send_message(chat_id, "📎 [вложение не поддерживается]")


async def _send_vk_wall(tg_bot, vk_user_token, chat_id, w, peer_id=None, cmid=None,
                        links=None, depth=0):
    """Разворачивает пересланный пост VK (wall) в TG: заголовок + текст + вложения
    + цепочку репостов (copy_history). Раньше слалась только ссылка на пост."""
    owner_id = getattr(w, "owner_id", None) or getattr(w, "from_id", None)
    post_id = getattr(w, "id", None)
    link = f"https://vk.com/wall{owner_id}_{post_id}" if owner_id and post_id else None

    marker = "📌" + "▸" * depth
    header = f"<i>{marker} пост:</i>"
    if link:
        header += f' <a href="{link}">vk.com</a>'

    text = (getattr(w, "text", None) or "").strip()
    if text:
        header += f"\n{html.escape(text)}"

    sent = await _tg_send(tg_bot, "send_message", chat_id, None,
                          header, parse_mode=ParseMode.HTML,
                          link_preview_options=LinkPreviewOptions(is_disabled=True))
    if sent and links is not None:
        links.link(chat_id, sent.message_id, peer_id, cmid)

    # Вложения самого поста.
    await _send_vk_attachments(tg_bot, vk_user_token, chat_id,
                               getattr(w, "attachments", None), peer_id, cmid, links)

    # copy_history — если этот пост сам является репостом, разворачиваем источник.
    for src in (getattr(w, "copy_history", None) or []):
        await _send_vk_wall(tg_bot, vk_user_token, chat_id, src,
                            peer_id, cmid, links, depth + 1)

    return sent


async def _send_vk_circle(tg_bot, vk_user_token, chat_id, v, peer_id=None, cmid=None):
    """Кружок (VK video_message). Файл недоступен через API — шлём ссылку на
    сообщение в беседе (vk.com/im), чтобы открыть его своей VK-сессией."""
    # Автор: VK-title кружка обычно "Видеосообщение от @user".
    title = v.title or ""
    author = title.split("от ", 1)[1].strip() if "от " in title else ""
    text = f"🔴 Кружок от {author}".rstrip() if author else "🔴 Кружок"

    link = None
    if vk_user_token and cmid:
        try:
            link = await resolve_message_link(vk_user_token, peer_id, cmid, v.owner_id, v.id)
        except Exception:  # noqa: BLE001
            log.exception("VK->TG: не смог построить ссылку на кружок")
    if link:
        text += f"\n{link}"
    return await tg_bot.send_message(chat_id, text)


async def _send_vk_video(tg_bot, vk_user_token, chat_id, v, peer_id=None, cmid=None):
    title = v.title or "видео"

    # 1) С user-токеном тянем mp4 через video.get и шлём настоящим видео.
    if vk_user_token:
        data = await download_vk_video(
            vk_user_token, v.owner_id, v.id, getattr(v, "access_key", None),
            peer_id=peer_id, cmid=cmid)
        if data:
            # Без caption: у видео, залитых прямо в беседу VK, "title" — это
            # авто-мусор (случайные символы / "видео недоступно"). Реальный текст
            # пользователя и так уходит отдельным header-сообщением, так что
            # подпись тут только засоряет сообщение.
            return await tg_bot.send_video(
                chat_id, BufferedInputFile(data, filename="video.mp4"))

    # Фолбэк без mp4. Превью VK через send_photo не шлём: для приватных/внешних
    # это либо заглушка-замок ("Доступ ограничен"), либо Telegram вообще не может
    # скачать URL — в обоих случаях получается мусор. Шлём чистой строкой.
    link = f"https://vk.com/video{v.owner_id}_{v.id}"
    access_key = getattr(v, "access_key", None)
    if access_key:
        link += f"?access_key={access_key}"

    # 2) Автор ограничил доступ — mp4 недоступен нашему аккаунту (но не другим).
    if getattr(v, "content_restricted", None) or getattr(v, "is_private", None):
        note = getattr(v, "content_restricted_message", None) or "автор ограничил доступ к видео"
        return await tg_bot.send_message(chat_id, f"🎬🔒 {note}\n{link}")

    # 3) Внешнее/непереносимое видео — отдаём рабочей ссылкой.
    return await tg_bot.send_message(chat_id, f"🎬 {title}\n{link}")


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


# --------------------------------------------------------------------------- #
#  Расшифровка голосовых -> ответом в оба чата
# --------------------------------------------------------------------------- #

async def _download_url(url: str) -> bytes | None:
    """Скачать небольшой файл (голосовое) по прямой ссылке в память."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()


async def relay_voice_transcript(tg_bot, vk_api, *, tg_chat_id, tg_reply_to,
                                 vk_peer_id, vk_reply_cmid,
                                 ogg_url=None, tg_file_id=None) -> None:
    """Распознать речь в голосовом и отправить расшифровку ответом в оба чата.

    Источник аудио: либо прямая ссылка VK (ogg_url), либо file_id Telegram
    (tg_file_id). tg_reply_to / vk_reply_cmid — на какие сообщения отвечать в TG
    и VK (любой может быть None — туда не отвечаем). Фоновая задача: ошибки гасим,
    мост не роняем.
    """
    try:
        if tg_file_id:
            data = await _download_tg(tg_bot, tg_file_id)
        elif ogg_url:
            data = await _download_url(ogg_url)
        else:
            return

        text = await transcribe_voice(data)
        if not text:
            return

        label = f"📝 {text}"
        if tg_reply_to:
            try:
                await _tg_send(tg_bot, "send_message", tg_chat_id, tg_reply_to, label)
            except Exception:  # noqa: BLE001
                log.exception("STT: не удалось отправить расшифровку в TG")
        if vk_api is not None and vk_reply_cmid:
            forward = json.dumps({"peer_id": vk_peer_id,
                                  "conversation_message_ids": [vk_reply_cmid],
                                  "is_reply": True})
            # Один random_id на обе попытки: если первая дошла, но ответ
            # затерялся, VK по тому же random_id не отправит дубль.
            rid = _random_id()
            try:
                await vk_api.messages.send(peer_id=vk_peer_id, message=label,
                                           random_id=rid, forward=forward)
            except Exception:  # noqa: BLE001 — reply мог не пройти -> без него
                log.exception("STT: VK reply (cmid=%s) не прошёл, шлю без reply",
                              vk_reply_cmid)
                await vk_api.messages.send(peer_id=vk_peer_id, message=label,
                                           random_id=rid)
    except Exception:  # noqa: BLE001
        log.exception("STT: ошибка расшифровки голосового")
