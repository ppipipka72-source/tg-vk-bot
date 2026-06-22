import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import Config
from .media import edit_vk_from_tg, relay_link_video, send_tg_message_to_vk
from .state import LinkStore
from .video_dl import find_video_url

log = logging.getLogger(__name__)

_GROUP_TYPES = ("group", "supergroup")


class TGSide:
    def __init__(self, cfg: Config, links: LinkStore):
        self.cfg = cfg
        self.links = links
        self.bot = Bot(token=cfg.tg_token)
        self.dp = Dispatcher()
        self.vk_api = None  # выставляется в main
        self.vk_group_id = cfg.vk_group_id  # может уточниться автоопределением в main

        # Команды управления (только владельцы) — регистрируем ДО общего хендлера.
        self.dp.message(Command("link"))(self._cmd_link)
        self.dp.message(Command("unlink"))(self._cmd_unlink)
        self.dp.message(Command(commands=["status", "pairs"]))(self._cmd_status)
        self.dp.message(Command(commands=["help", "start"]))(self._cmd_help)
        self.dp.callback_query(F.data.startswith("link:"))(self._on_link_cb)
        self.dp.edited_message()(self._on_edited_message)
        self.dp.message()(self._on_message)

    # --- утилиты ------------------------------------------------------------

    def _is_admin(self, user_id: int | None) -> bool:
        return bool(user_id) and user_id in self.cfg.admin_ids

    async def _vk_titles(self, peer_ids: list[int]) -> dict[int, str]:
        """Названия бесед VK по peer_id (community-токен умеет это для известных)."""
        out: dict[int, str] = {}
        if not peer_ids:
            return out
        try:
            resp = await self.vk_api.messages.get_conversations_by_id(peer_ids=peer_ids)
            for conv in (getattr(resp, "items", None) or []):
                if conv.chat_settings and conv.chat_settings.title:
                    out[conv.peer.id] = conv.chat_settings.title
        except Exception:  # noqa: BLE001
            log.exception("Не удалось получить названия бесед VK")
        return out

    # --- команды ------------------------------------------------------------

    async def _cmd_help(self, message: Message) -> None:
        if not self._is_admin(message.from_user.id if message.from_user else None):
            return
        await message.reply(
            "Я связываю чаты TG ↔ VK. Команды (только для владельца):\n"
            "• /link — связать <b>этот</b> чат с беседой VK (покажу кнопки выбора)\n"
            "• /unlink — отвязать этот чат\n"
            "• /status — список всех связанных пар",
            parse_mode="HTML",
        )

    async def _cmd_link(self, message: Message) -> None:
        if not self._is_admin(message.from_user.id if message.from_user else None):
            return
        if message.chat.type not in _GROUP_TYPES:
            await message.reply("Эту команду нужно писать в групповом чате, "
                                "который хочешь связать.")
            return

        free = self.links.seen_vk_unpaired()
        if not free:
            await message.reply(
                "Бот пока не видел свободных бесед VK. Добавь сообщество в нужную "
                "беседу VK и напиши там любое сообщение — после этого повтори /link.")
            return

        # Подтягиваем актуальные названия для кнопок.
        titles = await self._vk_titles([pid for pid, _ in free])
        for pid, title in titles.items():
            self.links.mark_seen_vk(pid, title)  # запомним красивое название
        rows = []
        for pid, title in free:
            label = (titles.get(pid) or title or f"беседа {pid}")[:60]
            rows.append([InlineKeyboardButton(text=label, callback_data=f"link:{pid}")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await message.reply("Выбери беседу VK для связи с <b>этим</b> чатом:",
                            reply_markup=kb, parse_mode="HTML")

    async def _on_link_cb(self, cb: CallbackQuery) -> None:
        if not self._is_admin(cb.from_user.id if cb.from_user else None):
            await cb.answer("Только для владельца бота.", show_alert=True)
            return
        try:
            peer_id = int(cb.data.split(":", 1)[1])
        except (ValueError, IndexError):
            await cb.answer("Некорректные данные.", show_alert=True)
            return

        tg_chat_id = cb.message.chat.id
        # Подтянем актуальное название беседы.
        title = (await self._vk_titles([peer_id])).get(peer_id) or f"беседа {peer_id}"

        self.links.add_pair(tg_chat_id, peer_id, title)
        log.info("Связана пара: TG %s <-> VK %s (%s)", tg_chat_id, peer_id, title)
        await cb.message.edit_text(
            f"✅ Связано: этот чат ↔ «{title}» (VK).\n"
            f"Сообщения теперь дублируются в обе стороны.")
        await cb.answer("Готово")

    async def _cmd_unlink(self, message: Message) -> None:
        if not self._is_admin(message.from_user.id if message.from_user else None):
            return
        removed = self.links.remove_pair_by_tg(message.chat.id)
        if removed:
            await message.reply("Этот чат отвязан от беседы VK.")
        else:
            await message.reply("Этот чат и так ни с чем не связан.")

    async def _cmd_status(self, message: Message) -> None:
        if not self._is_admin(message.from_user.id if message.from_user else None):
            return
        pairs = self.links.all_pairs()
        if not pairs:
            await message.reply("Пока нет ни одной связанной пары. Напиши /link "
                                "в нужном чате.")
            return
        lines = ["<b>Связанные пары TG ↔ VK:</b>"]
        for tg_chat_id, vk_peer_id, title in pairs:
            mark = " (этот чат)" if tg_chat_id == message.chat.id else ""
            lines.append(f"• TG <code>{tg_chat_id}</code>{mark} ↔ "
                         f"VK «{title}» <code>{vk_peer_id}</code>")
        await message.reply("\n".join(lines), parse_mode="HTML")

    # --- обычные сообщения --------------------------------------------------

    async def _on_message(self, message: Message) -> None:
        vk_peer_id = self.links.vk_peer_for_tg_chat(message.chat.id)
        if vk_peer_id is None:
            return  # чат не связан — игнорируем
        # Игнорируем ботов (в т.ч. эхо нашего бота) и служебные сообщения.
        if message.from_user is None or message.from_user.is_bot:
            return

        name = message.from_user.full_name
        try:
            await send_tg_message_to_vk(
                self.bot, self.vk_api, self.cfg.vk_token, self.cfg.vk_user_token,
                self.vk_group_id, vk_peer_id, name, message, self.links)
        except Exception:  # noqa: BLE001
            log.exception("TG->VK: ошибка доставки сообщения")

        # Ссылка на видео (TikTok/Shorts/Reels) -> качаем и отвечаем в оба чата.
        text = message.text or message.caption or ""
        if find_video_url(text):
            vk_anchor = self.links.vk_for_tg(message.chat.id, message.message_id)
            asyncio.create_task(relay_link_video(
                self.bot, self.vk_api, message.chat.id, vk_peer_id,
                self.cfg.vk_token, self.cfg.vk_user_token, self.vk_group_id,
                text, message.message_id, vk_anchor))

    async def _on_edited_message(self, message: Message) -> None:
        vk_peer_id = self.links.vk_peer_for_tg_chat(message.chat.id)
        if vk_peer_id is None:
            return
        if message.from_user is None or message.from_user.is_bot:
            return
        # Только текстовые правки: правка подписи к медиа стёрла бы вложение в VK.
        if not message.text:
            return
        vk_cmid = self.links.vk_for_tg(message.chat.id, message.message_id)
        if not vk_cmid:
            return
        await edit_vk_from_tg(self.vk_api, vk_peer_id, vk_cmid,
                              message.from_user.full_name, message.text)

    async def start(self) -> None:
        log.info("Telegram polling запущен (пар: %d, владельцев: %d)",
                 len(self.links.all_pairs()), len(self.cfg.admin_ids))
        await self.dp.start_polling(self.bot, handle_signals=False)
