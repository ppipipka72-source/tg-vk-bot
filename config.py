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
    # Необязательная начальная пара (для совместимости/первого запуска):
    tg_chat_id: int = 0
    vk_peer_id: int = 0
    # --- Discord (опционально) ---
    # Если discord_token пуст — Discord-бот не запускается, мост работает как обычно.
    discord_token: str = ""
    discord_guild_id: int = 0          # для мгновенной регистрации slash-команд
    discord_notify_channel_id: int = 0  # текстовый канал для уведомлений о войсах


def load_config() -> Config:
    return Config(
        tg_token=_require("TG_BOT_TOKEN"),
        vk_token=_require("VK_TOKEN"),
        vk_group_id=int(os.getenv("VK_GROUP_ID") or 0),
        vk_user_token=os.getenv("VK_USER_TOKEN", ""),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        admin_ids=_parse_admins(os.getenv("TG_ADMINS", "")),
        tg_chat_id=int(os.getenv("TG_CHAT_ID") or 0),
        vk_peer_id=int(os.getenv("VK_PEER_ID") or 0),
        discord_token=os.getenv("DISCORD_TOKEN", ""),
        discord_guild_id=int(os.getenv("DISCORD_GUILD_ID") or 0),
        discord_notify_channel_id=int(os.getenv("DISCORD_NOTIFY_CHANNEL_ID") or 0),
    )
