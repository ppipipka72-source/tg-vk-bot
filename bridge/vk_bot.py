import asyncio
import logging
import random

from vkbottle.bot import Bot, Message

from config import Config
from .media import relay_link_video, send_vk_message_to_tg
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
        self._name_cache: dict[int, str] = {}

        self.bot.on.message()(self._on_message)

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
