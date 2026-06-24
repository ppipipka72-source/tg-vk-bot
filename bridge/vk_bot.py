import asyncio
import logging
import random

from vkbottle import Callback, GroupEventType, Keyboard
from vkbottle.bot import Bot, Message, MessageEventMin

from config import Config
from .media import relay_link_video, send_vk_message_to_tg
from .state import LinkStore
from .video_dl import find_video_url

log = logging.getLogger(__name__)

_ALT_HELP = (
    "Альты — сохранённые видео:\n"
    "• ответь на видео «/alt название» — сохранить;\n"
    "• «/alt название» (без ответа) — прислать это видео;\n"
    "• «/alt list» — кнопки со всеми видео;\n"
    "• «/alt search название» — найти; «/alt delete название» — удалить.\n"
    "Альты общие с Telegram-чатом: сохранил там — доступно тут."
)


class VKSide:
    def __init__(self, cfg: Config, links: LinkStore):
        self.cfg = cfg
        self.links = links
        self.bot = Bot(token=cfg.vk_token)
        self.api = self.bot.api
        self.tg_bot = None  # выставляется в main
        self.vk_group_id = cfg.vk_group_id  # может уточниться автоопределением в main
        self.discord = None  # DiscordSide, выставляется в main (если включён Discord)
        self.alts = None  # AltService, выставляется в main
        self._name_cache: dict[int, str] = {}

        self.bot.on.message()(self._on_message)
        # Нажатия inline-кнопок альтов (callback-кнопки -> message_event).
        self.bot.on.raw_event(
            GroupEventType.MESSAGE_EVENT, dataclass=MessageEventMin)(self._on_alt_event)

    async def _resolve_name(self, user_id: int) -> str:
        if user_id in self._name_cache:
            return self._name_cache[user_id]
        name = f"id{user_id}"
        try:
            if user_id > 0:
                users = await self.api.users.get(user_ids=[user_id])
                if users:
                    u = users[0]
                    name = f"{u.first_name} {u.last_name}".strip()
            else:
                groups = await self.api.groups.get_by_id(group_id=str(-user_id))
                if groups:
                    name = groups[0].name
        except Exception:  # noqa: BLE001
            log.exception("VK: не удалось получить имя для %s", user_id)
        self._name_cache[user_id] = name
        return name

    async def _on_message(self, message: Message) -> None:
        # Диагностика: реальный peer_id входящих сообщений (LOG_LEVEL=DEBUG).
        log.debug("VK входящее: peer_id=%s from_id=%s text=%r",
                  message.peer_id, message.from_id, (message.text or "")[:50])
        # Беседы (peer >= 2e9) запоминаем для /link, даже если ещё не связаны.
        if message.peer_id and message.peer_id >= 2000000000:
            self.links.mark_seen_vk(message.peer_id)
        # Беседа должна быть связана с TG-чатом, иначе игнорируем.
        tg_chat_id = self.links.tg_chat_for_vk_peer(message.peer_id)
        if tg_chat_id is None:
            return
        # Игнорируем сообщения от сообществ/ботов (в т.ч. эхо нашего бота).
        if message.from_id is None or message.from_id < 0:
            return

        # Команда /vc — кто в голосовых Discord (вывод в оба чата). Не пересылаем.
        if (message.text or "").strip().lower() == "/vc":
            await self._handle_vc(message.peer_id, tg_chat_id)
            return

        # Команда /alt — сохранённые видео. Перехватываем (в TG не пересылаем).
        low = (message.text or "").strip().lower()
        if low == "/alt" or low.startswith("/alt ") or low.startswith("/alt\n"):
            await self._cmd_alt(message, tg_chat_id)
            return

        name = await self._resolve_name(message.from_id)
        try:
            await send_vk_message_to_tg(
                self.tg_bot, self.cfg.vk_user_token, tg_chat_id,
                name, message, self.links)
        except Exception:  # noqa: BLE001
            log.exception("VK->TG: ошибка доставки сообщения")

        # Ссылка на видео (TikTok/Shorts/Reels) -> качаем и отвечаем в оба чата.
        text = message.text or ""
        if find_video_url(text):
            vk_cmid = message.conversation_message_id or message.id
            tg_anchor = self.links.tg_for_vk(message.peer_id, vk_cmid)
            asyncio.create_task(relay_link_video(
                self.tg_bot, self.api, tg_chat_id, message.peer_id,
                self.cfg.vk_token, self.cfg.vk_user_token, self.vk_group_id,
                text, tg_anchor, vk_cmid))

    # --- альты: сохранённые видео (/alt) ------------------------------------

    async def _vk_send(self, peer_id: int, text: str, keyboard: str | None = None) -> None:
        params = dict(peer_id=peer_id, message=text, random_id=random.getrandbits(31))
        if keyboard is not None:
            params["keyboard"] = keyboard
        await self.api.messages.send(**params)

    @staticmethod
    def _alt_keyboard(items: list[tuple[int, str]]) -> str:
        """Inline-меню альтов: callback-кнопки по 2 в ряд (VK-лимиты — до 10 рядов)."""
        kb = Keyboard(inline=False, one_time=True)
        for i, (aid, name) in enumerate(items[:20]):
            if i and i % 2 == 0:
                kb.row()
            kb.add(Callback((name or "?")[:40], payload={"alt": aid}))
        return kb.get_json()

    async def _cmd_alt(self, message: Message, tg_chat_id: int) -> None:
        arg = (message.text or "").strip()[len("/alt"):].strip()
        peer_id = message.peer_id
        if not arg:
            await self._vk_send(peer_id, _ALT_HELP)
            return

        head, _, rest = arg.partition(" ")
        sub = head.casefold()
        rest = rest.strip()

        if sub == "list":
            items = self.links.list_alts(tg_chat_id)
            if not items:
                await self._vk_send(peer_id, "В этом чате пока нет сохранённых "
                                    "альтов. Ответь на видео «/alt название».")
                return
            extra = "" if len(items) <= 20 else f" (показаны первые 20 из {len(items)})"
            await self._vk_send(peer_id, f"Сохранённые альты ({len(items)}){extra}:",
                                keyboard=self._alt_keyboard(items))
            return

        if sub == "search":
            if not rest:
                await self._vk_send(peer_id, "Что искать? «/alt search часть_имени»")
                return
            items = self.links.search_alts(tg_chat_id, rest)
            if not items:
                await self._vk_send(peer_id, f"По запросу «{rest}» ничего не нашёл.")
                return
            await self._vk_send(peer_id, f"Нашёл ({len(items)}):",
                                keyboard=self._alt_keyboard(items))
            return

        if sub == "delete":
            if not rest:
                await self._vk_send(peer_id, "Что удалить? «/alt delete название»")
                return
            if self.links.delete_alt(tg_chat_id, rest):
                await self._vk_send(peer_id, f"🗑 Удалил альт «{rest}».")
            else:
                await self._vk_send(peer_id, f"Альта «{rest}» нет в этом чате.")
            return

        # arg — название. С ответом на видео сохраняем, без ответа — выдаём.
        name = arg
        if message.reply_message is not None:
            if self.alts.vk_message_video(message.reply_message) is None:
                await self._vk_send(peer_id, "В том сообщении нет видео.")
                return
            if len(name) > 64:
                await self._vk_send(peer_id, "Слишком длинное название (макс. 64).")
                return
            if self.links.get_alt(tg_chat_id, name) is not None:
                await self._vk_send(peer_id, f"Альт «{name}» уже есть. "
                                    f"Удали старый: «/alt delete {name}».")
                return
            await self._vk_send(peer_id, f"⏳ Сохраняю «{name}»…")
            asyncio.create_task(self._save_alt(message, tg_chat_id, name))
            return

        alt = self.links.get_alt(tg_chat_id, name)
        if alt is None:
            await self._vk_send(peer_id, f"Альта «{name}» нет. «/alt list» — список, "
                                f"или ответь на видео «/alt {name}», чтобы сохранить.")
            return
        asyncio.create_task(self._deliver_alt(peer_id, alt))

    async def _save_alt(self, message: Message, tg_chat_id: int, name: str) -> None:
        try:
            ok, err = await self.alts.save_from_vk(
                tg_chat_id, name, message, message.from_id)
        except Exception:  # noqa: BLE001
            log.exception("alt VK: ошибка сохранения «%s»", name)
            ok, err = False, "error"
        if ok:
            await self._vk_send(message.peer_id, f"✅ Сохранил альт «{name}». "
                                "«/alt list» — все.")
            return
        reason = {
            "no_media": "в том сообщении нет видео",
            "dup": f"альт «{name}» уже есть",
            "no_user_token": "сохранение видео из VK недоступно (нет user-токена)",
            "download_failed": "не удалось скачать это видео из VK",
            "upload_failed": "не удалось сохранить видео",
        }.get(err, "не удалось сохранить")
        await self._vk_send(message.peer_id, f"⚠️ {reason}.")

    async def _deliver_alt(self, peer_id: int, alt) -> None:
        # send_both шлёт и в эту беседу, и в парный TG-чат, чтобы запрос был
        # виден на обеих платформах.
        try:
            ok = await self.alts.send_both(alt)
        except Exception:  # noqa: BLE001
            log.exception("alt VK: ошибка выдачи «%s»", alt["name"])
            ok = False
        if not ok:
            await self._vk_send(peer_id, f"Не удалось отправить «{alt['name']}».")

    async def _on_alt_event(self, event: MessageEventMin) -> None:
        payload = event.payload or {}
        alt_id = payload.get("alt")
        if alt_id is None:
            return
        row = self.links.get_alt_by_id(int(alt_id))
        if not row:
            await event.show_snackbar("Этот альт уже удалён.")
            return
        # Отвечаем на событие сразу (снять «часики»), видео шлём фоном — ленивая
        # конвертация TG→VK может занять секунды и не уложиться в окно ответа.
        await event.show_snackbar(f"🎬 {row['name']}")
        asyncio.create_task(self._deliver_alt(event.peer_id, row))

    async def _handle_vc(self, peer_id: int, tg_chat_id: int) -> None:
        if self.discord is None or not self.discord.ready:
            await self.api.messages.send(
                peer_id=peer_id, message="Discord-бот не подключён.",
                random_id=random.getrandbits(31))
            return
        count = await self.discord.handle_vc(tg_chat_id)
        if count == 0:
            await self.api.messages.send(
                peer_id=peer_id,
                message="К этому чату не привязан Discord-сервер (в Telegram: /ds_connect).",
                random_id=random.getrandbits(31))

    async def start(self) -> None:
        log.info("VK polling запущен (пар: %d)", len(self.links.all_pairs()))
        await self.bot.run_polling()
