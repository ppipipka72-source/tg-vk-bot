import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Не задана обязательная переменная окружения {name}. "
            f"Скопируй .env.example в .env и заполни."
        )
    return value


def _parse_admins(raw: str) -> tuple[int, ...]:
    ids = []
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part:
            try:
                ids.append(int(part))
            except ValueError:
                pass
    return tuple(ids)


@dataclass(frozen=True)
class Config:
    tg_token: str
    vk_token: str
    vk_group_id: int  # 0 если не задан (определится автоматически)
    vk_user_token: str  # опционально: для нативного видео (video.get/video.save)
    log_level: str
    admin_ids: tuple[int, ...]  # TG user id владельцев — кто управляет парами
    # Локальный Telegram Bot API сервер (для видео >20 МБ). Пусто -> облачный
    # api.telegram.org (лимит скачивания 20 МБ, отправки 50 МБ). С локальным
    # сервером лимиты поднимаются до 2 ГБ. tg_api_local=True — сервер запущен
    # с флагом --local (getFile отдаёт локальные пути, файл читается с диска).
    tg_api_url: str = ""
    tg_api_local: bool = False
    # Необязательная начальная пара (для совместимости/первого запуска):
    tg_chat_id: int = 0
    vk_peer_id: int = 0
    # --- Discord (опционально) ---
    # Если discord_token пуст — Discord-бот не запускается, мост работает как обычно.
    # Команды управления (/ds_connect) и вывод (/vc, уведомления) идут через мост
    # (Telegram + VK), а не внутри Discord.
    discord_token: str = ""
    # Схлопывание флуда голосовых событий (по пользователю): с первого события
    # заводится одно живое сообщение со свёрнутой цитатой, куда дописываются все
    # действия человека. Окно закрывается НЕ по таймеру тишины, а когда люди
    # напишут в чат discord_flood_msgs сообщений (старое сообщение «уехало»
    # вверх — править его бессмысленно, следующее событие пишет новое). Пока в
    # чате тихо — сообщение обновляется сколько угодно, вплоть до страховочного
    # предела discord_flood_max_age секунд (чуть меньше лимита правок VK — 24ч).
    discord_flood_collapse: bool = True
    discord_flood_msgs: int = 5
    discord_flood_max_age: float = 82800.0  # 23ч — ниже лимита правок VK (24ч)
    # --- Веб-приложение «альты» (Telegram Mini App, /alt web) ---
    # Включается, когда заданы И webapp_public_url, И webapp_bot_app. Mini App
    # настраивается один раз в @BotFather (/newapp), его web app URL = webapp_public_url.
    webapp_public_url: str = ""   # https://<домен> — корень страницы Mini App
    webapp_bot_app: str = ""      # короткое имя Mini App из BotFather
    webapp_host: str = "127.0.0.1"  # на каком интерфейсе слушает aiohttp (за Caddy)
    webapp_port: int = 8090       # локальный порт aiohttp (Caddy проксирует на него)


def load_config() -> Config:
    return Config(
        tg_token=_require("TG_BOT_TOKEN"),
        vk_token=_require("VK_TOKEN"),
        vk_group_id=int(os.getenv("VK_GROUP_ID") or 0),
        vk_user_token=os.getenv("VK_USER_TOKEN", ""),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        admin_ids=_parse_admins(os.getenv("TG_ADMINS", "")),
        tg_api_url=os.getenv("TG_API_URL", "").strip(),
        tg_api_local=(os.getenv("TG_API_LOCAL", "") or "").strip().lower()
        in ("1", "true", "yes", "on"),
        tg_chat_id=int(os.getenv("TG_CHAT_ID") or 0),
        vk_peer_id=int(os.getenv("VK_PEER_ID") or 0),
        discord_token=os.getenv("DISCORD_TOKEN", ""),
        discord_flood_collapse=(os.getenv("DISCORD_FLOOD_COLLAPSE", "1") or "1")
        .strip().lower() in ("1", "true", "yes", "on"),
        discord_flood_msgs=int(os.getenv("DISCORD_FLOOD_MSGS") or 5),
        discord_flood_max_age=float(os.getenv("DISCORD_FLOOD_MAX_AGE") or 82800),
        webapp_public_url=os.getenv("WEBAPP_PUBLIC_URL", "").strip().rstrip("/"),
        webapp_bot_app=os.getenv("WEBAPP_BOT_APP", "").strip(),
        webapp_host=os.getenv("WEBAPP_HOST", "127.0.0.1").strip() or "127.0.0.1",
        webapp_port=int(os.getenv("WEBAPP_PORT") or 8090),
    )
