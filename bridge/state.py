import sqlite3
import time


class LinkStore:
    """Хранилище связей сообщений и пар чатов TG <-> VK (несколько пар).

    id сообщений уникальны лишь внутри своего чата/беседы, поэтому ключи
    составные: (tg_chat_id, tg_msg_id) <-> (vk_peer_id, vk_cmid).

    Пары чатов (1:1): один TG-чат связан с одной VK-беседой.
    Всё в одном SQLite-файле, переживает перезапуск.
    """

    def __init__(self, path: str = "links.db", trim_keep: int = 100000):
        self._db = sqlite3.connect(path)
        # Row-доступ по имени колонки (для альтов); индексный доступ r[0] тоже
        # продолжает работать, так что остальной код не ломается.
        self._db.row_factory = sqlite3.Row
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS msg_links ("
            "tg_chat_id INTEGER, tg_msg_id INTEGER, "
            "vk_peer_id INTEGER, vk_cmid INTEGER, "
            "PRIMARY KEY (tg_chat_id, tg_msg_id))"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS i_vk ON msg_links(vk_peer_id, vk_cmid)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS pairs ("
            "tg_chat_id INTEGER PRIMARY KEY, "
            "vk_peer_id INTEGER UNIQUE, "
            "title TEXT)"
        )
        # Беседы VK, из которых бот видел сообщения (для выбора в /link):
        # community-токен не умеет перечислять беседы, поэтому копим их сами.
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS seen_vk ("
            "peer_id INTEGER PRIMARY KEY, title TEXT)"
        )
        # Привязки Discord-сервер -> TG-чат (а VK берётся из пары pairs).
        # Голосовые события сервера льются в этот TG-чат и его VK-беседу.
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS ds_bindings ("
            "guild_id INTEGER, tg_chat_id INTEGER, title TEXT, "
            "PRIMARY KEY (guild_id, tg_chat_id))"
        )
        # Участники TG-чатов (для @all): Bot API не умеет перечислять всех
        # участников, поэтому копим тех, кто писал в чат.
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS chat_members ("
            "tg_chat_id INTEGER, user_id INTEGER, "
            "username TEXT, full_name TEXT, "
            "PRIMARY KEY (tg_chat_id, user_id))"
        )
        # Персональные префиксы участников (команда /prefix). Ключ —
        # (платформа, user_id): tg-пользователь и vk-пользователь независимы.
        # Префикс подставляется перед именем в заголовке пересланного
        # сообщения: «[VK] 🕋Ирина Седойкина:».
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS user_prefixes ("
            "platform TEXT, user_id INTEGER, prefix TEXT, "
            "PRIMARY KEY (platform, user_id))"
        )
        # Сохранённые видео («альты», команда /alt). Альты — на каждый чат
        # (ключ — tg_chat_id, он же определяет VK-беседу через pairs).
        # Кросс-платформенная выдача: храним хэндл на КАЖДОЙ платформе —
        #   tg_file_id    — file_id видео в Telegram (мгновенная выдача в TG);
        #   vk_attachment — строка video{owner}_{id}_{key} в VK (выдача в VK).
        # Недостающую сторону достраиваем лениво при первом запросе и кешируем.
        # origin — где сохранён изначально; kind — тип TG-медиа для отправки.
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS alts ("
            "id INTEGER PRIMARY KEY, "
            "tg_chat_id INTEGER, name TEXT, "
            "origin TEXT, kind TEXT, "
            "tg_file_id TEXT, vk_attachment TEXT, "
            "src_chat_id INTEGER, src_msg_id INTEGER, "
            "created_by INTEGER, created_at INTEGER, "
            "thumb TEXT)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS i_alts_chat ON alts(tg_chat_id)"
        )
        # Миграция со старой схемы (без кросс-платформенных колонок и даты).
        have = {r[1] for r in self._db.execute("PRAGMA table_info(alts)").fetchall()}
        # thumb — file_id миниатюры в TG / url превью в VK (для веб-списка /alt web).
        for col in ("origin", "kind", "tg_file_id", "vk_attachment", "thumb"):
            if col not in have:
                self._db.execute(f"ALTER TABLE alts ADD COLUMN {col} TEXT")
        if "created_at" not in have:
            self._db.execute("ALTER TABLE alts ADD COLUMN created_at INTEGER")
        self._db.commit()
        self._seen_cache: set[int] = set()
        self._trim_keep = trim_keep
        self._since_trim = 0

    # --- связи сообщений (для нативных reply) -------------------------------

    def link(self, tg_chat_id, tg_msg_id, vk_peer_id, vk_cmid) -> None:
        if not (tg_chat_id and tg_msg_id and vk_peer_id and vk_cmid):
            return
        self._db.execute(
            "INSERT OR REPLACE INTO msg_links"
            "(tg_chat_id, tg_msg_id, vk_peer_id, vk_cmid) VALUES (?, ?, ?, ?)",
            (tg_chat_id, tg_msg_id, vk_peer_id, vk_cmid),
        )
        self._db.commit()
        self._since_trim += 1
        if self._since_trim >= 1000:
            self._trim()

    def vk_for_tg(self, tg_chat_id, tg_msg_id) -> int | None:
        if not (tg_chat_id and tg_msg_id):
            return None
        row = self._db.execute(
            "SELECT vk_cmid FROM msg_links WHERE tg_chat_id=? AND tg_msg_id=?",
            (tg_chat_id, tg_msg_id),
        ).fetchone()
        return row[0] if row else None

    def tg_for_vk(self, vk_peer_id, vk_cmid) -> int | None:
        if not (vk_peer_id and vk_cmid):
            return None
        row = self._db.execute(
            "SELECT tg_msg_id FROM msg_links WHERE vk_peer_id=? AND vk_cmid=? "
            "ORDER BY rowid LIMIT 1",
            (vk_peer_id, vk_cmid),
        ).fetchone()
        return row[0] if row else None

    def _trim(self) -> None:
        self._db.execute(
            "DELETE FROM msg_links WHERE rowid < "
            "(SELECT MAX(rowid) - ? FROM msg_links)",
            (self._trim_keep,),
        )
        self._db.commit()
        self._since_trim = 0

    # --- пары чатов ----------------------------------------------------------

    def add_pair(self, tg_chat_id: int, vk_peer_id: int, title: str = "") -> None:
        # Чат/беседа могут участвовать только в одной паре: чистим возможные конфликты.
        self._db.execute("DELETE FROM pairs WHERE tg_chat_id=? OR vk_peer_id=?",
                         (tg_chat_id, vk_peer_id))
        self._db.execute(
            "INSERT INTO pairs(tg_chat_id, vk_peer_id, title) VALUES (?, ?, ?)",
            (tg_chat_id, vk_peer_id, title),
        )
        self._db.commit()

    def remove_pair_by_tg(self, tg_chat_id: int) -> bool:
        cur = self._db.execute("DELETE FROM pairs WHERE tg_chat_id=?", (tg_chat_id,))
        self._db.commit()
        return cur.rowcount > 0

    def vk_peer_for_tg_chat(self, tg_chat_id: int) -> int | None:
        row = self._db.execute(
            "SELECT vk_peer_id FROM pairs WHERE tg_chat_id=?", (tg_chat_id,)
        ).fetchone()
        return row[0] if row else None

    def tg_chat_for_vk_peer(self, vk_peer_id: int) -> int | None:
        row = self._db.execute(
            "SELECT tg_chat_id FROM pairs WHERE vk_peer_id=?", (vk_peer_id,)
        ).fetchone()
        return row[0] if row else None

    def all_pairs(self) -> list[tuple[int, int, str]]:
        return [
            (r[0], r[1], r[2] or "")
            for r in self._db.execute(
                "SELECT tg_chat_id, vk_peer_id, title FROM pairs ORDER BY rowid"
            ).fetchall()
        ]

    def paired_vk_peers(self) -> set[int]:
        return {r[0] for r in self._db.execute("SELECT vk_peer_id FROM pairs").fetchall()}

    # --- увиденные беседы VK (для /link) ------------------------------------

    def mark_seen_vk(self, peer_id: int, title: str | None = None) -> None:
        if not peer_id:
            return
        if title:
            self._db.execute(
                "INSERT INTO seen_vk(peer_id, title) VALUES (?, ?) "
                "ON CONFLICT(peer_id) DO UPDATE SET title=excluded.title",
                (peer_id, title))
            self._db.commit()
        elif peer_id not in self._seen_cache:
            self._db.execute("INSERT OR IGNORE INTO seen_vk(peer_id, title) "
                             "VALUES (?, NULL)", (peer_id,))
            self._db.commit()
        self._seen_cache.add(peer_id)

    def seen_vk_unpaired(self) -> list[tuple[int, str]]:
        paired = self.paired_vk_peers()
        rows = self._db.execute(
            "SELECT peer_id, title FROM seen_vk ORDER BY rowid").fetchall()
        return [(p, t or "") for p, t in rows if p not in paired]

    # --- привязки Discord-сервер <-> TG-чат --------------------------------

    def add_ds_binding(self, guild_id: int, tg_chat_id: int, title: str = "") -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO ds_bindings(guild_id, tg_chat_id, title) "
            "VALUES (?, ?, ?)", (guild_id, tg_chat_id, title))
        self._db.commit()

    def remove_ds_binding(self, guild_id: int, tg_chat_id: int) -> bool:
        cur = self._db.execute(
            "DELETE FROM ds_bindings WHERE guild_id=? AND tg_chat_id=?",
            (guild_id, tg_chat_id))
        self._db.commit()
        return cur.rowcount > 0

    def ds_guilds_for_tg(self, tg_chat_id: int) -> list[tuple[int, str]]:
        """Привязанные к TG-чату Discord-серверы: [(guild_id, title), ...]."""
        return [(r[0], r[1] or "") for r in self._db.execute(
            "SELECT guild_id, title FROM ds_bindings WHERE tg_chat_id=? ORDER BY rowid",
            (tg_chat_id,)).fetchall()]

    def ds_tg_chats_for_guild(self, guild_id: int) -> list[int]:
        """TG-чаты, куда (и в их VK-беседы) слать события сервера."""
        return [r[0] for r in self._db.execute(
            "SELECT tg_chat_id FROM ds_bindings WHERE guild_id=?",
            (guild_id,)).fetchall()]

    # --- участники TG-чатов (для @all) --------------------------------------

    def track_member(self, tg_chat_id: int, user_id: int,
                     username: str | None, full_name: str | None) -> None:
        """Запомнить (или обновить) участника, написавшего в чат."""
        if not (tg_chat_id and user_id):
            return
        self._db.execute(
            "INSERT INTO chat_members(tg_chat_id, user_id, username, full_name) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(tg_chat_id, user_id) DO UPDATE SET "
            "username=excluded.username, full_name=excluded.full_name",
            (tg_chat_id, user_id, username, full_name))
        self._db.commit()

    def chat_members(self, tg_chat_id: int) -> list[tuple[int, str, str]]:
        """Накопленные участники чата: [(user_id, username, full_name), ...]."""
        return [(r[0], r[1] or "", r[2] or "") for r in self._db.execute(
            "SELECT user_id, username, full_name FROM chat_members "
            "WHERE tg_chat_id=? ORDER BY rowid", (tg_chat_id,)).fetchall()]

    def forget_member(self, tg_chat_id: int, user_id: int) -> None:
        """Забыть участника (например, ушёл из чата)."""
        self._db.execute(
            "DELETE FROM chat_members WHERE tg_chat_id=? AND user_id=?",
            (tg_chat_id, user_id))
        self._db.commit()

    # --- персональные префиксы (/prefix) ------------------------------------

    def set_prefix(self, platform: str, user_id: int, prefix: str) -> None:
        """Задать префикс пользователю на платформе (tg/vk)."""
        if not (platform and user_id):
            return
        self._db.execute(
            "INSERT INTO user_prefixes(platform, user_id, prefix) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(platform, user_id) DO UPDATE SET prefix=excluded.prefix",
            (platform, user_id, prefix))
        self._db.commit()

    def clear_prefix(self, platform: str, user_id: int) -> None:
        """Убрать префикс пользователя."""
        if not (platform and user_id):
            return
        self._db.execute(
            "DELETE FROM user_prefixes WHERE platform=? AND user_id=?",
            (platform, user_id))
        self._db.commit()

    def get_prefix(self, platform: str, user_id: int) -> str:
        """Префикс пользователя (пустая строка, если не задан)."""
        if not (platform and user_id):
            return ""
        row = self._db.execute(
            "SELECT prefix FROM user_prefixes WHERE platform=? AND user_id=?",
            (platform, user_id)).fetchone()
        return (row[0] if row else "") or ""

    # --- сохранённые видео («альты», /alt) ----------------------------------
    # Сравнение имён регистронезависимое и работает с кириллицей, поэтому
    # совпадение/поиск делаем в Python через casefold (SQLite NOCASE/LIKE
    # умеют только ASCII). Альтов на чат немного — это дёшево.
    # Строки возвращаются как sqlite3.Row: поля доступны по имени (r["name"]).

    def add_alt(self, tg_chat_id: int, name: str, *, origin: str,
                kind: str = "video", tg_file_id: str | None = None,
                vk_attachment: str | None = None, src_chat_id: int | None = None,
                src_msg_id: int | None = None, created_by: int = 0,
                created_at: int | None = None, thumb: str | None = None) -> int | None:
        """Сохранить альт. Возвращает id, либо None если имя в чате занято."""
        if self.get_alt(tg_chat_id, name) is not None:
            return None
        if created_at is None:
            created_at = int(time.time())
        cur = self._db.execute(
            "INSERT INTO alts(tg_chat_id, name, origin, kind, tg_file_id, "
            "vk_attachment, src_chat_id, src_msg_id, created_by, created_at, thumb) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tg_chat_id, name, origin, kind, tg_file_id, vk_attachment,
             src_chat_id, src_msg_id, created_by, created_at, thumb))
        self._db.commit()
        return cur.lastrowid

    def get_alt(self, tg_chat_id: int, name: str) -> sqlite3.Row | None:
        """Найти альт по имени (регистронезависимо). Полная строка или None."""
        key = name.casefold()
        rows = self._db.execute(
            "SELECT * FROM alts WHERE tg_chat_id=? ORDER BY rowid",
            (tg_chat_id,)).fetchall()
        for r in rows:
            if (r["name"] or "").casefold() == key:
                return r
        return None

    def get_alt_by_id(self, alt_id: int) -> sqlite3.Row | None:
        """Альт по id (полная строка) или None."""
        return self._db.execute(
            "SELECT * FROM alts WHERE id=?", (alt_id,)).fetchone()

    def list_alts(self, tg_chat_id: int) -> list[tuple[int, str]]:
        """Все альты чата: [(id, name), ...]."""
        return [(r["id"], r["name"] or "") for r in self._db.execute(
            "SELECT id, name FROM alts WHERE tg_chat_id=? ORDER BY rowid",
            (tg_chat_id,)).fetchall()]

    def web_alts(self, tg_chat_id: int) -> list[sqlite3.Row]:
        """Альты чата для веб-списка (/alt web): полные строки, новые сверху."""
        return self._db.execute(
            "SELECT id, name, origin, created_by, created_at, "
            "       (thumb IS NOT NULL AND thumb<>'') AS has_thumb "
            "FROM alts WHERE tg_chat_id=? ORDER BY created_at DESC, rowid DESC",
            (tg_chat_id,)).fetchall()

    def member_name(self, tg_chat_id: int, user_id: int) -> str | None:
        """Имя участника TG-чата (из накопленных в chat_members), иначе None."""
        row = self._db.execute(
            "SELECT full_name, username FROM chat_members "
            "WHERE tg_chat_id=? AND user_id=?", (tg_chat_id, user_id)).fetchone()
        if not row:
            return None
        return row["full_name"] or row["username"] or None

    def search_alts(self, tg_chat_id: int, query: str) -> list[tuple[int, str]]:
        """Альты чата, чьё имя содержит query (регистронезависимо)."""
        q = query.casefold()
        return [(r[0], r[1] or "") for r in self._db.execute(
            "SELECT id, name FROM alts WHERE tg_chat_id=? ORDER BY rowid",
            (tg_chat_id,)).fetchall() if q in (r[1] or "").casefold()]

    def delete_alt(self, tg_chat_id: int, name: str) -> bool:
        """Удалить альт по имени. False, если не нашли."""
        row = self.get_alt(tg_chat_id, name)
        if not row:
            return False
        self._db.execute("DELETE FROM alts WHERE id=?", (row["id"],))
        self._db.commit()
        return True

    def set_alt_tg(self, alt_id: int, tg_file_id: str, kind: str = "video") -> None:
        """Закешировать TG-хэндл (после ленивой конвертации VK→TG)."""
        self._db.execute("UPDATE alts SET tg_file_id=?, kind=? WHERE id=?",
                         (tg_file_id, kind, alt_id))
        self._db.commit()

    def set_alt_vk(self, alt_id: int, vk_attachment: str) -> None:
        """Закешировать VK-вложение (после ленивой конвертации TG→VK)."""
        self._db.execute("UPDATE alts SET vk_attachment=? WHERE id=?",
                         (vk_attachment, alt_id))
        self._db.commit()
