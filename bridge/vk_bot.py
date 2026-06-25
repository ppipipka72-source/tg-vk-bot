import asyncio
import logging
import random

from vkbottle import GroupEventType
from vkbottle.bot import Bot, Message, MessageEventMin

from config import Config
from .alts import ALT_HELP
from .media import (
    find_vk_voice,
    relay_link_video,
    relay_voice_transcript,
    send_vk_message_to_tg,
)
from .state import LinkStore
from .video_dl import find_video_url

log = logging.getLogger(__name__)


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

        # Команда /prefix — личный префикс. Перехватываем (в TG не пересылаем).
        if low == "/prefix" or low.startswith("/prefix ") or low.startswith("/prefix\n"):
            await self._cmd_prefix(message)
            return

        name = await self._resolve_name(message.from_id)
        prefix = self.links.get_prefix("vk", message.from_id)
        tg_voice_id = None
        try:
            tg_voice_id = await send_vk_message_to_tg(
                self.tg_bot, self.cfg.vk_user_token, tg_chat_id,
                name, message, self.links, prefix)
        except Exception:  # noqa: BLE001
            log.exception("VK->TG: ошибка доставки сообщения")

        # Голосовое из VK: расшифровываем и отвечаем текстом в VK (на оригинал)
        # и в TG (на пересланную версию). Фоном — Vosk/ffmpeg блокирующие.
        voice = find_vk_voice(message.attachments)
        if voice is not None:
            vk_cmid = message.conversation_message_id or message.id
            asyncio.create_task(relay_voice_transcript(
                self.tg_bot, self.api,
                tg_chat_id=tg_chat_id, tg_reply_to=tg_voice_id,
                vk_peer_id=message.peer_id, vk_reply_cmid=vk_cmid,
                ogg_url=(voice.link_ogg or voice.link_mp3)))

        # Ссылка на видео (TikTok/Shorts/Reels) -> качаем и отвечаем в оба чата.
        text = message.text or ""
        if find_video_url(text):
            vk_cmid = message.conversation_message_id or message.id
            tg_anchor = self.links.tg_for_vk(message.peer_id, vk_cmid)
            asyncio.create_task(relay_link_video(
                self.tg_bot, self.api, tg_chat_id, message.peer_id,
                self.cfg.vk_token, self.cfg.vk_user_token, self.vk_group_id,
                text, tg_anchor, vk_cmid))

    # --- персональный префикс (/prefix) -------------------------------------

    _PREFIX_MAX = 16

    async def _cmd_prefix(self, message: Message) -> None:
        arg = (message.text or "").strip()[len("/prefix"):].strip()
        if not arg:
            self.links.clear_prefix("vk", message.from_id)
            reply = "Префикс убран. Чтобы поставить — пришли «/prefix 🕋»."
        elif len(arg) > self._PREFIX_MAX:
            reply = f"Слишком длинный префикс (макс. {self._PREFIX_MAX} символов)."
        else:
            self.links.set_prefix("vk", message.from_id, arg)
            reply = (f"Готово! Теперь твои сообщения будут с префиксом: {arg}\n"
                     f"Убрать — «/prefix» без аргумента.")
        await self.api.messages.send(
            peer_id=message.peer_id, message=reply, random_id=random.getrandbits(31))

    # --- альты: сохранённые видео (/alt) ------------------------------------
    # Ответы шлём в ОБА чата (alts.broadcast_text), команду эхом — в парный
    # TG-чат (alts.echo_command), чтобы оба чата были зеркалом.

    async def _cmd_alt(self, message: Message, tg_chat_id: int) -> None:
        author = await self._resolve_name(message.from_id)
        await self.alts.echo_command("vk", tg_chat_id, author, (message.text or "").strip())

        arg = (message.text or "").strip()[len("/alt"):].strip()
        if not arg:
            await self.alts.broadcast_text(tg_chat_id, ALT_HELP)
            return

        head, _, rest = arg.partition(" ")
        sub = head.casefold()
        rest = rest.strip()

        if sub == "list":
            items = self.links.list_alts(tg_chat_id)
            if not items:
                await self.alts.broadcast_text(
                    tg_chat_id, "В этом чате пока нет сохранённых альтов. "
                    "Ответь на видео «/alt название».")
            else:
                await self.alts.broadcast_menu(
                    tg_chat_id, f"Сохранённые альты ({len(items)}):", items)
            return

        if sub == "web":
            await self.alts.broadcast_text(
                tg_chat_id, "🌐 Веб-список альтов открывается в Telegram: "
                "напиши там «/alt web».")
            return

        if sub == "search":
            if not rest:
                await self.alts.broadcast_text(tg_chat_id, "Что искать? «/alt search часть_имени»")
                return
            items = self.links.search_alts(tg_chat_id, rest)
            if not items:
                await self.alts.broadcast_text(tg_chat_id, f"По запросу «{rest}» ничего не нашёл.")
            else:
                await self.alts.broadcast_menu(tg_chat_id, f"Нашёл ({len(items)}):", items, rest)
            return

        if sub == "delete":
            if not rest:
                await self.alts.broadcast_text(tg_chat_id, "Что удалить? «/alt delete название»")
                return
            if self.links.delete_alt(tg_chat_id, rest):
                await self.alts.broadcast_text(tg_chat_id, f"🗑 Удалил альт «{rest}».")
            else:
                await self.alts.broadcast_text(tg_chat_id, f"Альта «{rest}» нет в этом чате.")
            return

        # arg — название. С ответом на видео сохраняем, без ответа — выдаём.
        name = arg
        if message.reply_message is not None:
            if self.alts.vk_message_video(message.reply_message) is None:
                await self.alts.broadcast_text(tg_chat_id, "В том сообщении нет видео.")
                return
            if len(name) > 64:
                await self.alts.broadcast_text(tg_chat_id, "Слишком длинное название (макс. 64).")
                return
            if self.links.get_alt(tg_chat_id, name) is not None:
                await self.alts.broadcast_text(
                    tg_chat_id, f"Альт «{name}» уже есть. Удали старый: «/alt delete {name}».")
                return
            await self.alts.broadcast_text(tg_chat_id, f"⏳ Сохраняю «{name}»…")
            asyncio.create_task(self._save_alt(message, tg_chat_id, name))
            return

        alt = self.links.get_alt(tg_chat_id, name)
        if alt is None:
            await self.alts.broadcast_text(
                tg_chat_id, f"Альта «{name}» нет. «/alt list» — список, "
                f"или ответь на видео «/alt {name}», чтобы сохранить.")
            return
        asyncio.create_task(self._deliver_alt(alt))

    async def _save_alt(self, message: Message, tg_chat_id: int, name: str) -> None:
        try:
            ok, err = await self.alts.save_from_vk(
                tg_chat_id, name, message, message.from_id)
        except Exception:  # noqa: BLE001
            log.exception("alt VK: ошибка сохранения «%s»", name)
            ok, err = False, "error"
        await self.alts.broadcast_text(tg_chat_id, self.alts.save_message(ok, err, name))

    async def _deliver_alt(self, alt) -> None:
        # send_both шлёт и в эту беседу, и в парный TG-чат, чтобы запрос был
        # виден на обеих платформах.
        try:
            ok = await self.alts.send_both(alt)
        except Exception:  # noqa: BLE001
            log.exception("alt VK: ошибка выдачи «%s»", alt["name"])
            ok = False
        if not ok:
            await self.alts.broadcast_text(
                alt["tg_chat_id"], f"Не удалось отправить «{alt['name']}».")

    async def _on_alt_event(self, event: MessageEventMin) -> None:
        payload = event.payload or {}
        cmid = event.conversation_message_id

        # Листание меню («ещё ▶️»): перерисовываем кнопки на нужную страницу.
        if "p" in payload:
            page = int(payload.get("p") or 0)
            query = payload.get("q") or ""
            tg_chat_id = self.links.tg_chat_for_vk_peer(event.peer_id)
            items = self.alts.menu_items(tg_chat_id, query) if tg_chat_id else []
            header = self.alts.menu_header(items, query)
            try:
                await event.edit_message(
                    message=header,
                    keyboard=self.alts.vk_menu_keyboard(items, page, query))
            except Exception:  # noqa: BLE001
                log.exception("alt VK: листание меню")
            self.alts.schedule_menu_close(event.peer_id, cmid, header)
            await event.send_empty_answer()
            return

        alt_id = payload.get("a")
        if alt_id is None:
            await event.send_empty_answer()
            return

        # Выбор альта: убираем таймер, сворачиваем меню (кнопки пропадают),
        # шлём видео фоном (ленивая конвертация может не уложиться в окно ответа).
        self.alts.cancel_menu_timer(cmid)
        row = self.links.get_alt_by_id(int(alt_id))
        if not row:
            try:
                await event.edit_message(message="Этот альт уже удалён.",
                                         keyboard=self.alts.empty_keyboard())
            except Exception:  # noqa: BLE001
                pass
            await event.show_snackbar("Этот альт уже удалён.")
            return
        try:
            await event.edit_message(message=f"🎬 {row['name']}",
                                     keyboard=self.alts.empty_keyboard())
        except Exception:  # noqa: BLE001
            log.exception("alt VK: сворачивание меню")
        await event.show_snackbar(f"🎬 {row['name']}")
        asyncio.create_task(self._deliver_alt(row))

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
