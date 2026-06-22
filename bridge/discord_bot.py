"""Standalone Discord-бот для отслеживания голосовых каналов.

Не мост: просто следит за войсами на сервере и пишет в текстовый канал, когда
кто-то заходит / выходит / переходит между каналами. Команда /vc рисует красивую
карточку (Pillow) со всеми, кто сейчас в голосовых: канал, аватар, игра, время.

Включается опционально: если в .env нет DISCORD_TOKEN — модуль не запускается,
и TG<->VK мост работает как обычно.
"""

import asyncio
import io
import logging
import re
import sqlite3
import time
from functools import lru_cache

import discord
from discord import app_commands
from PIL import Image, ImageDraw, ImageFont

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


# Эмодзи и пиктограммы, которых нет в DejaVuSans — вырезаем перед отрисовкой,
# иначе Pillow рисует «квадратик» (tofu).
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
        base = Image.new("RGBA", (size, size), color + (255,))
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


class DiscordStore:
    """Реестр привязок «сервер -> текстовый канал» для уведомлений о войсах.

    Привязка: голосовые события сервера guild_id шлются в канал channel_id.
    Поддерживается много привязок (канал может слушать несколько серверов,
    сервер может слаться в несколько каналов). Лежит в том же links.db.
    """

    def __init__(self, path: str = "links.db") -> None:
        self._db = sqlite3.connect(path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS ds_bindings ("
            "guild_id INTEGER, channel_id INTEGER, title TEXT, "
            "PRIMARY KEY (guild_id, channel_id))"
        )
        self._db.commit()

    def add(self, guild_id: int, channel_id: int, title: str = "") -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO ds_bindings(guild_id, channel_id, title) "
            "VALUES (?, ?, ?)", (guild_id, channel_id, title))
        self._db.commit()

    def remove(self, guild_id: int, channel_id: int) -> bool:
        cur = self._db.execute(
            "DELETE FROM ds_bindings WHERE guild_id=? AND channel_id=?",
            (guild_id, channel_id))
        self._db.commit()
        return cur.rowcount > 0

    def channels_for_guild(self, guild_id: int) -> list[int]:
        return [r[0] for r in self._db.execute(
            "SELECT channel_id FROM ds_bindings WHERE guild_id=?",
            (guild_id,)).fetchall()]

    def guilds_for_channel(self, channel_id: int) -> set[int]:
        return {r[0] for r in self._db.execute(
            "SELECT guild_id FROM ds_bindings WHERE channel_id=?",
            (channel_id,)).fetchall()}

    def bindings_for_channel(self, channel_id: int) -> list[tuple[int, str]]:
        return [(r[0], r[1] or "") for r in self._db.execute(
            "SELECT guild_id, title FROM ds_bindings WHERE channel_id=? ORDER BY rowid",
            (channel_id,)).fetchall()]


def _is_admin(interaction: discord.Interaction) -> bool:
    """Только админ сервера (или с правом «Управление сервером»)."""
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and (perms.administrator or perms.manage_guild))


class _ConnectSelect(discord.ui.Select):
    def __init__(self, store: DiscordStore, channel_id: int, guilds: list) -> None:
        options = [discord.SelectOption(label=g.name[:100], value=str(g.id))
                   for g in guilds]
        super().__init__(placeholder="Выберите сервер…", min_values=1,
                         max_values=1, options=options)
        self._store = store
        self._channel_id = channel_id
        self._guilds = {g.id: g for g in guilds}

    async def callback(self, interaction: discord.Interaction) -> None:
        gid = int(self.values[0])
        g = self._guilds.get(gid)
        self._store.add(gid, self._channel_id, g.name if g else "")
        await interaction.response.edit_message(
            content=f"✅ Готово: голосовые события сервера **{g.name if g else gid}** "
                    f"теперь приходят в этот канал.", view=None)


class _DisconnectSelect(discord.ui.Select):
    def __init__(self, store: DiscordStore, client: discord.Client,
                 channel_id: int, bindings: list[tuple[int, str]]) -> None:
        options = []
        for gid, title in bindings:
            g = client.get_guild(gid)
            label = (title or (g.name if g else str(gid)))[:100]
            options.append(discord.SelectOption(label=label, value=str(gid)))
        super().__init__(placeholder="Что отвязать…", min_values=1,
                         max_values=1, options=options)
        self._store = store

    async def callback(self, interaction: discord.Interaction) -> None:
        gid = int(self.values[0])
        self._store.remove(gid, interaction.channel_id)
        await interaction.response.edit_message(
            content="✅ Отвязано — уведомления этого сервера сюда больше не идут.",
            view=None)


class _PickView(discord.ui.View):
    def __init__(self, item: discord.ui.Select, timeout: float = 120) -> None:
        super().__init__(timeout=timeout)
        self.add_item(item)


class DiscordSide:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.store = DiscordStore()
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True        # привилегированный: Server Members
        intents.voice_states = True
        intents.presences = True      # привилегированный: Presence (во что играет)
        self.client = discord.Client(intents=intents)
        self.tree = app_commands.CommandTree(self.client)
        # время захода в текущий войс: {member_id: unix_ts}
        self._since: dict[int, float] = {}
        self._register()

    # ---------- запуск ----------
    async def start(self) -> None:
        await self.client.start(self.cfg.discord_token)

    # ---------- регистрация событий и команд ----------
    def _register(self) -> None:
        client = self.client

        @client.event
        async def on_ready() -> None:
            now = time.time()
            # «засеять» тех, кто уже сидит в войсах на момент старта
            for guild in client.guilds:
                for ch in guild.voice_channels:
                    for m in ch.members:
                        self._since.setdefault(m.id, now)
            try:
                if self.cfg.discord_guild_id:
                    g = discord.Object(id=self.cfg.discord_guild_id)
                    self.tree.copy_global_to(guild=g)
                    await self.tree.sync(guild=g)
                else:
                    await self.tree.sync()
            except Exception:
                log.exception("Не удалось синхронизировать slash-команды")
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
                await self._announce(member, "join", ach)
            elif bch is not None and ach is None:
                self._since.pop(member.id, None)
                await self._announce(member, "leave", bch)
            elif bch and ach and bch.id != ach.id:
                self._since[member.id] = now
                await self._announce(member, "move", ach, bch)
            # иначе — mute/deafen/stream: игнорируем

        @self.tree.command(name="vc", description="Кто сейчас в голосовых каналах")
        async def vc(interaction: discord.Interaction) -> None:
            if interaction.guild is None:
                await interaction.response.send_message(
                    "Команда работает только на сервере.", ephemeral=True)
                return
            await interaction.response.defer(thinking=True)
            file = await self.render_vc(interaction.guild)
            if file is None:
                await interaction.followup.send("Сейчас в голосовых каналах никого нет. 🔇")
            else:
                await interaction.followup.send(file=file)

        @self.tree.command(
            name="ds-connect",
            description="Привязать сервер: его голосовые события будут идти в этот канал")
        async def ds_connect(interaction: discord.Interaction) -> None:
            if interaction.guild is None or not _is_admin(interaction):
                await interaction.response.send_message(
                    "Команда только для администраторов сервера.", ephemeral=True)
                return
            already = self.store.guilds_for_channel(interaction.channel_id)
            options = [g for g in self._suitable_guilds(interaction.user.id)
                       if g.id not in already][:25]
            if not options:
                await interaction.response.send_message(
                    "Нет доступных серверов для привязки "
                    "(нужны права админа на сервере, где есть бот; "
                    "возможно, все уже привязаны к этому каналу).", ephemeral=True)
                return
            view = _PickView(_ConnectSelect(self.store, interaction.channel_id, options))
            await interaction.response.send_message(
                "Выберите сервер, чьи голосовые события слать в **этот** канал:",
                view=view, ephemeral=True)

        @self.tree.command(
            name="ds-disconnect",
            description="Отвязать сервер от этого канала")
        async def ds_disconnect(interaction: discord.Interaction) -> None:
            if interaction.guild is None or not _is_admin(interaction):
                await interaction.response.send_message(
                    "Команда только для администраторов сервера.", ephemeral=True)
                return
            bindings = self.store.bindings_for_channel(interaction.channel_id)
            if not bindings:
                await interaction.response.send_message(
                    "К этому каналу ничего не привязано.", ephemeral=True)
                return
            view = _PickView(_DisconnectSelect(
                self.store, self.client, interaction.channel_id, bindings))
            await interaction.response.send_message(
                "Выберите, какой сервер отвязать от этого канала:",
                view=view, ephemeral=True)

    # ---------- подходящие серверы (для /ds-connect) ----------
    def _suitable_guilds(self, user_id: int) -> list:
        """Серверы, где есть бот и где пользователь — администратор."""
        out = []
        for g in self.client.guilds:
            m = g.get_member(user_id)
            if m and (m.guild_permissions.administrator
                      or m.guild_permissions.manage_guild):
                out.append(g)
        return out

    # ---------- объявления о войсах ----------
    def _destinations(self, guild) -> list:
        """Каналы, куда слать голосовые события данного сервера."""
        cids = set(self.store.channels_for_guild(guild.id))
        env = self.cfg.discord_notify_channel_id
        if env:
            ech = self.client.get_channel(env)
            # статичный канал из .env получает события только своего сервера
            if ech is not None and getattr(ech, "guild", None) and ech.guild.id == guild.id:
                cids.add(env)
        result = []
        for cid in cids:
            ch = self.client.get_channel(cid)
            if ch is not None:
                result.append(ch)
        return result

    @staticmethod
    def _humans(channel) -> int:
        return sum(1 for m in channel.members if not m.bot)

    async def _announce(self, member, kind: str, channel, src=None) -> None:
        destinations = self._destinations(member.guild)
        if not destinations:
            return
        n = self._humans(channel)
        name = f"**{member.display_name}**"
        if kind == "join":
            text = (f"🔊 {name} зашёл в **{channel.name}**. Сейчас в канале: **{n}**. "
                    f"Напишите `/vc` для полного списка.")
        elif kind == "leave":
            text = f"👋 {name} вышел из **{channel.name}**. Осталось в канале: **{n}**."
        else:  # move
            text = (f"🔁 {name}: **{src.name}** → **{channel.name}**. "
                    f"В «{channel.name}»: **{n}**.")
        for ch in destinations:
            try:
                await ch.send(text)
            except discord.HTTPException:
                log.exception("Не удалось отправить уведомление в канал %s",
                              getattr(ch, "id", "?"))

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

    # ---------- рендер карточки /vc ----------
    async def render_vc(self, guild) -> discord.File | None:
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

        png = await asyncio.to_thread(self._draw, guild.name, sections)
        return discord.File(io.BytesIO(png), filename="voice.png")

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
