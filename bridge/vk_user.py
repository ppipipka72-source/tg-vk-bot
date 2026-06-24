"""Видео через пользовательский токен VK (video.* недоступны community-токену).

VK→TG: video.get отдаёт прямые mp4 -> скачиваем -> шлём настоящим видео.
TG→VK: video.save -> заливаем файл -> прикрепляем video{owner}_{id} к сообщению.
ID сообщества (group_id) определяется автоматически из community-токена.
"""

import asyncio
import logging
import ssl

import aiohttp

from .vk_upload import _method, _post_file

log = logging.getLogger(__name__)

TG_UPLOAD_LIMIT = 50 * 1024 * 1024  # лимит загрузки файла ботом в Telegram

# CDN видео VK (okcdn.ru) отдаёт цепочку, которую системный trust store Windows
# не всегда проверяет -> берём корни из certifi.
try:
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except Exception:  # noqa: BLE001 — certifi нет -> системный trust store
    _SSL = None


def _session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=_SSL))


async def detect_group_id(community_token: str) -> int | None:
    """Узнать id сообщества по его токену (groups.getById без параметров)."""
    try:
        async with _session() as s:
            resp = await _method(s, community_token, "groups.getById", {})
        groups = resp.get("groups") if isinstance(resp, dict) else resp
        if groups:
            return int(groups[0]["id"])
    except Exception:  # noqa: BLE001
        log.exception("Не удалось определить group_id")
    return None


async def _try_download(session, user_token, owner_id, video_id, access_key):
    """Одна попытка: video.get -> качаем лучший mp4 в пределах лимита TG.

    Возвращает (data, item): data — байты mp4 или None; item — сырой объект
    видео из video.get (или None, если items нет/ошибка), чтобы вызывающий мог
    посмотреть на content_restricted / processing и решить, ждать ли.
    """
    videos = f"{owner_id}_{video_id}" + (f"_{access_key}" if access_key else "")
    try:
        resp = await _method(session, user_token, "video.get", {"videos": videos})
        items = resp.get("items") or []
        if not items:
            log.info("video.get: видео %s недоступно (нет items)", videos)
            return None, None
        item = items[0]
        files = item.get("files") or {}
        mp4 = sorted(
            ((int(k.split("_")[1]), url) for k, url in files.items()
             if k.startswith("mp4_") and url),
            reverse=True,
        )
        if not mp4:
            log.info("video.get: для %s нет mp4 (ключи: %s, restricted=%s, processing=%s)",
                     videos, list(files.keys()), item.get("content_restricted"),
                     item.get("processing"))
            return None, item
        for _, url in mp4:
            async with session.get(url) as vr:
                if vr.content_length and vr.content_length > TG_UPLOAD_LIMIT:
                    continue
                data = await vr.read()
            if len(data) <= TG_UPLOAD_LIMIT:
                return data, item
        return None, item
    except Exception:  # noqa: BLE001
        log.exception("VK video.get: не удалось скачать видео")
    return None, None


def _find_video_key(messages, owner_id, video_id) -> str | None:
    """Рекурсивно (attachments/fwd_messages/reply) ищет access_key видео."""
    for m in messages or []:
        for a in (m.get("attachments") or []):
            v = a.get("video")
            if v and v.get("owner_id") == owner_id and v.get("id") == video_id:
                return v.get("access_key")
        if key := _find_video_key(m.get("fwd_messages"), owner_id, video_id):
            return key
        rm = m.get("reply_message")
        if rm and (key := _find_video_key([rm], owner_id, video_id)):
            return key
    return None


async def _resolve_user_access_key(session, user_token, peer_id, cmid,
                                   owner_id, video_id) -> str | None:
    """Взять access_key видео, выданный под ПОЛЬЗОВАТЕЛЬСКИЙ токен.

    Сообщение приходит через community-токен, и access_key во вложении привязан к
    нему — video.get под user-токеном его не принимает (VK отвечает как для
    приватного видео). Перечитываем то же сообщение user-токеном.

    ВАЖНО: peer_id у community-токена ЛОКАЛЬНЫЙ (нумерация бесед своя) и не
    совпадает с тем, что видит user-токен. Поэтому пробуем переданный peer_id, а
    если пусто — ищем ту же беседу среди диалогов user-токена по cmid + видео.
    """
    async def lookup(peer) -> str | None:
        try:
            resp = await _method(session, user_token, "messages.getByConversationMessageId",
                                 {"peer_id": peer, "conversation_message_ids": cmid})
        except Exception:  # noqa: BLE001
            return None
        return _find_video_key(resp.get("items"), owner_id, video_id)

    if peer_id and (key := await lookup(peer_id)):
        return key

    try:
        convs = await _method(session, user_token, "messages.getConversations", {"count": 200})
    except Exception:  # noqa: BLE001
        log.exception("messages.getConversations не удался")
        return None
    for c in convs.get("items", []):
        peer = (c.get("conversation") or {}).get("peer", {}).get("id")
        if not peer or peer < 2000000000 or peer == peer_id:
            continue  # только беседы; переданный peer уже пробовали
        if key := await lookup(peer):
            return key
    return None


async def download_vk_video(user_token, owner_id, video_id, access_key,
                            peer_id=None, cmid=None) -> bytes | None:
    """Скачать лучший mp4 в пределах лимита Telegram. None — если не вышло.

    peer_id/cmid (если есть) позволяют перечитать сообщение и взять access_key
    под user-токен — иначе приватные видео из бесед не качаются (групповой ключ).
    """
    async with _session() as s:
        key = access_key
        data, item = await _try_download(s, user_token, owner_id, video_id, key)
        if data is not None:
            return data

        # Ограничено для группового ключа -> перечитываем access_key под user-токен.
        restricted = item is not None and item.get("content_restricted")
        if (item is None or restricted) and cmid:
            fresh = await _resolve_user_access_key(s, user_token, peer_id, cmid,
                                                   owner_id, video_id)
            if fresh and fresh != key:
                log.info("video.get: повтор с user-scoped access_key для %s_%s",
                         owner_id, video_id)
                key = fresh
                data, item = await _try_download(s, user_token, owner_id, video_id, key)
                if data is not None:
                    return data
            else:
                log.info("video.get: user-scoped ключ не найден для %s_%s (видео реально недоступно?)",
                         owner_id, video_id)

        # Кружок (video_message) ещё транскодируется — VK отдаёт объект без files
        # (флаг processing ненадёжен: бывает и 1, и None). Если видео нам доступно
        # (не content_restricted), но mp4 ещё нет — ждём и опрашиваем с бэкоффом.
        if item is not None and not item.get("content_restricted"):
            for delay in (2, 3, 5, 8, 12, 15):
                await asyncio.sleep(delay)
                data, item = await _try_download(s, user_token, owner_id, video_id, key)
                if data is not None:
                    log.info("video.get: %s_%s готово после ожидания транскодинга",
                             owner_id, video_id)
                    return data
                if item is None or item.get("content_restricted"):
                    break
            log.info("video.get: %s_%s так и не отдало mp4 за отведённое время",
                     owner_id, video_id)
    return None


async def upload_vk_video(user_token, group_id, filename, data) -> str:
    """Залить видео в сообщество и вернуть строку вложения video{owner}_{id}.

    is_private=1: видео не попадает в каталог/«Клипы» сообщества и доступно
    только по прямой ссылке (через access_key во вложении сообщения).
    """
    params = {"name": filename or "video", "wallpost": 0, "is_private": 1}
    if group_id:
        params["group_id"] = group_id
    async with _session() as s:
        save = await _method(s, user_token, "video.save", params)
        await _post_file(s, save["upload_url"], "video_file", data,
                         filename or "video.mp4", "video/mp4")
    owner_id = save["owner_id"]
    video_id = save["video_id"]
    access_key = save.get("access_key")
    return f"video{owner_id}_{video_id}" + (f"_{access_key}" if access_key else "")
