import sqlite3


class LinkStore:
    """Хранилище связей сообщений и пар чатов TG <-> VK (несколько пар).

    id сообщений уникальны лишь внутри своего чата/беседы, поэтому ключи
    составные: (tg_chat_id, tg_msg_id) <-> (vk_peer_id, vk_cmid).

    Пары чатов (1:1): один TG-чат связан с одной VK-беседой.
    Всё в одном SQLite-файле, переживает перезапуск.
    """

    def __init__(self, path: str = "links.db", trim_keep: int = 100000):
        self._db = sqlite3.connect(path)
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
        # Сохранённые видео («альты», команда /alt): храним лишь координаты
        # исходного сообщения (src_chat_id, src_msg_id), чтобы отдавать видео
        # через copy_message и не качать файл на сервер. Альты — на каждый чат.
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS alts ("
            "id INTEGER PRIMARY KEY, "
            "tg_chat_id INTEGER, name TEXT, "
            "src_chat_id INTEGER, src_msg_id INTEGER, "
            "created_by INTEGER)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS i_alts_chat ON alts(tg_chat_id)"
        )
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

    # --- сохранённые видео («альты», /alt) ----------------------------------
    # Сравнение имён регистронезависимое и работает с кириллицей, поэтому
    # совпадение/поиск делаем в Python через casefold (SQLite NOCASE/LIKE
    # умеют только ASCII). Альтов на чат немного — это дёшево.

    def _alt_rows(self, tg_chat_id: int) -> list[tuple]:
        """Сырые строки альтов чата: [(id, name, src_chat_id, src_msg_id), ...]."""
        return self._db.execute(
            "SELECT id, name, src_chat_id, src_msg_id FROM alts "
            "WHERE tg_chat_id=? ORDER BY rowid", (tg_chat_id,)).fetchall()

    def add_alt(self, tg_chat_id: int, name: str, src_chat_id: int,
                src_msg_id: int, created_by: int) -> bool:
        """Сохранить альт. False, если имя в этом чате уже занято."""
        if self.get_alt(tg_chat_id, name) is not None:
            return False
        self._db.execute(
            "INSERT INTO alts(tg_chat_id, name, src_chat_id, src_msg_id, created_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (tg_chat_id, name, src_chat_id, src_msg_id, created_by))
        self._db.commit()
        return True

    def get_alt(self, tg_chat_id: int, name: str) -> tuple | None:
        """Найти альт по имени (регистронезависимо): (id, name, src_chat_id, src_msg_id)."""
        key = name.casefold()
        for r in self._alt_rows(tg_chat_id):
            if (r[1] or "").casefold() == key:
                return r
        return None

    def get_alt_by_id(self, alt_id: int) -> tuple | None:
        """Альт по id: (id, name, src_chat_id, src_msg_id, tg_chat_id)."""
        return self._db.execute(
            "SELECT id, name, src_chat_id, src_msg_id, tg_chat_id FROM alts "
            "WHERE id=?", (alt_id,)).fetchone()

    def list_alts(self, tg_chat_id: int) -> list[tuple[int, str]]:
        """Все альты чата: [(id, name), ...]."""
        return [(r[0], r[1] or "") for r in self._alt_rows(tg_chat_id)]

    def search_alts(self, tg_chat_id: int, query: str) -> list[tuple[int, str]]:
        """Альты чата, чьё имя содержит query (регистронезависимо)."""
        q = query.casefold()
        return [(r[0], r[1] or "") for r in self._alt_rows(tg_chat_id)
                if q in (r[1] or "").casefold()]

    def delete_alt(self, tg_chat_id: int, name: str) -> bool:
        """Удалить альт по имени. False, если не нашли."""
        row = self.get_alt(tg_chat_id, name)
        if not row:
            return False
        self._db.execute("DELETE FROM alts WHERE id=?", (row[0],))
        self._db.commit()
        return True
