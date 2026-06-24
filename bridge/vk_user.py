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


async def _download_best_mp4(session, files: dict) -> bytes | None:
    """Из словаря files видео (mp4_240/480/...) качает лучший mp4 в лимите TG."""
    mp4 = sorted(
        ((int(k.split("_")[1]), url) for k, url in (files or {}).items()
         if k.startswith("mp4_") and url),
        reverse=True,
    )
    for _, url in mp4:
        async with session.get(url) as vr:
            if vr.content_length and vr.content_length > TG_UPLOAD_LIMIT:
                continue
            data = await vr.read()
        if len(data) <= TG_UPLOAD_LIMIT:
            return data
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
        data = await _download_best_mp4(session, files)
        if data is None:
            log.info("video.get: для %s нет mp4 (ключи: %s, restricted=%s, processing=%s)",
                     videos, list(files.keys()), item.get("content_restricted"),
                     item.get("processing"))
        return data, item
    except Exception:  # noqa: BLE001
        log.exception("VK video.get: не удалось скачать видео")
    return None, None


def _find_video(messages, owner_id, video_id) -> dict | None:
    """Рекурсивно (attachments/fwd_messages/reply) ищет объект видео."""
    for m in messages or []:
        for a in (m.get("attachments") or []):
            v = a.get("video")
            if v and v.get("owner_id") == owner_id and v.get("id") == video_id:
                return v
        if v := _find_video(m.get("fwd_messages"), owner_id, video_id):
            return v
        rm = m.get("reply_message")
        if rm and (v := _find_video([rm], owner_id, video_id)):
            return v
    return None


def _find_video_key(messages, owner_id, video_id) -> str | None:
    v = _find_video(messages, owner_id, video_id)
    return v.get("access_key") if v else None


async def _resolve_user_video(session, user_token, peer_id, cmid,
                              owner_id, video_id) -> dict | None:
    """Перечитать видео ПОЛЬЗОВАТЕЛЬСКИМ токеном и вернуть полный объект.

    Сообщение приходит через community-токен, и access_key во вложении привязан к
    нему — video.get под user-токеном его не принимает (VK отвечает как для
    приватного видео). Перечитываем то же сообщение user-токеном; заодно
    messages-версия объекта богаче лонгполльной (response_type=min).

    ВАЖНО: peer_id у community-токена ЛОКАЛЬНЫЙ (нумерация бесед своя) и не
    совпадает с тем, что видит user-токен. Поэтому пробуем переданный peer_id, а
    если пусто — ищем ту же беседу среди диалогов user-токена по cmid + видео.
    """
    async def lookup(peer) -> dict | None:
        try:
            resp = await _method(session, user_token, "messages.getByConversationMessageId",
                                 {"peer_id": peer, "conversation_message_ids": cmid})
        except Exception:  # noqa: BLE001
            return None
        v = _find_video(resp.get("items"), owner_id, video_id)
        if v is not None:
            # [DEBUG кружки] полный объект видео из messages — есть ли тут files/player?
            import json
            log.info("DEBUG msg-video raw (peer=%s): %s", peer,
                     json.dumps(v, ensure_ascii=False, default=str))
        return v

    if peer_id and (v := await lookup(peer_id)):
        return v

    try:
        convs = await _method(session, user_token, "messages.getConversations", {"count": 200})
    except Exception:  # noqa: BLE001
        log.exception("messages.getConversations не удался")
        return None
    for c in convs.get("items", []):
        peer = (c.get("conversation") or {}).get("peer", {}).get("id")
        if not peer or peer < 2000000000 or peer == peer_id:
            continue  # только беседы; переданный peer уже пробовали
        if v := await lookup(peer):
            return v
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

        if not cmid:
            return None

        # Перечитываем видео user-токеном (полный объект; заодно user-scoped key).
        v = await _resolve_user_video(s, user_token, peer_id, cmid, owner_id, video_id)
        if v is None:
            log.info("video.get: %s_%s не найдено user-токеном (реально недоступно?)",
                     owner_id, video_id)
            return None

        # 1) mp4 прямо в объекте из messages (кружки отдают files именно так).
        data = await _download_best_mp4(s, v.get("files") or {})
        if data is not None:
            log.info("video.get: %s_%s взято из messages-объекта", owner_id, video_id)
            return data

        # 2) Повтор video.get с user-scoped access_key (приватные видео в беседе).
        fresh = v.get("access_key")
        if fresh and fresh != key:
            log.info("video.get: повтор с user-scoped access_key для %s_%s",
                     owner_id, video_id)
            data, _ = await _try_download(s, user_token, owner_id, video_id, fresh)
            if data is not None:
                return data
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
