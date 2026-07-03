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


def _cmid_of(resp):
    """conversation_message_id из ответа messages.send(peer_ids=[...])."""
    try:
        if isinstance(resp, list) and resp:
            return getattr(resp[0], "conversation_message_id", None)
    except Exception:  # noqa: BLE001
        pass
    return None


async def send_pair(side, tg_chat_id: int, tg_text: str, vk_text: str,
                    *, tg_html: bool = False) -> dict:
    """Отправить сообщение в TG-чат и парную VK-беседу, вернуть хэндл для
    последующей правки/удаления: {chat_id, tg_id, peer, cmid}. Любое поле может
    остаться None, если соответствующая сторона недоступна."""
    handle = {"chat_id": tg_chat_id, "tg_id": None, "peer": None, "cmid": None}
    try:
        m = await side.tg_bot.send_message(
            tg_chat_id, tg_text, parse_mode="HTML" if tg_html else None)
        handle["tg_id"] = m.message_id
    except Exception:  # noqa: BLE001
        log.exception("Discord flood: не удалось отправить в TG %s", tg_chat_id)

    peer = side.links.vk_peer_for_tg_chat(tg_chat_id)
    if peer:
        handle["peer"] = peer
        try:
            resp = await side.vk_api.messages.send(
                peer_ids=[peer], message=vk_text, random_id=_rid())
            handle["cmid"] = _cmid_of(resp)
        except Exception:  # noqa: BLE001
            log.exception("Discord flood: не удалось отправить в VK %s", peer)
    return handle


async def edit_pair(side, handle: dict, tg_text: str, vk_text: str,
                    *, tg_html: bool = False) -> None:
    """Отредактировать ранее отправленную пару сообщений по хэндлу."""
    if handle.get("tg_id"):
        try:
            await side.tg_bot.edit_message_text(
                tg_text, chat_id=handle["chat_id"], message_id=handle["tg_id"],
                parse_mode="HTML" if tg_html else None)
        except Exception:  # noqa: BLE001 — «не изменилось»/удалено/старше лимита
            log.debug("Discord flood: правка TG не удалась", exc_info=True)
    if handle.get("cmid"):
        try:
            await side.vk_api.messages.edit(
                peer_id=handle["peer"], cmid=handle["cmid"], message=vk_text)
        except Exception:  # noqa: BLE001 — лимит VK 24ч / сообщение удалено
            log.debug("Discord flood: правка VK не удалась", exc_info=True)


async def delete_pair(side, handle: dict) -> None:
    """Удалить ранее отправленную пару сообщений по хэндлу."""
    if handle.get("tg_id"):
        try:
            await side.tg_bot.delete_message(handle["chat_id"], handle["tg_id"])
        except Exception:  # noqa: BLE001 — нет прав/старше 48ч
            log.debug("Discord flood: удаление TG не удалось", exc_info=True)
    if handle.get("cmid"):
        try:
            await side.vk_api.messages.delete(
                peer_id=handle["peer"], cmids=[handle["cmid"]], delete_for_all=1)
        except Exception:  # noqa: BLE001
            log.debug("Discord flood: удаление VK не удалось", exc_info=True)


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
