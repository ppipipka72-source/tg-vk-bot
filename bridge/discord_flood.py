"""Схлопывание флуда голосовых событий Discord в живое сообщение.

Проблема: когда пара человек скачет по войсам, чат заваливает десятками
строк «зашёл/вышел/перешёл». Здесь события группируются ПО ПОЛЬЗОВАТЕЛЮ: с
первого же события заводится ОДНО живое сообщение со свёрнутой цитатой
(Telegram expandable blockquote), в заголовке — где человек сейчас, в цитате —
все его действия. Каждое следующее событие дописывается в ту же цитату.

Когда закрывать окно (дальше следующее событие заведёт новое сообщение)?
Раньше — по таймеру тишины голосовых. Теперь — по тому, ПИСАЛИ ЛИ ЛЮДИ в чат
после уведомления: пока в чате тихо, живое сообщение обновляется сколько угодно
(хоть через час на выходе с войса — правится старое сообщение, оно никуда не
уехало). Как только люди написали `threshold` сообщений (кроме бота), окно
закрывается: старое сообщение «уехало» вверх, править его бессмысленно, и
следующее событие пишет новое. Счётчик человеческих сообщений приходит извне
через `note_human_message` (из обработчиков входящих TG/VK).

Страховочный предел `max_age`: даже при тихом чате окно всё равно закрывается,
когда сообщению становится слишком много лет — иначе упрёмся в лимит правок
(VK правит сообщения ≤24ч, TG ≤48ч) и правка молча провалится.

Правки живого сообщения дебаунсятся (edit_debounce), чтобы не упереться во
флуд-лимиты Telegram. Удалять ничего не нужно — сообщение изначально одно,
поэтому и прав администратора на удаление в TG не требуется.

VK свёрнутых цитат не умеет — там то же сообщение просто редактируется
растущим списком (длинный текст VK сам прячет под «показать полностью»).
"""

import asyncio
import html
import logging

from .discord_relay import edit_pair, send_pair

log = logging.getLogger("discord_flood")

# Сколько последних строк держим в цитате (защита от лимита 4096 и тяжёлых правок).
_MAX_LINES = 40


class _Stream:
    """Состояние живого сообщения по одному пользователю в одном чате."""

    __slots__ = ("chat_id", "name", "lines", "channel", "count", "here",
                 "agg", "msgs", "lock", "expire_task", "edit_task")

    def __init__(self, chat_id: int, name: str) -> None:
        self.chat_id = chat_id
        self.name = name
        self.lines: list[str] = []   # компактные строки действий для цитаты
        self.channel = ""            # где пользователь сейчас
        self.count = 0               # людей в текущем канале
        self.here = True             # в войсе (True) или уже вышел (False)
        self.agg: dict | None = None  # хэндл живого сообщения
        self.msgs = 0                # сообщений людей в чате после уведомления
        self.lock = asyncio.Lock()
        self.expire_task: asyncio.Task | None = None
        self.edit_task: asyncio.Task | None = None


class VoiceFloodAggregator:
    def __init__(self, side, *, threshold: int = 5, max_age: float = 82800.0,
                 edit_debounce: float = 1.2) -> None:
        self.side = side                 # DiscordSide: читаем tg_bot/vk_api/links лениво
        self.threshold = max(1, threshold)  # сообщений людей до закрытия окна
        self.max_age = max_age           # страховочный предел жизни сообщения, сек
        self.edit_debounce = edit_debounce
        self._streams: dict[tuple[int, int], _Stream] = {}

    async def add(self, tg_chat_id: int, member_id: int, name: str,
                  line: str, channel_name: str, count: int, here: bool) -> None:
        """Обработать одно голосовое событие пользователя.

        line — компактная строка для цитаты (без имени: имя в заголовке).
        here — остался ли пользователь в войсе (False для выхода).
        """
        key = (tg_chat_id, member_id)
        while True:
            st = self._streams.get(key)
            if st is None:
                st = _Stream(tg_chat_id, name)
                self._streams[key] = st
            async with st.lock:
                # Поток могли закрыть, пока мы ждали лок — берём заново.
                if self._streams.get(key) is not st:
                    continue
                st.name = name
                st.channel = channel_name
                st.count = count
                st.here = here
                st.lines.append(line)

                if st.agg is None:
                    tg_text, vk_text = self._render(st)
                    st.agg = await send_pair(self.side, tg_chat_id, tg_text,
                                             vk_text, tg_html=True)
                    self._arm_expire(key, st)
                else:
                    self._arm_edit(key, st)
                return

    async def note_human_message(self, tg_chat_id: int) -> None:
        """Учесть одно человеческое сообщение в чате (не бот).

        Крутим счётчик у всех живых окон этого чата; окно, у которого набралось
        threshold сообщений после уведомления, закрываем — следующее голосовое
        событие заведёт новое сообщение под свежими репликами людей.
        """
        for key, st in list(self._streams.items()):
            if key[0] != tg_chat_id:
                continue
            async with st.lock:
                if self._streams.get(key) is not st:
                    continue
                st.msgs += 1
                if st.msgs >= self.threshold:
                    await self._close_locked(key, st)

    # ---------- рендер ----------
    def _render(self, st: _Stream) -> tuple[str, str]:
        shown = st.lines[-_MAX_LINES:]
        hidden = len(st.lines) - len(shown)
        note = f"…ещё {hidden} действий выше\n" if hidden else ""

        where = f"сейчас в «{st.channel}» ({st.count})" if st.here else "вышел из войсов"
        head_tg = f"🎧 <b>{html.escape(st.name)}</b> — {html.escape(where)}"
        body_tg = html.escape(note + "\n".join(shown))
        tg = f"{head_tg}\n<blockquote expandable>{body_tg}</blockquote>"

        vk = f"🎧 {st.name} — {where}\n{note}" + "\n".join(shown)
        return tg, vk

    # ---------- закрытие окна ----------
    async def _close_locked(self, key, st: _Stream) -> None:
        """Финальная правка со свежим состоянием и снятие потока. Вызывать под
        st.lock. Идемпотентно: повторный вызов на уже снятом потоке — no-op."""
        if self._streams.get(key) is not st:
            return
        if st.edit_task:
            st.edit_task.cancel()
            st.edit_task = None
        if st.expire_task:
            st.expire_task.cancel()
            st.expire_task = None
        if st.agg is not None:
            tg_text, vk_text = self._render(st)
            await edit_pair(self.side, st.agg, tg_text, vk_text, tg_html=True)
        self._streams.pop(key, None)

    # ---------- страховочный предел жизни сообщения ----------
    def _arm_expire(self, key, st: _Stream) -> None:
        st.expire_task = asyncio.create_task(self._expire_later(key))

    async def _expire_later(self, key) -> None:
        try:
            await asyncio.sleep(self.max_age)
        except asyncio.CancelledError:
            return
        st = self._streams.get(key)
        if st is None:
            return
        async with st.lock:
            if self._streams.get(key) is not st:
                return
            st.expire_task = None  # это мы и есть — не отменяем сами себя в _close
            await self._close_locked(key, st)

    # ---------- дебаунс правок живого сообщения ----------
    def _arm_edit(self, key, st: _Stream) -> None:
        if st.edit_task and not st.edit_task.done():
            return  # правка уже запланирована — подхватит свежее состояние
        st.edit_task = asyncio.create_task(self._edit_later(key))

    async def _edit_later(self, key) -> None:
        try:
            await asyncio.sleep(self.edit_debounce)
        except asyncio.CancelledError:
            return
        st = self._streams.get(key)
        if st is None:
            return
        async with st.lock:
            st.edit_task = None
            if self._streams.get(key) is not st or st.agg is None:
                return
            tg_text, vk_text = self._render(st)
        await edit_pair(self.side, st.agg, tg_text, vk_text, tg_html=True)
