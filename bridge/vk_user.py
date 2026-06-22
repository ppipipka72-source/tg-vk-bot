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


async def download_vk_video(user_token, owner_id, video_id, access_key) -> bytes | None:
    """Скачать лучший mp4 в пределах лимита Telegram. None — если не вышло."""
    videos = f"{owner_id}_{video_id}" + (f"_{access_key}" if access_key else "")
    try:
        async with _session() as s:
            resp = await _method(s, user_token, "video.get", {"videos": videos})
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
                log.info("video.get: для %s нет mp4 (ключи: %s) — внешнее/недоступное видео",
                         videos, list(files.keys()))
                return None
            for _, url in mp4:
                async with s.get(url) as vr:
                    if vr.content_length and vr.content_length > TG_UPLOAD_LIMIT:
                        continue
                    data = await vr.read()
                if len(data) <= TG_UPLOAD_LIMIT:
                    return data
    except Exception:  # noqa: BLE001
        log.exception("VK video.get: не удалось скачать видео")
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
