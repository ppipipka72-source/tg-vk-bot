"""Прямая загрузка вложений в VK через HTTP API.

vkbottle'овские uploader'ы в текущих версиях криво формируют запрос к
*.getMessagesUploadServer (VK отвечает null). Поэтому ходим в API напрямую:
getUploadServer -> POST файла на upload_url -> save.
"""

import json

import aiohttp

API = "https://api.vk.com/method/"
API_VERSION = "5.199"


async def _method(session: aiohttp.ClientSession, token: str, name: str, params: dict) -> dict:
    full = {k: str(v) for k, v in params.items() if v is not None}
    full["access_token"] = token
    full["v"] = API_VERSION
    async with session.get(API + name, params=full) as resp:
        data = await resp.json(content_type=None)
    if not data or "error" in data:
        raise RuntimeError(f"VK {name} error: {data.get('error') if data else 'empty response'}")
    return data["response"]


async def _post_file(session, upload_url, field, data, filename, content_type):
    if not data:
        raise RuntimeError("файл пустой (0 байт) — нечего загружать")

    url = upload_url
    status = text = None
    # Идём по редиректам ВРУЧНУЮ, сохраняя POST (aiohttp на 302 сбрасывает
    # метод на GET, и upload-сервер VK отвечает 405).
    for _ in range(5):
        form = aiohttp.FormData()
        form.add_field(field, data, filename=filename, content_type=content_type)
        async with session.post(url, data=form, allow_redirects=False) as resp:
            status = resp.status
            if status in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                url = resp.headers["Location"]
                continue
            text = await resp.text()
        break

    host = url.split("/", 3)[2] if "//" in url else url
    diag = f"HTTP {status}, host={host}, sent={len(data)}b"
    if not text or not text.strip():
        raise RuntimeError(f"upload-сервер вернул пусто ({diag})")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError(f"upload-сервер вернул не JSON ({diag}): {text[:200]!r}")


async def upload_photo(token: str, peer_id: int, data: bytes) -> str:
    async with aiohttp.ClientSession() as s:
        server = await _method(s, token, "photos.getMessagesUploadServer", {"peer_id": peer_id})
        up = await _post_file(s, server["upload_url"], "photo", data, "photo.png", "image/png")
        saved = await _method(s, token, "photos.saveMessagesPhoto", {
            "photo": up["photo"], "server": up["server"], "hash": up["hash"],
        })
    obj = saved[0]
    return f"photo{obj['owner_id']}_{obj['id']}"


async def upload_doc(token: str, peer_id: int, filename: str, data: bytes) -> str:
    async with aiohttp.ClientSession() as s:
        server = await _method(s, token, "docs.getMessagesUploadServer",
                               {"type": "doc", "peer_id": peer_id})
        up = await _post_file(s, server["upload_url"], "file", data, filename,
                              "application/octet-stream")
        saved = await _method(s, token, "docs.save", {"file": up["file"], "title": filename})
    obj = saved.get("doc") or saved
    return f"doc{obj['owner_id']}_{obj['id']}"


async def upload_voice(token: str, peer_id: int, data: bytes) -> str:
    async with aiohttp.ClientSession() as s:
        server = await _method(s, token, "docs.getMessagesUploadServer",
                               {"type": "audio_message", "peer_id": peer_id})
        up = await _post_file(s, server["upload_url"], "file", data, "voice.ogg", "audio/ogg")
        saved = await _method(s, token, "docs.save", {"file": up["file"]})
    am = saved.get("audio_message")
    if am:
        return f"audio_message{am['owner_id']}_{am['id']}"
    obj = saved.get("doc")
    return f"doc{obj['owner_id']}_{obj['id']}"
