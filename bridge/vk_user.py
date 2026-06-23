"""Видео через пользовательский токен VK (video.* недоступны community-токену).

VK→TG: video.get отдаёт прямые mp4 -> скачиваем -> шлём настоящим видео.
TG→VK: video.save -> заливаем файл -> прикрепляем video{owner}_{id} к сообщению.
ID сообщества (group_id) определяется автоматически из community-токена.
"""

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


async def _try_download(session, user_token, owner_id, video_id, access_key) -> bytes | None:
    """Одна попытка: video.get -> качаем лучший mp4 в пределах лимита TG."""
    videos = f"{owner_id}_{video_id}" + (f"_{access_key}" if access_key else "")
    try:
        resp = await _method(session, user_token, "video.get", {"videos": videos})
        items = resp.get("items") or []
        if not items:
            log.info("video.get: видео %s недоступно (нет items)", videos)
            return None
        files = items[0].get("files") or {}
        mp4 = sorted(
            ((int(k.split("_")[1]), url) for k, url in files.items()
             if k.startswith("mp4_") and url),
            reverse=True,
        )
        if not mp4:
            log.info("video.get: для %s нет mp4 (ключи: %s, restricted=%s)",
                     videos, list(files.keys()), items[0].get("content_restricted"))
            return None
        for _, url in mp4:
            async with session.get(url) as vr:
                if vr.content_length and vr.content_length > TG_UPLOAD_LIMIT:
                    continue
                data = await vr.read()
            if len(data) <= TG_UPLOAD_LIMIT:
                return data
    except Exception:  # noqa: BLE001
        log.exception("VK video.get: не удалось скачать видео")
    return None


async def _resolve_user_access_key(session, user_token, peer_id, cmid,
                                   owner_id, video_id) -> str | None:
    """Взять access_key видео, выданный под ПОЛЬЗОВАТЕЛЬСКИЙ токен.

    Сообщение приходит через групповой лонгполл, и access_key во вложении привязан
    к community-токену — video.get под user-токеном его не принимает (VK отвечает
    content_restricted, как для приватных видео, залитых прямо в беседу).
    Перечитываем то же сообщение user-токеном и берём его access_key.
    """
    try:
        resp = await _method(session, user_token, "messages.getByConversationMessageId",
                             {"peer_id": peer_id, "conversation_message_ids": cmid})
    except Exception:  # noqa: BLE001
        log.exception("messages.getByConversationMessageId не удался")
        return None

    def find(messages):
        for m in messages or []:
            for a in (m.get("attachments") or []):
                v = a.get("video")
                if v and v.get("owner_id") == owner_id and v.get("id") == video_id:
                    return v.get("access_key")
            key = find(m.get("fwd_messages"))
            if key:
                return key
            rm = m.get("reply_message")
            if rm and (key := find([rm])):
                return key
        return None

    return find(resp.get("items"))


async def download_vk_video(user_token, owner_id, video_id, access_key,
                            peer_id=None, cmid=None) -> bytes | None:
    """Скачать лучший mp4 в пределах лимита Telegram. None — если не вышло.

    peer_id/cmid (если есть) позволяют перечитать сообщение и взять access_key
    под user-токен — иначе приватные видео из бесед не качаются (групповой ключ).
    """
    async with _session() as s:
        data = await _try_download(s, user_token, owner_id, video_id, access_key)
        if data is not None:
            return data
        if peer_id and cmid:
            fresh = await _resolve_user_access_key(s, user_token, peer_id, cmid,
                                                   owner_id, video_id)
            if fresh and fresh != access_key:
                log.info("video.get: повтор с user-scoped access_key для %s_%s",
                         owner_id, video_id)
                return await _try_download(s, user_token, owner_id, video_id, fresh)
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
