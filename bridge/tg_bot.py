import asyncio
import html
import logging
import re

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import Config
from .alts import ALT_HELP
from .media import (
    edit_vk_from_tg,
    relay_link_video,
    relay_voice_transcript,
    send_tg_message_to_vk,
)
from .state import LinkStore
from .video_dl import find_video_url

log = logging.getLogger(__name__)

_GROUP_TYPES = ("group", "supergroup")

# @all как отдельное слово (не часть e-mail/ника): @all, @All, @ALL.
_ALL_RE = re.compile(r"(?<![\w@])@all\b", re.IGNORECASE)
# Сколько упоминаний класть в одно сообщение (лимит TG ~4096 симв. и entities).
_MENTIONS_PER_MSG = 30


class TGSide:
    def __init__(self, cfg: Config, links: LinkStore):
        self.cfg = cfg
        self.links = links
        # Локальный Bot API сервер (если задан) снимает лимит 20 МБ на скачивание
        # и 50 МБ на отправку — нужен для переноса крупных видео. Иначе облачный.
        if cfg.tg_api_url:
            # timeout=600: крупные видео (скачивание getFile и заливка send_video
            # через локальный сервер) не укладываются в дефолтные ~60 с.
            session = AiohttpSession(
                api=TelegramAPIServer.from_base(cfg.tg_api_url, is_local=cfg.tg_api_local),
                timeout=600,
            )
            self.bot = Bot(token=cfg.tg_token, session=session)
            log.info("Telegram Bot API: локальный сервер %s (local=%s)",
                     cfg.tg_api_url, cfg.tg_api_local)
        else:
            self.bot = Bot(token=cfg.tg_token)
        self.dp = Dispatcher()
        self.vk_api = None  # выставляется в main
        self.vk_group_id = cfg.vk_group_id  # может уточниться автоопределением в main
        self.discord = None  # DiscordSide, выставляется в main (если включён Discord)
        self.alts = None  # AltService, выставляется в main
        self.webapp = None  # WebAppServer, выставляется в main (если включён /alt web)

        # Команды управления (только владельцы) — регистрируем ДО общего хендлера.
        self.dp.message(Command("link"))(self._cmd_link)
        self.dp.message(Command("unlink"))(self._cmd_unlink)
        self.dp.message(Command(commands=["status", "pairs"]))(self._cmd_status)
        self.dp.message(Command(commands=["help", "start"]))(self._cmd_help)
        # Discord-фича: /vc — всем, привязки — только владельцам.
        self.dp.message(Command(commands=["vc"]))(self._cmd_vc)
        self.dp.message(Command(commands=["ds_connect"]))(self._cmd_ds_connect)
        self.dp.message(Command(commands=["ds_disconnect"]))(self._cmd_ds_disconnect)
        # Персональный префикс — доступен всем участникам чата.
        self.dp.message(Command(commands=["prefix"]))(self._cmd_prefix)
        # Альты (сохранённые видео) — доступны всем участникам чата.
        self.dp.message(Command(commands=["alt"]))(self._cmd_alt)
        self.dp.callback_query(F.data.startswith("alt:"))(self._on_alt_cb)
        self.dp.callback_query(F.data.startswith("link:"))(self._on_link_cb)
        self.dp.callback_query(F.data.startswith("dsc:"))(self._on_ds_connect_cb)
        self.dp.callback_query(F.data.startswith("dsd:"))(self._on_ds_disconnect_cb)
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
            "• /status — список всех связанных пар\n"
            "\n<b>Discord (голосовые):</b>\n"
            "• /ds_connect — привязать Discord-сервер к этому чату\n"
            "• /ds_disconnect — отвязать Discord-сервер\n"
            "• /vc — кто сейчас в голосовых (картинка; доступно всем, в TG и VK)\n"
            "\n<b>Прочее (доступно всем):</b>\n"
            "• <code>@all</code> в сообщении — тегну всех участников чата "
            "(кого видел писавшими). В VK не дублируется.\n"
            "• <code>/prefix 🕋</code> — поставить личный префикс перед своим "
            "именем в пересланных сообщениях. <code>/prefix</code> без "
            "аргумента — убрать.\n"
            "• <code>/alt</code> — сохранённые видео: ответь на видео "
            "<code>/alt название</code>, потом <code>/alt list</code>.",
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

    # --- Discord: голосовые каналы ------------------------------------------

    async def _cmd_vc(self, message: Message) -> None:
        if self.discord is None:
            await message.reply("Discord-бот не подключён.")
            return
        if not self.discord.ready:
            await message.reply("Discord-бот ещё подключается, попробуй через "
                                "несколько секунд.")
            return
        count = await self.discord.handle_vc(message.chat.id)
        if count == 0:
            await message.reply("К этому чату не привязан Discord-сервер. "
                                "Владелец: /ds_connect.")

    async def _cmd_ds_connect(self, message: Message) -> None:
        if not self._is_admin(message.from_user.id if message.from_user else None):
            return
        if message.chat.type not in _GROUP_TYPES:
            await message.reply("Эту команду нужно писать в групповом чате, "
                                "который привязываешь.")
            return
        if self.discord is None or not self.discord.ready:
            await message.reply("Discord-бот не подключён (или ещё подключается).")
            return
        bound = {gid for gid, _ in self.links.ds_guilds_for_tg(message.chat.id)}
        options = [(gid, name) for gid, name in self.discord.list_guilds()
                   if gid not in bound]
        if not options:
            await message.reply("Нет доступных Discord-серверов для привязки "
                                "(бот не на серверах, либо все уже привязаны к "
                                "этому чату).")
            return
        rows = [[InlineKeyboardButton(text=name[:60], callback_data=f"dsc:{gid}")]
                for gid, name in options[:20]]
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await message.reply(
            "Выбери Discord-сервер, чьи голосовые слать в <b>этот</b> чат "
            "(и его VK-беседу):", reply_markup=kb, parse_mode="HTML")

    async def _on_ds_connect_cb(self, cb: CallbackQuery) -> None:
        if not self._is_admin(cb.from_user.id if cb.from_user else None):
            await cb.answer("Только для владельца бота.", show_alert=True)
            return
        try:
            guild_id = int(cb.data.split(":", 1)[1])
        except (ValueError, IndexError):
            await cb.answer("Некорректные данные.", show_alert=True)
            return
        name = (self.discord.guild_name(guild_id) if self.discord else None) or str(guild_id)
        self.links.add_ds_binding(guild_id, cb.message.chat.id, name)
        log.info("DS-привязка: сервер %s (%s) -> TG %s",
                 guild_id, name, cb.message.chat.id)
        await cb.message.edit_text(
            f"✅ Привязан Discord-сервер «{name}». События голосовых идут в этот "
            f"чат и в VK. /vc — полный список.")
        await cb.answer("Готово")

    async def _cmd_ds_disconnect(self, message: Message) -> None:
        if not self._is_admin(message.from_user.id if message.from_user else None):
            return
        bound = self.links.ds_guilds_for_tg(message.chat.id)
        if not bound:
            await message.reply("К этому чату не привязан ни один Discord-сервер.")
            return
        rows = [[InlineKeyboardButton(text=(name or str(gid))[:60],
                                      callback_data=f"dsd:{gid}")]
                for gid, name in bound]
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await message.reply("Выбери Discord-сервер для отвязки от этого чата:",
                            reply_markup=kb)

    async def _on_ds_disconnect_cb(self, cb: CallbackQuery) -> None:
        if not self._is_admin(cb.from_user.id if cb.from_user else None):
            await cb.answer("Только для владельца бота.", show_alert=True)
            return
        try:
            guild_id = int(cb.data.split(":", 1)[1])
        except (ValueError, IndexError):
            await cb.answer("Некорректные данные.", show_alert=True)
            return
        self.links.remove_ds_binding(guild_id, cb.message.chat.id)
        await cb.message.edit_text("✅ Discord-сервер отвязан от этого чата.")
        await cb.answer("Готово")

    # --- персональный префикс (/prefix) -------------------------------------

    _PREFIX_MAX = 16

    async def _cmd_prefix(self, message: Message, command: CommandObject) -> None:
        if message.from_user is None:
            return
        arg = (command.args or "").strip()
        if not arg:
            self.links.clear_prefix("tg", message.from_user.id)
            await message.reply(
                "Префикс убран. Чтобы поставить — пришли «/prefix 🕋».")
            return
        if len(arg) > self._PREFIX_MAX:
            await message.reply(
                f"Слишком длинный префикс (макс. {self._PREFIX_MAX} символов).")
            return
        self.links.set_prefix("tg", message.from_user.id, arg)
        await message.reply(
            f"Готово! Теперь твои сообщения будут с префиксом: {arg}\n"
            f"Убрать — «/prefix» без аргумента.")

    # --- альты: сохранённые видео (/alt) ------------------------------------
    # Ответы шлём в ОБА чата (alts.broadcast_text), а саму команду эхом — на
    # противоположную платформу (alts.echo_command), чтобы оба чата были
    # зеркалом. Локальный message.reply тут не используем.

    async def _cmd_alt(self, message: Message, command: CommandObject) -> None:
        chat_id = message.chat.id
        author = message.from_user.full_name if message.from_user else "?"
        await self.alts.echo_command("tg", chat_id, author, (message.text or "").strip())

        arg = (command.args or "").strip()
        if not arg:
            await self.alts.broadcast_text(chat_id, ALT_HELP)
            return

        head, _, rest = arg.partition(" ")
        sub = head.casefold()
        rest = rest.strip()

        if sub == "list":
            items = self.links.list_alts(chat_id)
            if not items:
                await self.alts.broadcast_text(
                    chat_id, "В этом чате пока нет сохранённых альтов. "
                    "Ответь на видео «/alt название».")
            else:
                await self.alts.broadcast_menu(
                    chat_id, f"Сохранённые альты ({len(items)}):", items)
            return

        if sub == "web":
            if not self.webapp:
                await self.alts.broadcast_text(
                    chat_id, "Веб-список альтов сейчас не настроен на сервере.")
                return
            url = self.webapp.build_start_url(chat_id)
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="🎬 Открыть список альтов", url=url)]])
            await self.bot.send_message(
                chat_id, "🌐 Веб-список альтов: поиск, фильтры по датам, превью "
                "и отправка в чат одним тапом.", reply_markup=kb)
            await self.alts.note_vk(
                chat_id, "🌐 Веб-список альтов открывается в Telegram (/alt web там).")
            return

        if sub == "search":
            if not rest:
                await self.alts.broadcast_text(chat_id, "Что искать? «/alt search часть_имени»")
                return
            items = self.links.search_alts(chat_id, rest)
            if not items:
                await self.alts.broadcast_text(chat_id, f"По запросу «{rest}» ничего не нашёл.")
            else:
                await self.alts.broadcast_menu(chat_id, f"Нашёл ({len(items)}):", items, rest)
            return

        if sub == "delete":
            if not rest:
                await self.alts.broadcast_text(chat_id, "Что удалить? «/alt delete название»")
                return
            if self.links.delete_alt(chat_id, rest):
                await self.alts.broadcast_text(chat_id, f"🗑 Удалил альт «{rest}».")
            else:
                await self.alts.broadcast_text(chat_id, f"Альта «{rest}» нет в этом чате.")
            return

        # Иначе arg — это название. С ответом на видео — сохраняем; без
        # ответа — присылаем ранее сохранённый альт с таким именем.
        name = arg
        reply = message.reply_to_message
        if reply is not None:
            if len(name) > 64:
                await self.alts.broadcast_text(chat_id, "Слишком длинное название (макс. 64).")
                return
            created_by = message.from_user.id if message.from_user else 0
            ok, err = await self.alts.save_from_tg(chat_id, name, reply, created_by)
            await self.alts.broadcast_text(chat_id, self.alts.save_message(ok, err, name))
            return

        # Без ответа — выдача по имени (видео уходит в оба чата).
        alt = self.links.get_alt(chat_id, name)
        if alt is None:
            await self.alts.broadcast_text(
                chat_id, f"Альта «{name}» нет. «/alt list» — список, "
                f"или ответь на видео «/alt {name}», чтобы сохранить.")
            return
        try:
            ok = await self.alts.send_both(alt)
        except Exception:  # noqa: BLE001
            log.exception("alt: ошибка выдачи «%s»", name)
            ok = False
        if not ok:
            await self.alts.broadcast_text(chat_id, "Не удалось отправить это видео.")

    async def _on_alt_cb(self, cb: CallbackQuery) -> None:
        try:
            alt_id = int(cb.data.split(":", 1)[1])
        except (ValueError, IndexError):
            await cb.answer("Некорректные данные.", show_alert=True)
            return
        row = self.links.get_alt_by_id(alt_id)
        if not row:
            await cb.answer("Этот альт уже удалён.", show_alert=True)
            return
        try:
            ok = await self.alts.send_both(row)
        except Exception:  # noqa: BLE001
            log.exception("alt: не удалось отправить видео «%s»", row["name"])
            ok = False
        if ok:
            await cb.answer()
        else:
            await cb.answer("Не удалось отправить видео.", show_alert=True)

    # --- обычные сообщения --------------------------------------------------

    async def _on_message(self, message: Message) -> None:
        vk_peer_id = self.links.vk_peer_for_tg_chat(message.chat.id)
        if vk_peer_id is None:
            return  # чат не связан — игнорируем

        # Служебные сообщения о входе/выходе — поддерживаем список участников.
        for u in (message.new_chat_members or []):
            if not u.is_bot:
                self.links.track_member(message.chat.id, u.id,
                                        u.username, u.full_name)
        if message.left_chat_member and not message.left_chat_member.is_bot:
            self.links.forget_member(message.chat.id, message.left_chat_member.id)

        # Игнорируем ботов (в т.ч. эхо нашего бота) и служебные сообщения.
        if message.from_user is None or message.from_user.is_bot:
            return

        # Копим участников чата для @all (Bot API не умеет перечислять всех).
        self.links.track_member(message.chat.id, message.from_user.id,
                                message.from_user.username,
                                message.from_user.full_name)

        # @all — тегаем всех известных участников ОТДЕЛЬНЫМ сообщением бота.
        # Оно не попадает в VK: бот не получает свои же апдейты, а ниже мы
        # его и не релеим. Само сообщение пользователя с «@all» уходит в VK
        # как обычно.
        text_all = message.text or message.caption or ""
        if _ALL_RE.search(text_all):
            asyncio.create_task(self._tag_everyone(message))

        name = message.from_user.full_name
        prefix = self.links.get_prefix("tg", message.from_user.id)
        try:
            await send_tg_message_to_vk(
                self.bot, self.vk_api, self.cfg.vk_token, self.cfg.vk_user_token,
                self.vk_group_id, vk_peer_id, name, message, self.links, prefix)
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

        # Голосовое из TG: расшифровываем и отвечаем текстом в TG (на оригинал)
        # и в VK (на пересланную версию). Фоном — Vosk/ffmpeg блокирующие.
        if message.voice:
            vk_cmid = self.links.vk_for_tg(message.chat.id, message.message_id)
            asyncio.create_task(relay_voice_transcript(
                self.bot, self.vk_api,
                tg_chat_id=message.chat.id, tg_reply_to=message.message_id,
                vk_peer_id=vk_peer_id, vk_reply_cmid=vk_cmid,
                tg_file_id=message.voice.file_id))

    async def _tag_everyone(self, message: Message) -> None:
        """Ответить на @all упоминаниями всех накопленных участников чата."""
        author_id = message.from_user.id if message.from_user else None
        mentions: list[str] = []
        for user_id, username, full_name in self.links.chat_members(message.chat.id):
            if user_id == author_id:
                continue  # автора @all не дёргаем
            if username:
                mentions.append(f"@{username}")
            else:
                label = html.escape(full_name or "user")
                mentions.append(f'<a href="tg://user?id={user_id}">{label}</a>')

        if not mentions:
            await message.reply("Пока некого тегать: я ещё не видел сообщений "
                                "других участников этого чата.")
            return

        for i in range(0, len(mentions), _MENTIONS_PER_MSG):
            chunk = " ".join(mentions[i:i + _MENTIONS_PER_MSG])
            try:
                await self.bot.send_message(
                    message.chat.id, chunk, parse_mode="HTML",
                    reply_to_message_id=message.message_id if i == 0 else None,
                    disable_notification=False)
            except Exception:  # noqa: BLE001
                log.exception("@all: не удалось отправить упоминания")

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
        prefix = self.links.get_prefix("tg", message.from_user.id)
        await edit_vk_from_tg(self.vk_api, vk_peer_id, vk_cmid,
                              message.from_user.full_name, message.text, prefix)

    async def start(self) -> None:
        log.info("Telegram polling запущен (пар: %d, владельцев: %d)",
                 len(self.links.all_pairs()), len(self.cfg.admin_ids))
        await self.dp.start_polling(self.bot, handle_signals=False)
