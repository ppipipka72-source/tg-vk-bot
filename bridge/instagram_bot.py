"""Instagram-наблюдатель (часть моста TG↔VK).

Логинится в личный аккаунт Instagram по sessionid-cookie (instagrapi) и опрашивает
директ-инбокс. Новые ВХОДЯЩИЕ сообщения льются в привязанную пару моста (Telegram +
связанная VK-беседа). Рилсы (и прочие видео из ссылок/шэров) скачиваются и уходят
нативным видео — так же, как если бы ссылку на рилс кинули прямо в чат.

Односторонне: из чата в Instagram ничего не отправляется.

Управление (/instagram add|connect|disconnect|list) живёт в Telegram (см. tg_bot).
Здесь — только логин, опрос инбокса и доставка в пару.

instagrapi синхронный → все его вызовы уносим в поток (asyncio.to_thread). Опрос
устойчив к сбоям: падение одного аккаунта не роняет мост; при ошибке логина/сессии
клиент выбрасывается из кеша и на следующем цикле перелогинивается.
"""

import asyncio
import html
import json
import logging
import random

from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from .discord_relay import deliver_text
from .video_dl import download_video, find_video_url
from .vk_upload import upload_doc
from .vk_user import upload_vk_video

log = logging.getLogger("instagram_bot")

try:
    from instagrapi import Client as _IGClient
except Exception:  # noqa: BLE001 — instagrapi опционален (без него фича выключена)
    _IGClient = None

_CAPTION_MAX = 180   # подпись к видео (лимит TG caption — 1024, но короче читабельнее)
_TEXT_MAX = 900      # текстовое сообщение в пару


def _rid() -> int:
    return random.getrandbits(31)


def parse_sessionid(raw: str) -> str | None:
    """Достать sessionid из присланного: JSON-массив куки (экспорт браузера),
    JSON-объект {"sessionid": ...} или голая строка sessionid."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.startswith("["):
        try:
            for c in json.loads(raw):
                if isinstance(c, dict) and c.get("name") == "sessionid":
                    return c.get("value")
        except (ValueError, TypeError):
            return None
        return None
    if raw.startswith("{"):
        try:
            return json.loads(raw).get("sessionid")
        except (ValueError, AttributeError):
            return None
    # Голый sessionid вида "16028593223%3A...%3A25%3A..." (или уже раскодированный).
    if "%3A" in raw or ":" in raw:
        return raw
    return None


async def probe_account(raw: str) -> tuple[int, str, str]:
    """Проверить cookie: залогиниться, вернуть (ig_user_id, username, cookies_json).

    cookies_json — нормализованный блоб {"sessionid", "settings"} для хранения в БД.
    Бросает исключение, если instagrapi нет, sessionid не найден или логин не удался.
    """
    if _IGClient is None:
        raise RuntimeError("instagrapi не установлен — Instagram недоступен.")
    sessionid = parse_sessionid(raw)
    if not sessionid:
        raise ValueError("Не нашёл sessionid в присланных данных.")

    def _work() -> tuple[int, str, str]:
        cl = _IGClient()
        cl.delay_range = [1, 3]
        cl.login_by_sessionid(sessionid)
        info = cl.account_info()
        settings = cl.get_settings()
        blob = json.dumps({"sessionid": sessionid, "settings": settings})
        return int(info.pk), info.username, blob

    return await asyncio.to_thread(_work)


class InstagramSide:
    def __init__(self, cfg, links) -> None:
        self.cfg = cfg
        self.links = links
        self.poll = max(15, int(getattr(cfg, "instagram_poll_seconds", 15) or 15))
        # Ссылки на мост (выставляются в main):
        self.tg_bot = None
        self.vk_api = None
        self.vk_token = ""
        self.vk_user_token = ""
        self.vk_group_id = 0
        # Кеш залогиненных клиентов по ig_user_id.
        self._clients: dict[int, object] = {}

    @property
    def available(self) -> bool:
        return _IGClient is not None

    def drop_client(self, ig_user_id: int) -> None:
        """Выбросить залогиненный клиент из кеша (после обновления cookie)."""
        self._clients.pop(ig_user_id, None)

    # ---------- запуск / цикл опроса ----------
    async def start(self) -> None:
        if _IGClient is None:
            log.warning("instagrapi не установлен — Instagram-наблюдатель выключен")
            return
        log.info("Instagram-наблюдатель запущен (опрос каждые %s с)", self.poll)
        while True:
            try:
                await self._poll_all()
            except Exception:  # noqa: BLE001 — цикл не должен умирать
                log.exception("Instagram: сбой цикла опроса")
            await asyncio.sleep(self.poll)

    async def _poll_all(self) -> None:
        for acc in self.links.bound_ig_accounts():
            ig_user_id = acc[0]
            try:
                await self._poll_account(acc)
            except Exception:  # noqa: BLE001 — сброс клиента → перелогин на след. цикле
                self._clients.pop(ig_user_id, None)
                log.exception("Instagram: сбой опроса аккаунта %s (@%s)", ig_user_id, acc[1])

    async def _poll_account(self, acc: tuple) -> None:
        ig_user_id, username, cookies_json, last_seen = acc
        chats = self.links.ig_tg_chats_for_account(ig_user_id)
        if not chats:
            return
        cl = await self._client(ig_user_id, cookies_json)
        if cl is None:
            return
        threads = await asyncio.to_thread(self._fetch_threads, cl)

        last_seen = int(last_seen or 0)
        first_run = last_seen == 0
        new_max = last_seen
        items: list[tuple[int, object, str]] = []
        for th, is_pending in threads:
            if is_pending:
                for tg_chat_id in chats:
                    await self._notify_pending(tg_chat_id, ig_user_id, username, th)
                continue
            umap = {str(u.pk): (u.username or "instagram") for u in (th.users or [])}
            for msg in (th.messages or []):
                ts = self._ts(msg)
                if ts <= last_seen:
                    continue
                new_max = max(new_max, ts)
                if first_run or getattr(msg, "is_sent_by_viewer", False):
                    continue  # первый прогон — только запоминаем курсор; свои — мимо
                author = umap.get(str(getattr(msg, "user_id", "") or "")) or username
                items.append((ts, msg, author))

        items.sort(key=lambda x: x[0])
        for _ts, msg, author in items:
            for tg_chat_id in chats:
                try:
                    await self._forward(tg_chat_id, msg, author)
                except Exception:  # noqa: BLE001 — одно сообщение не должно ронять опрос
                    log.exception("Instagram: не удалось переслать сообщение в %s", tg_chat_id)

        if new_max > last_seen:
            self.links.set_ig_last_seen(ig_user_id, new_max)

    # ---------- логин ----------
    async def _client(self, ig_user_id: int, cookies_json: str):
        cl = self._clients.get(ig_user_id)
        if cl is not None:
            return cl
        cl = await asyncio.to_thread(self._login, cookies_json)
        if cl is not None:
            self._clients[ig_user_id] = cl
        return cl

    @staticmethod
    def _login(cookies_json: str):
        try:
            data = json.loads(cookies_json) if cookies_json else {}
        except ValueError:
            data = {}
        sessionid = data.get("sessionid")
        if not sessionid:
            return None
        cl = _IGClient()
        cl.delay_range = [1, 3]
        settings = data.get("settings")
        if settings:
            try:
                cl.set_settings(settings)
            except Exception:  # noqa: BLE001 — битые settings не должны мешать логину
                pass
        cl.login_by_sessionid(sessionid)
        return cl

    @staticmethod
    def _fetch_threads(cl) -> list[tuple[object, bool]]:
        out: list[tuple[object, bool]] = []
        try:
            out.extend((thread, False) for thread in (cl.direct_threads(amount=15, selected_filter="") or []))
        except Exception:  # noqa: BLE001
            log.exception("Instagram: не удалось получить директ-треды")
            raise  # наверх → сброс клиента и перелогин
        try:
            out.extend((thread, True) for thread in (cl.direct_pending_inbox(amount=10) or []))
        except TypeError:
            try:
                out.extend((thread, True) for thread in (cl.direct_pending_inbox() or []))
            except Exception:  # noqa: BLE001 — pending-инбокс необязателен
                pass
        except Exception:  # noqa: BLE001
            pass
        return out

    async def _notify_pending(self, tg_chat_id: int, ig_user_id: int, account: str, thread) -> None:
        """Показать запрос на переписку один раз в привязанном TG-чате."""
        thread_id = str(getattr(thread, "id", None) or getattr(thread, "thread_id", ""))
        if not thread_id:
            log.warning("Instagram: pending-тред без id для @%s", account)
            return
        if self.links.ig_pending_notified(ig_user_id, thread_id, tg_chat_id):
            return

        users = list(getattr(thread, "users", None) or [])
        sender = users[0] if users else None
        username = str(getattr(sender, "username", "") or "")
        full_name = str(getattr(sender, "full_name", "") or "")
        profile_url = str(getattr(sender, "profile_pic_url", "") or "")
        label = full_name or (f"@{username}" if username else "Неизвестный пользователь")
        lines = [
            "<b>Новый запрос в Instagram</b>",
            f"Аккаунт: <a href=\"https://www.instagram.com/{html.escape(account)}/\">@{html.escape(account)}</a>",
            f"Отправитель: <b>{html.escape(label)}</b>",
        ]
        if username:
            lines.append(f"Профиль: <a href=\"https://www.instagram.com/{html.escape(username)}/\">@{html.escape(username)}</a>")
        caption = "\n".join(lines)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Принять запрос", callback_data=f"igpa:{ig_user_id}:{thread_id}")
        ]])
        try:
            if profile_url:
                await self.tg_bot.send_photo(tg_chat_id, profile_url, caption=caption,
                                             parse_mode="HTML", reply_markup=keyboard)
            else:
                await self.tg_bot.send_message(tg_chat_id, caption, parse_mode="HTML",
                                               reply_markup=keyboard)
        except Exception:  # noqa: BLE001
            log.exception("Instagram: не удалось показать pending-запрос в %s", tg_chat_id)
            return
        self.links.mark_ig_pending_notified(ig_user_id, thread_id, tg_chat_id)

    async def approve_pending(self, tg_chat_id: int, ig_user_id: int, thread_id: str) -> None:
        """Принять запрос, только если кнопка находится в привязанном чате."""
        if tg_chat_id not in self.links.ig_tg_chats_for_account(ig_user_id):
            raise PermissionError("Этот Instagram-аккаунт не привязан к данному чату.")
        account = next((row for row in self.links.bound_ig_accounts() if row[0] == ig_user_id), None)
        if account is None:
            raise ValueError("Instagram-аккаунт больше не подключён.")
        client = await self._client(ig_user_id, account[2])
        if client is None:
            raise RuntimeError("Не удалось войти в Instagram.")
        approved = await asyncio.to_thread(client.direct_pending_approve, int(thread_id))
        if not approved:
            raise RuntimeError("Instagram не подтвердил принятие запроса.")
        self.links.clear_ig_pending_notification(ig_user_id, thread_id)
        # Первый рилс нередко и есть сообщение-запрос.  Отправляем его сразу,
        # иначе общий курсор опроса может посчитать его уже старой историей.
        try:
            thread = await asyncio.to_thread(client.direct_thread, int(thread_id), 20)
            users = {str(user.pk): (user.username or account[1]) for user in (thread.users or [])}
            newest = int(account[3] or 0)
            messages = sorted(thread.messages or [], key=self._ts)
            for msg in messages:
                if getattr(msg, "is_sent_by_viewer", False):
                    continue
                await self._forward(tg_chat_id, msg, users.get(str(getattr(msg, "user_id", ""))) or account[1])
                newest = max(newest, self._ts(msg))
            if newest > int(account[3] or 0):
                self.links.set_ig_last_seen(ig_user_id, newest)
        except Exception:  # noqa: BLE001 — запрос уже принят, доставка повторится при опросе
            log.exception("Instagram: не удалось сразу переслать принятый запрос %s", thread_id)

    @staticmethod
    def _ts(msg) -> int:
        t = getattr(msg, "timestamp", None)
        if t is None:
            return 0
        try:
            return int(t.timestamp() * 1000)
        except (OverflowError, OSError, ValueError):
            return 0

    # ---------- разбор сообщения ----------
    @staticmethod
    def _video_url(msg) -> str | None:
        """URL видео/рилса из сообщения (для download_video) или None."""
        # Рилс/пост как медиа-шэр: строим ссылку на рилс по коду.
        for m in (getattr(msg, "clip", None), getattr(msg, "media_share", None)):
            if m is None:
                continue
            code = getattr(m, "code", None)
            mt = getattr(m, "media_type", None)
            if code and mt in (2, None):
                return f"https://www.instagram.com/reel/{code}/"
            vurl = getattr(m, "video_url", None)
            if vurl:
                return str(vurl)
        # Современный шэр рилса (xma) — прямой mp4.
        xma = getattr(msg, "xma_share", None)
        if xma is not None and getattr(xma, "video_url", None):
            return str(xma.video_url)
        # Эфемерное видео / вложенное медиа.
        vm = getattr(msg, "visual_media", None)
        if vm is not None and getattr(vm, "media", None) is not None:
            dm = vm.media
            if getattr(dm, "media_type", None) == 2 and getattr(dm, "video_url", None):
                return str(dm.video_url)
        dm = getattr(msg, "media", None)
        if dm is not None and getattr(dm, "media_type", None) == 2 and getattr(dm, "video_url", None):
            return str(dm.video_url)
        # Ссылка в тексте (например TikTok/Reels, кинутая в директ).
        lk = getattr(msg, "link", None)
        if lk is not None and getattr(lk, "text", None):
            u = find_video_url(lk.text)
            if u:
                return u
        return None

    @staticmethod
    def _describe(msg) -> str:
        """Короткая пометка для нескачиваемого вложения (фото/голос/гиф/история)."""
        if getattr(msg, "visual_media", None) is not None:
            return "📷 фото/видео"
        dm = getattr(msg, "media", None)
        if dm is not None:
            return "📷 фото" if getattr(dm, "media_type", None) == 1 else "🎬 медиа"
        if getattr(msg, "animated_media", None) is not None:
            return "🎞 GIF"
        if getattr(msg, "story_share", None) or getattr(msg, "reel_share", None):
            return "📖 история"
        it = getattr(msg, "item_type", None) or ""
        if "voice" in it or "audio" in it:
            return "🎤 голосовое"
        return f"[{it}]" if it else ""

    async def _forward(self, tg_chat_id: int, msg, author: str) -> None:
        text = (getattr(msg, "text", None) or "").strip()
        video_url = self._video_url(msg)
        if not video_url and text:
            video_url = find_video_url(text)

        if video_url:
            note = text if (text and text != video_url) else ""
            caption = f"[IG] {author}" + (f": {note}" if note else "")
            res = await download_video(video_url)
            if res:
                data, filename, _title = res
                await self._deliver_video(tg_chat_id, data, filename, caption[:_CAPTION_MAX])
                return
            # Скачать не вышло (приватное/крупное/гео) — отдаём ссылкой.
            await self._deliver_text(tg_chat_id, f"[IG] {author}: 🎬 {video_url}")
            return

        if text:
            await self._deliver_text(tg_chat_id, f"[IG] {author}: {text}")
            return

        note = self._describe(msg)
        if note:
            await self._deliver_text(tg_chat_id, f"[IG] {author}: {note}")

    # ---------- доставка в пару (TG-чат + связанная VK-беседа) ----------
    async def _deliver_text(self, tg_chat_id: int, text: str) -> None:
        await deliver_text(self.tg_bot, self.vk_api, self.vk_token, self.links,
                           tg_chat_id, text[:_TEXT_MAX])

    async def _deliver_video(self, tg_chat_id: int, data: bytes, filename: str,
                             caption: str) -> None:
        try:
            await self.tg_bot.send_video(
                tg_chat_id, BufferedInputFile(data, filename=filename),
                caption=caption or None)
        except Exception:  # noqa: BLE001
            log.exception("Instagram->TG: не удалось отправить видео в %s", tg_chat_id)

        peer = self.links.vk_peer_for_tg_chat(tg_chat_id)
        if not peer:
            return
        attachment = None
        if self.vk_user_token and self.vk_group_id:
            try:
                attachment = await upload_vk_video(
                    self.vk_user_token, self.vk_group_id, filename, data)
            except Exception:  # noqa: BLE001 — нативная заливка не вышла, шлём документом
                log.exception("Instagram->VK: нативная заливка не удалась")
        if not attachment:
            try:
                attachment = await upload_doc(self.vk_token, peer, filename, data)
            except Exception:  # noqa: BLE001
                log.exception("Instagram->VK: заливка документом не удалась")
                return
        try:
            await self.vk_api.messages.send(
                peer_id=peer, message=caption or "", attachment=attachment,
                random_id=_rid())
        except Exception:  # noqa: BLE001
            log.exception("Instagram->VK: не удалось отправить видео в %s", peer)
