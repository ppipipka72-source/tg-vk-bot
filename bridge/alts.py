"""Сохранённые видео («альты») — кросс-платформенная выдача TG ↔ VK.

Альт хранит хэндл видео на той платформе, где сохранён, а для второй платформы
достраивается лениво при первом запросе и кешируется:
  • TG-origin: сразу есть tg_file_id; VK-вложение льётся при первом запросе в VK.
  • VK-origin: видео скачивается и перезаливается в наше сообщество (мы им
    владеем → стабильный хэндл); TG file_id берётся при первом запросе в TG.

Так «сохранил в TG → запросил в VK» (и наоборот) работает гладко и без повторной
перекладки байтов после первого раза.
"""

import asyncio
import logging
import random
from datetime import datetime

from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from vkbottle import Callback, Keyboard

from .formatting import format_for_tg, format_for_vk
from .media import _download_tg
from .state import LinkStore
from .vk_upload import upload_doc
from .vk_user import download_vk_video, upload_vk_video

log = logging.getLogger(__name__)

ALT_HELP = (
    "Альты — сохранённые видео:\n"
    "• ответь на видео «/alt название» — сохранить;\n"
    "• «/alt название» (без ответа) — прислать это видео;\n"
    "• «/alt list» — кнопки со всеми видео;\n"
    "• «/alt search название» — найти; «/alt delete название» — удалить.\n"
    "Альты общие для Telegram и VK: всё видно на обеих платформах."
)


def _random_id() -> int:
    return random.getrandbits(31)


class AltService:
    """Логика сохранения/выдачи альтов, общая для TG- и VK-стороны."""

    def __init__(self, cfg, links: LinkStore):
        self.cfg = cfg
        self.links = links
        self.tg_bot = None       # aiogram Bot — выставляется в main
        self.vk_api = None       # community-API VK — выставляется в main
        self.vk_group_id = None  # id сообщества — выставляется в main

    # --- зеркало команд и ответов между платформами ------------------------
    # Команды /alt перехватываются и мостом не пересылаются, а ответы шлёт сам
    # бот (свои сообщения он не получает обратно). Поэтому, чтобы оба чата были
    # «зеркалом», вручную дублируем и введённую команду, и ответ бота в обе
    # стороны. Текст ответов — plain, одинаковый на обеих платформах.

    @staticmethod
    def tg_keyboard(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
        rows = [[InlineKeyboardButton(text=(n or "?")[:60], callback_data=f"alt:{i}")]
                for i, n in items[:100]]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def vk_keyboard(items: list[tuple[int, str]]) -> str:
        kb = Keyboard(inline=False, one_time=True)
        for idx, (i, n) in enumerate(items[:20]):
            if idx and idx % 2 == 0:
                kb.row()
            kb.add(Callback((n or "?")[:40], payload={"alt": i}))
        return kb.get_json()

    async def _tg_say(self, chat_id, text, kb=None, html=False) -> None:
        if not (chat_id and text is not None):
            return
        try:
            await self.tg_bot.send_message(
                chat_id, text, reply_markup=kb,
                parse_mode=("HTML" if html else None))
        except Exception:  # noqa: BLE001
            log.exception("alt: не удалось отправить текст в TG")

    async def _vk_say(self, peer_id, text, kb=None) -> None:
        if not (peer_id and text is not None):
            return
        params = dict(peer_id=peer_id, message=text, random_id=_random_id())
        if kb:
            params["keyboard"] = kb
        try:
            await self.vk_api.messages.send(**params)
        except Exception:  # noqa: BLE001
            log.exception("alt: не удалось отправить текст в VK")

    async def echo_command(self, origin: str, tg_chat_id, author: str, text: str) -> None:
        """Показать введённую команду на ПРОТИВОПОЛОЖНОЙ платформе."""
        vk_peer = self.links.vk_peer_for_tg_chat(tg_chat_id) if tg_chat_id else None
        if origin == "tg":
            await self._vk_say(vk_peer, format_for_vk("TG", author, text))
        else:
            await self._tg_say(tg_chat_id, format_for_tg("VK", author, text), html=True)

    async def broadcast_text(self, tg_chat_id, text, items=None) -> None:
        """Ответ бота — в оба чата. items -> меню (своя клавиатура на каждой)."""
        vk_peer = self.links.vk_peer_for_tg_chat(tg_chat_id) if tg_chat_id else None
        await self._tg_say(tg_chat_id, text, self.tg_keyboard(items) if items else None)
        await self._vk_say(vk_peer, text, self.vk_keyboard(items) if items else None)

    @staticmethod
    def save_message(ok: bool, err: str | None, name: str) -> str:
        if ok:
            return f"✅ Сохранил альт «{name}». «/alt list» — все."
        reason = {
            "no_media": "в том сообщении нет видео",
            "dup": f"альт «{name}» уже есть",
            "no_user_token": "сохранение видео из VK недоступно (нет user-токена)",
            "download_failed": "не удалось скачать это видео из VK",
            "upload_failed": "не удалось сохранить видео",
        }.get(err, "не удалось сохранить")
        return f"⚠️ {reason}."

    # --- извлечение видео из сообщений --------------------------------------

    @staticmethod
    def tg_media(message):
        """(kind, file_id) для видео-подобного вложения TG, иначе None."""
        if message.video:
            return "video", message.video.file_id
        if message.animation:
            return "animation", message.animation.file_id
        if message.video_note:
            return "video_note", message.video_note.file_id
        if message.document:
            return "document", message.document.file_id
        return None

    @staticmethod
    def vk_message_video(msg):
        """Объект видео из VK-сообщения (не кружок), иначе None."""
        if msg is None:
            return None
        for att in (msg.attachments or []):
            v = getattr(att, "video", None)
            if v and getattr(v, "type", None) != "video_message":
                return v
        return None

    @staticmethod
    def _parse_vk_attachment(att: str):
        """'video-123_456_key' -> (-123, 456, 'key' | None)."""
        body = att[len("video"):]
        parts = body.split("_")
        owner = int(parts[0])
        vid = int(parts[1])
        key = parts[2] if len(parts) > 2 else None
        return owner, vid, key

    # --- сохранение ----------------------------------------------------------

    async def save_from_tg(self, tg_chat_id, name, reply_message, created_by):
        """Сохранить альт из TG (reply на видео). -> (ok, err)."""
        media = self.tg_media(reply_message)
        if media is None:
            return False, "no_media"
        kind, file_id = media
        alt_id = self.links.add_alt(
            tg_chat_id, name, origin="tg", kind=kind, tg_file_id=file_id,
            src_chat_id=reply_message.chat.id, src_msg_id=reply_message.message_id,
            created_by=created_by)
        if alt_id is None:
            return False, "dup"
        return True, None

    async def save_from_vk(self, tg_chat_id, name, message, created_by):
        """Сохранить альт из VK (reply на видео). -> (ok, err).

        Видео скачивается user-токеном и перезаливается в наше сообщество —
        получаем стабильное вложение, которым владеем, поэтому потом его можно
        и слать в VK, и качать для TG без привязки к исходному сообщению.
        """
        video = self.vk_message_video(message.reply_message)
        if video is None:
            return False, "no_media"
        if not (self.cfg.vk_user_token and self.vk_group_id):
            return False, "no_user_token"
        if self.links.get_alt(tg_chat_id, name) is not None:
            return False, "dup"

        peer_id = message.peer_id
        cmid = message.reply_message.conversation_message_id or message.reply_message.id
        data = await download_vk_video(
            self.cfg.vk_user_token, video.owner_id, video.id,
            getattr(video, "access_key", None), peer_id=peer_id, cmid=cmid)
        if not data:
            return False, "download_failed"
        try:
            attachment = await upload_vk_video(
                self.cfg.vk_user_token, self.vk_group_id, f"{name}.mp4", data)
        except Exception:  # noqa: BLE001
            log.exception("alt: перезаливка видео в VK не удалась")
            return False, "upload_failed"

        alt_id = self.links.add_alt(
            tg_chat_id, name, origin="vk", kind="video",
            vk_attachment=attachment, created_by=created_by)
        if alt_id is None:
            return False, "dup"
        return True, None

    # --- выдача --------------------------------------------------------------

    @staticmethod
    def caption(alt) -> str:
        """Подпись к видео: название + дата добавления."""
        name = alt["name"] or "видео"
        ts = alt["created_at"]
        if ts:
            return f"🎬 {name}\n📅 {datetime.fromtimestamp(ts):%d.%m.%Y}"
        return f"🎬 {name}"

    async def send_both(self, alt) -> bool:
        """Отправить альт сразу в TG-чат И в парную VK-беседу.

        Видео шлёт бот, а свои сообщения он не получает обратно, поэтому мост их
        не дублирует — рассылаем в обе стороны сами, чтобы запрос на одной
        платформе был виден и на другой. Стороны идут параллельно (ленивая
        конвертация одной не тормозит мгновенную выдачу на другой)."""
        tg_chat_id = alt["tg_chat_id"]
        vk_peer_id = self.links.vk_peer_for_tg_chat(tg_chat_id) if tg_chat_id else None

        jobs = []
        if tg_chat_id:
            jobs.append(("TG", self.send_to_tg(tg_chat_id, alt)))
        if vk_peer_id:
            jobs.append(("VK", self.send_to_vk(vk_peer_id, alt)))

        results = await asyncio.gather(*(c for _, c in jobs), return_exceptions=True)
        ok = False
        for (side, _), res in zip(jobs, results):
            if isinstance(res, Exception):
                log.error("alt: выдача в %s не удалась", side, exc_info=res)
            elif res:
                ok = True
        return ok

    async def send_to_tg(self, chat_id, alt) -> bool:
        """Отправить альт в TG-чат. Лениво конвертирует VK→TG и кеширует."""
        cap = self.caption(alt)
        file_id = alt["tg_file_id"]
        if file_id:
            await self._tg_send_by_kind(chat_id, alt["kind"] or "video", file_id, cap)
            return True

        # Легаси-альт старого формата: только координаты исходного сообщения.
        if alt["origin"] == "tg" and alt["src_chat_id"] and alt["src_msg_id"]:
            await self.tg_bot.copy_message(
                chat_id=chat_id, from_chat_id=alt["src_chat_id"],
                message_id=alt["src_msg_id"], caption=cap)
            return True

        # VK-origin без TG-кеша: качаем наше видео, шлём, кешируем file_id.
        att = alt["vk_attachment"]
        if att and self.cfg.vk_user_token:
            owner, vid, key = self._parse_vk_attachment(att)
            data = await download_vk_video(self.cfg.vk_user_token, owner, vid, key)
            if data:
                sent = await self.tg_bot.send_video(
                    chat_id, BufferedInputFile(data, filename="video.mp4"), caption=cap)
                fid = sent.video.file_id if sent and sent.video else None
                if fid:
                    self.links.set_alt_tg(alt["id"], fid, "video")
                return True
        return False

    async def send_to_vk(self, peer_id, alt) -> bool:
        """Отправить альт в VK-беседу. Лениво конвертирует TG→VK и кеширует."""
        cap = self.caption(alt)
        att = alt["vk_attachment"]
        if att:
            await self.vk_api.messages.send(
                peer_id=peer_id, message=cap, attachment=att, random_id=_random_id())
            return True

        # TG-origin без VK-кеша: качаем у TG, заливаем в VK, кешируем вложение.
        file_id = alt["tg_file_id"]
        if not file_id:
            return False  # легаси-альт без file_id — сконвертировать нельзя
        data = await _download_tg(self.tg_bot, file_id)
        fname = (alt["name"] or "video") + ".mp4"
        attachment = None
        if self.cfg.vk_user_token and self.vk_group_id:
            try:
                attachment = await upload_vk_video(
                    self.cfg.vk_user_token, self.vk_group_id, fname, data)
            except Exception:  # noqa: BLE001 — откат на документ
                log.exception("alt TG->VK: нативная заливка не удалась, шлю документом")
        if not attachment:
            attachment = await upload_doc(self.cfg.vk_token, peer_id, fname, data)
        self.links.set_alt_vk(alt["id"], attachment)
        await self.vk_api.messages.send(
            peer_id=peer_id, message=cap, attachment=attachment, random_id=_random_id())
        return True

    async def _tg_send_by_kind(self, chat_id, kind, file_id, caption=None):
        if kind == "animation":
            await self.tg_bot.send_animation(chat_id, file_id, caption=caption)
        elif kind == "video_note":
            # У кружков нет подписи — шлём её отдельным сообщением.
            await self.tg_bot.send_video_note(chat_id, file_id)
            if caption:
                await self.tg_bot.send_message(chat_id, caption)
        elif kind == "document":
            await self.tg_bot.send_document(chat_id, file_id, caption=caption)
        else:
            await self.tg_bot.send_video(chat_id, file_id, caption=caption)
