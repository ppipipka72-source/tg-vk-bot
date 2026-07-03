"""Схлопывание флуда голосовых событий Discord в живое сообщение.

Проблема: когда пара человек скачет по войсам, чат заваливает десятками
строк «зашёл/вышел/перешёл». Здесь события группируются ПО ПОЛЬЗОВАТЕЛЮ:
первые `keep` событий идут обычными сообщениями (как раньше, без регрессии
для спокойного чата). Но если один и тот же человек за короткое окно нашумел
сверх порога — его отдельные сообщения удаляются, а вместо них поднимается
одно живое сообщение со свёрнутой цитатой (Telegram expandable blockquote),
куда дописываются все дальнейшие его действия.

Окно скользящее: закрывается после `silence` секунд тишины (по этому
пользователю), дальше начинается новое сообщение. Правки живого сообщения
дебаунсятся (edit_debounce), чтобы не упереться во флуд-лимиты Telegram.

VK свёрнутых цитат не умеет — там то же сообщение просто редактируется
растущим списком (длинный текст VK сам прячет под «показать полностью»).
"""

import asyncio
import html
import logging

from .discord_relay import delete_pair, edit_pair, send_pair

log = logging.getLogger("discord_flood")

# Сколько последних строк держим в цитате (защита от лимита 4096 и тяжёлых правок).
_MAX_LINES = 40


class _Stream:
    """Состояние агрегации по одному пользователю в одном чате."""

    __slots__ = ("chat_id", "name", "lines", "channel", "count",
                 "plain", "agg", "lock", "close_task", "edit_task")

    def __init__(self, chat_id: int, name: str) -> None:
        self.chat_id = chat_id
        self.name = name
        self.lines: list[str] = []   # компактные строки для цитаты
        self.channel = ""            # где пользователь сейчас
        self.count = 0               # людей в текущем канале
        self.plain: list[dict] = []  # хэндлы одиночных сообщений (до схлопывания)
        self.agg: dict | None = None  # хэндл живого сообщения
        self.lock = asyncio.Lock()
        self.close_task: asyncio.Task | None = None
        self.edit_task: asyncio.Task | None = None


class VoiceFloodAggregator:
    def __init__(self, side, *, keep: int = 2, silence: float = 60.0,
                 edit_debounce: float = 1.2) -> None:
        self.side = side                 # DiscordSide: читаем tg_bot/vk_api/links лениво
        self.keep = max(0, keep)
        self.silence = silence
        self.edit_debounce = edit_debounce
        self._streams: dict[tuple[int, int], _Stream] = {}

    async def add(self, tg_chat_id: int, member_id: int, name: str,
                  full_text: str, line: str, channel_name: str, count: int) -> None:
        """Обработать одно голосовое событие пользователя.

        full_text — точный текст обычного (несхлопнутого) сообщения, как раньше.
        line — компактная строка для цитаты живого сообщения (без имени).
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
                st.lines.append(line)
                self._arm_close(key, st)

                n = len(st.lines)
                if st.agg is None and n <= self.keep:
                    # Спокойный режим: обычное сообщение, как прежде.
                    h = await send_pair(self.side, tg_chat_id, full_text, full_text)
                    st.plain.append(h)
                elif st.agg is None:
                    # Порог превышен — сносим одиночные, поднимаем живое сообщение.
                    for h in st.plain:
                        await delete_pair(self.side, h)
                    st.plain.clear()
                    tg_text, vk_text = self._render(st)
                    st.agg = await send_pair(self.side, tg_chat_id, tg_text,
                                             vk_text, tg_html=True)
                else:
                    # Живое сообщение уже есть — дебаунсим правку.
                    self._arm_edit(key, st)
                return

    # ---------- рендер ----------
    def _render(self, st: _Stream) -> tuple[str, str]:
        shown = st.lines[-_MAX_LINES:]
        hidden = len(st.lines) - len(shown)
        note = f"…ещё {hidden} действий выше\n" if hidden else ""

        head_tg = (f"🎧 <b>{html.escape(st.name)}</b> — сейчас в "
                   f"«{html.escape(st.channel)}» ({st.count})")
        body_tg = html.escape(note + "\n".join(shown))
        tg = f"{head_tg}\n<blockquote expandable>{body_tg}</blockquote>"

        head_vk = f"🎧 {st.name} — сейчас в «{st.channel}» ({st.count})"
        vk = f"{head_vk}\n{note}" + "\n".join(shown)
        return tg, vk

    # ---------- таймер тишины (закрытие окна) ----------
    def _arm_close(self, key, st: _Stream) -> None:
        if st.close_task:
            st.close_task.cancel()
        st.close_task = asyncio.create_task(self._close_later(key))

    async def _close_later(self, key) -> None:
        try:
            await asyncio.sleep(self.silence)
        except asyncio.CancelledError:
            return
        st = self._streams.get(key)
        if st is None:
            return
        async with st.lock:
            if self._streams.get(key) is not st:
                return
            if st.edit_task:
                st.edit_task.cancel()
                st.edit_task = None
            if st.agg is not None:  # финальная правка со свежим состоянием
                tg_text, vk_text = self._render(st)
                await edit_pair(self.side, st.agg, tg_text, vk_text, tg_html=True)
            self._streams.pop(key, None)

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
