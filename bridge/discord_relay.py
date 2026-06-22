"""Доставка уведомлений Discord-наблюдателя в пару моста (Telegram + VK).

Текст/картинка отправляются СРАЗУ в оба чата: TG-чат и связанную с ним
VK-беседу (peer берётся из pairs по tg_chat_id).
"""

import logging
import random

from aiogram.types import BufferedInputFile

from .vk_upload import upload_photo

log = logging.getLogger(__name__)


def _rid() -> int:
    return random.getrandbits(31)


async def deliver_text(tg_bot, vk_api, vk_token, links, tg_chat_id: int,
                       text: str) -> None:
    """Отправить текст в TG-чат и в связанную VK-беседу."""
    try:
        await tg_bot.send_message(tg_chat_id, text)
    except Exception:  # noqa: BLE001
        log.exception("Discord->TG: не удалось отправить текст в %s", tg_chat_id)

    peer_id = links.vk_peer_for_tg_chat(tg_chat_id)
    if peer_id:
        try:
            await vk_api.messages.send(peer_id=peer_id, message=text, random_id=_rid())
        except Exception:  # noqa: BLE001
            log.exception("Discord->VK: не удалось отправить текст в %s", peer_id)


async def deliver_photo(tg_bot, vk_api, vk_token, links, tg_chat_id: int,
                        png: bytes, caption: str = "") -> None:
    """Отправить картинку (PNG) в TG-чат и в связанную VK-беседу."""
    try:
        await tg_bot.send_photo(
            tg_chat_id, BufferedInputFile(png, "voice.png"), caption=caption or None)
    except Exception:  # noqa: BLE001
        log.exception("Discord->TG: не удалось отправить фото в %s", tg_chat_id)

    peer_id = links.vk_peer_for_tg_chat(tg_chat_id)
    if peer_id:
        try:
            attachment = await upload_photo(vk_token, peer_id, png)
            await vk_api.messages.send(peer_id=peer_id, message=caption or "",
                                       attachment=attachment, random_id=_rid())
        except Exception:  # noqa: BLE001
            log.exception("Discord->VK: не удалось отправить фото в %s", peer_id)
