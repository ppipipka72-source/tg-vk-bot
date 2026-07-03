"""Discord-наблюдатель за голосовыми каналами (часть моста TG↔VK).

Бот молча сидит в Discord и следит за войсами. Когда кто-то заходит/выходит/
переходит — шлёт уведомление в привязанные пары моста (Telegram + VK). Команда
/vc (вызывается в TG или VK) рисует Pillow-карточку со всеми, кто в голосовых,
и отправляет её в оба чата.

Управление привязками (/ds_connect, /ds_disconnect) живёт в Telegram (см. tg_bot).
Здесь — только наблюдение, рендер карточки и доставка через мост.

Включается опционально: нет DISCORD_TOKEN -> не запускается, мост работает как обычно.
"""

import asyncio
import io
import logging
import re
import time
from functools import lru_cache

import discord
from PIL import Image, ImageDraw, ImageFont

from .discord_flood import VoiceFloodAggregator
from .discord_relay import deliver_photo, deliver_text

log = logging.getLogger("discord_bot")

# --- Палитра в стиле Discord (тёмная тема) ---
DARK_BG = (30, 31, 34)
PANEL = (43, 45, 49)
TEXT = (242, 243, 245)
MUTED = (148, 155, 164)
GREEN = (35, 165, 90)
GAME_CLR = (87, 242, 135)

# --- Поиск шрифта с поддержкой кириллицы (кроссплатформенно) ---
_REG_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "C:\\Windows\\Fonts\\segoeui.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
]
_BOLD_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "C:\\Windows\\Fonts\\segoeuib.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
]

# Эмодзи/пиктограммы, которых нет в DejaVuSans — вырезаем перед отрисовкой
# КАРТИНКИ (в тексте сообщений TG/VK эмодзи оставляем — там они рендерятся).
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF\U00002300-\U000023FF\U0001F1E6-\U0001F1FF"
    "︀-️‍⃣™ℹ]+"
)


def _safe(text: str) -> str:
    cleaned = _EMOJI_RE.sub("", text or "").strip()
    return cleaned or "—"


@lru_cache(maxsize=32)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for path in (_BOLD_PATHS if bold else _REG_PATHS):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _make_avatar(data: bytes | None, size: int, name: str, color: tuple) -> Image.Image:
    """Круглый аватар. Если картинки нет — кружок с первой буквой имени."""
    base = None
    if data:
        try:
            base = Image.open(io.BytesIO(data)).convert("RGBA").resize((size, size))
        except Exception:
            base = None
    if base is None:
        base = Image.new("RGBA", (size, size), tuple(color) + (255,))
        dd = ImageDraw.Draw(base)
        initial = (name[:1] or "?").upper()
        ff = _font(size // 2, bold=True)
        w = dd.textlength(initial, font=ff)
        dd.text(((size - w) / 2, size / 4), initial, font=ff, fill=(255, 255, 255))

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(base, (0, 0), mask)
    return out


def _fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return "только что"
    mins = seconds // 60
    if mins < 60:
        return f"{mins} мин"
    hours, rem = divmod(mins, 60)
    return f"{hours} ч {rem} мин" if rem else f"{hours} ч"


class DiscordSide:
    def __init__(self, cfg, links) -> None:
        self.cfg = cfg
        self.links = links
        # Ссылки на мост (выставляются в main):
        self.tg_bot = None
        self.vk_api = None
        self.vk_token = ""

        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True        # привилегированный: Server Members
        intents.voice_states = True
        intents.presences = True      # привилегированный: Presence (во что играет)
        self.client = discord.Client(intents=intents)
        # время захода в текущий войс: {member_id: unix_ts}
        self._since: dict[int, float] = {}
        # Схлопывание флуда голосовых событий (по пользователю). Отключаемо.
        self.flood = (
            VoiceFloodAggregator(self, silence=cfg.discord_flood_silence)
            if cfg.discord_flood_collapse else None
        )
        self._register()

    # ---------- запуск ----------
    async def start(self) -> None:
        await self.client.start(self.cfg.discord_token)

    @property
    def ready(self) -> bool:
        return self.client.is_ready()

    # ---------- инфо о серверах (для /ds_connect в Telegram) ----------
    def list_guilds(self) -> list[tuple[int, str]]:
        return [(g.id, g.name) for g in self.client.guilds]

    def guild_name(self, guild_id: int) -> str | None:
        g = self.client.get_guild(guild_id)
        return g.name if g else None

    # ---------- события Discord ----------
    def _register(self) -> None:
        client = self.client

        @client.event
        async def on_ready() -> None:
            now = time.time()
            for guild in client.guilds:
                for ch in guild.voice_channels:
                    for m in ch.members:
                        self._since.setdefault(m.id, now)
            log.info("Discord-бот вошёл как %s (серверов: %d)",
                     client.user, len(client.guilds))

        @client.event
        async def on_voice_state_update(member, before, after) -> None:
            if member.bot:
                return
            bch, ach = before.channel, after.channel
            now = time.time()
            if bch is None and ach is not None:
                self._since[member.id] = now
                await self._on_voice(member, "join", ach)
            elif bch is not None and ach is None:
                self._since.pop(member.id, None)
                await self._on_voice(member, "leave", bch)
            elif bch and ach and bch.id != ach.id:
                self._since[member.id] = now
                await self._on_voice(member, "move", ach, bch)
            # иначе — mute/deafen/stream: игнорируем

    @staticmethod
    def _humans(channel) -> int:
        return sum(1 for m in channel.members if not m.bot)

    async def _on_voice(self, member, kind: str, channel, src=None) -> None:
        chats = self.links.ds_tg_chats_for_guild(member.guild.id)
        if not chats:
            return
        n = self._humans(channel)
        nm = member.display_name  # эмодзи в TG/VK рендерятся — не вырезаем
        cn = channel.name
        if kind == "join":
            text = (f"🔊 {nm} зашёл в «{cn}». Сейчас в канале: {n}. "
                    f"Напишите /vc для полного списка.")
            line = f"🔊 зашёл в «{cn}» — {n} в канале"
            here = True
        elif kind == "leave":
            text = f"👋 {nm} вышел из «{cn}». Осталось в канале: {n}."
            line = f"👋 вышел из «{cn}» — осталось {n}"
            here = False
        else:  # move
            text = f"🔁 {nm}: «{src.name}» → «{cn}». Сейчас в «{cn}»: {n}."
            line = f"🔁 «{src.name}» → «{cn}» — {n} в канале"
            here = True
        for tg_chat_id in chats:
            if self.flood is not None:
                await self.flood.add(tg_chat_id, member.id, nm, line, cn, n, here)
            else:
                await deliver_text(self.tg_bot, self.vk_api, self.vk_token,
                                   self.links, tg_chat_id, text)

    # ---------- активность (во что играет) ----------
    @staticmethod
    def _game_of(member) -> str | None:
        for act in member.activities:
            if act.type == discord.ActivityType.playing:
                return act.name
        return None

    def _dur_str(self, member_id: int) -> str:
        ts = self._since.get(member_id)
        if not ts:
            return ""
        return _fmt_duration(int(time.time() - ts))

    # ---------- /vc: рендер карточки и доставка в пару ----------
    async def handle_vc(self, tg_chat_id: int) -> int:
        """Отрисовать привязанные серверы и отправить в TG+VK. Возвращает число
        привязанных серверов (0 -> у этого чата нет /ds_connect)."""
        guilds = self.links.ds_guilds_for_tg(tg_chat_id)
        for guild_id, _title in guilds:
            png = await self.render_vc(guild_id)
            gname = self.guild_name(guild_id) or "сервер"
            if png is None:
                await deliver_text(self.tg_bot, self.vk_api, self.vk_token,
                                   self.links, tg_chat_id,
                                   f"🔇 {gname}: в голосовых каналах сейчас никого.")
            else:
                await deliver_photo(self.tg_bot, self.vk_api, self.vk_token,
                                    self.links, tg_chat_id, png,
                                    f"🎙 Голосовые — {gname}")
        return len(guilds)

    async def render_vc(self, guild_id: int) -> bytes | None:
        guild = self.client.get_guild(guild_id)
        if guild is None:
            return None
        sections = []
        for ch in guild.voice_channels:
            members = [m for m in ch.members if not m.bot]
            if not members:
                continue
            rows = []
            for m in members:
                try:
                    avatar_bytes = await m.display_avatar.with_size(64).read()
                except Exception:
                    avatar_bytes = None
                color = (m.color.r, m.color.g, m.color.b) if m.color.value else MUTED
                rows.append({
                    "name": m.display_name,
                    "avatar": avatar_bytes,
                    "color": color,
                    "game": self._game_of(m),
                    "dur": self._dur_str(m.id),
                })
            sections.append({"name": ch.name, "count": len(members), "rows": rows})

        if not sections:
            return None
        return await asyncio.to_thread(self._draw, guild.name, sections)

    def _draw(self, guild_name: str, sections: list) -> bytes:
        pad, width, av = 28, 760, 44
        row_h, head_h, title_h, sec_gap = 60, 46, 60, 18

        height = pad + title_h
        for s in sections:
            height += head_h + len(s["rows"]) * row_h + sec_gap
        height += pad - sec_gap

        img = Image.new("RGB", (width, height), DARK_BG)
        d = ImageDraw.Draw(img)
        f_title = _font(28, bold=True)
        f_head = _font(20, bold=True)
        f_name = _font(17, bold=True)
        f_sub = _font(14)
        f_dur = _font(14)

        y = pad
        d.text((pad, y), "Голосовые каналы", font=f_title, fill=TEXT)
        d.text((pad, y + 34), _safe(guild_name), font=f_sub, fill=MUTED)
        y += title_h

        for s in sections:
            cy = y + head_h // 2
            d.ellipse((pad, cy - 5, pad + 10, cy + 5), fill=GREEN)
            d.text((pad + 24, y + 11), _safe(s["name"]), font=f_head, fill=TEXT)
            badge = f'{s["count"]} в канале'
            d.text((width - pad - d.textlength(badge, font=f_sub), y + 16),
                   badge, font=f_sub, fill=MUTED)
            y += head_h

            ph = len(s["rows"]) * row_h
            d.rounded_rectangle((pad, y, width - pad, y + ph), radius=12, fill=PANEL)
            for r in s["rows"]:
                safe_name = _safe(r["name"])
                avatar = _make_avatar(r["avatar"], av, safe_name, r["color"])
                ax, ay = pad + 12, y + (row_h - av) // 2
                img.paste(avatar, (ax, ay), avatar)
                tx = ax + av + 14
                d.text((tx, y + 11), safe_name, font=f_name, fill=TEXT)
                if r["game"]:
                    sub, clr = f'в игре: {_safe(r["game"])}', GAME_CLR
                else:
                    sub, clr = "в голосовом", MUTED
                d.text((tx, y + 34), sub, font=f_sub, fill=clr)
                if r["dur"]:
                    d.text((width - pad - 16 - d.textlength(r["dur"], font=f_dur),
                            y + row_h // 2 - 8), r["dur"], font=f_dur, fill=MUTED)
                y += row_h
            y += sec_gap

        bio = io.BytesIO()
        img.save(bio, "PNG")
        return bio.getvalue()
