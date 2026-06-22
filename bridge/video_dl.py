"""Скачивание видео по ссылке (TikTok / YouTube Shorts / Instagram Reels и др.).

Используется yt-dlp как библиотека. Скачиваем во временную папку, читаем байты,
папку удаляем. Загрузка блокирующая -> выносим в пул потоков, чтобы не тормозить
мост. Лимит совпадает с лимитом отдачи файла ботом в Telegram (50 МБ).
"""

import asyncio
import glob
import logging
import os
import re
import shutil
import tempfile
from urllib.parse import urlparse

log = logging.getLogger(__name__)

MAX_BYTES = 50 * 1024 * 1024  # 50 МБ — лимит загрузки файла ботом в Telegram

# Корень проекта — чтобы искать cookies-файл независимо от рабочей директории.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Имена cookies-файлов, которые подхватываем автоматически (Instagram и пр.
# требуют авторизации). Достаточно положить такой файл в папку проекта.
_DEFAULT_COOKIE_FILES = ("cookies.txt", "ig_cookies.txt")

# Любой http(s)-URL в тексте.
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

# Правила: (домен, префикс пути | None). Сверка по хосту (а не подстроке),
# чтобы x.com не ловил xbox.com/max.com и т.п. None в пути = любой путь.
_RULES = (
    ("tiktok.com", None),
    ("youtu.be", None),
    ("youtube.com", "/shorts/"),
    ("instagram.com", "/reel"),     # покрывает /reel/ и /reels/
    ("x.com", None),
    ("twitter.com", None),
    ("vxtwitter.com", None),        # популярные зеркала, которые часто кидают
    ("fxtwitter.com", None),
    ("fixupx.com", None),
)


def _is_supported(url: str) -> bool:
    try:
        p = urlparse(url)
    except ValueError:
        return False
    host = (p.netloc or "").lower().split(":")[0]
    for prefix in ("www.", "m.", "mobile."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    path = p.path or ""
    for dom, path_prefix in _RULES:
        if host == dom or host.endswith("." + dom):
            if path_prefix is None or path.startswith(path_prefix):
                return True
    return False


def find_video_url(text: str | None) -> str | None:
    """Первый поддерживаемый URL из текста сообщения, иначе None."""
    if not text:
        return None
    for url in _URL_RE.findall(text):
        if _is_supported(url):
            return url
    return None


def _ydl_opts(outdir: str) -> dict:
    opts = {
        "outtmpl": os.path.join(outdir, "%(id)s.%(ext)s"),
        # Один готовый файл (без склейки видео+аудио -> не требует ffmpeg).
        "format": "best[ext=mp4]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_BYTES,
        "retries": 2,
        "socket_timeout": 30,
        # Иногда обходит YouTube-проверку "подтвердите, что вы не бот".
        "extractor_args": {"youtube": {"player_client": ["tv", "web_safari", "default"]}},
    }
    # Куки для площадок, требующих авторизации (Instagram, YouTube и пр.).
    # Приоритет: явный путь в YTDLP_COOKIES -> cookies.txt в папке проекта.
    cookiefile = os.getenv("YTDLP_COOKIES", "").strip()
    if not cookiefile:
        for name in _DEFAULT_COOKIE_FILES:
            cand = os.path.join(_PROJECT_ROOT, name)
            if os.path.exists(cand):
                cookiefile = cand
                break
    if cookiefile:
        opts["cookiefile"] = cookiefile
    browser = os.getenv("YTDLP_COOKIES_FROM_BROWSER", "").strip()
    if browser:
        # "firefox" | "chrome" | "edge" | "chrome:Profile 1"
        opts["cookiesfrombrowser"] = (
            tuple(browser.split(":", 1)) if ":" in browser else (browser,))
    return opts


def _blocking_download(url: str, outdir: str) -> tuple[str, str] | None:
    """Скачать видео в outdir. Возвращает (путь, заголовок) или None."""
    import yt_dlp

    with yt_dlp.YoutubeDL(_ydl_opts(outdir)) as ydl:
        info = ydl.extract_info(url, download=True)
        info = ydl.sanitize_info(info)

    path = None
    for d in (info or {}).get("requested_downloads") or []:
        if d.get("filepath"):
            path = d["filepath"]
            break
    if not path or not os.path.exists(path):
        files = glob.glob(os.path.join(outdir, "*"))
        path = files[0] if files else None
    if not path:
        return None
    return path, ((info or {}).get("title") or "")


async def download_video(url: str) -> tuple[bytes, str, str] | None:
    """Скачать видео по ссылке. Возвращает (данные, имя_файла, заголовок) или None."""
    loop = asyncio.get_running_loop()
    outdir = tempfile.mkdtemp(prefix="vdl_")
    try:
        res = await loop.run_in_executor(None, _blocking_download, url, outdir)
        if not res:
            log.info("link-video: ничего не скачалось (%s)", url)
            return None
        path, title = res
        size = os.path.getsize(path)
        if size > MAX_BYTES:
            log.info("link-video: %s слишком большой (%d МБ) — пропуск",
                     url, size // (1024 * 1024))
            return None
        with open(path, "rb") as f:
            data = f.read()
        return data, os.path.basename(path), title
    except Exception as e:  # noqa: BLE001 — недоступное видео не должно ронять мост
        # DownloadError — ожидаемо (приватное/гео/требует кук): без трейсбека.
        if type(e).__name__ == "DownloadError":
            first = str(e).replace("\n", " ").strip()[:200]
            log.warning("link-video: не удалось скачать %s — %s", url, first)
        else:
            log.exception("link-video: ошибка при скачивании %s", url)
        return None
    finally:
        shutil.rmtree(outdir, ignore_errors=True)
