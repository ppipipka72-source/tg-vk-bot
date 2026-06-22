import asyncio
import logging

from config import load_config
from .state import LinkStore
from .tg_bot import TGSide
from .vk_bot import VKSide
from .vk_user import detect_group_id


async def run() -> None:
    cfg = load_config()
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger(__name__)

    links = LinkStore()

    # Беседу из .env (если задана) показываем кандидатом в /link — связывать
    # пары теперь нужно командой /link, чтобы не воевать с ручным /unlink.
    if cfg.vk_peer_id:
        links.mark_seen_vk(cfg.vk_peer_id)
    if not cfg.admin_ids:
        log.warning("TG_ADMINS не задан — управлять парами (/link) будет некому. "
                    "Укажи свой TG user id в .env.")

    vk = VKSide(cfg, links)
    tg = TGSide(cfg, links)

    # Перекрёстные ссылки для двусторонней пересылки.
    tg.vk_api = vk.api
    vk.tg_bot = tg.bot

    # Для нативной заливки видео (TG->VK) нужен id сообщества — определим сами.
    if cfg.vk_user_token and not tg.vk_group_id:
        gid = await detect_group_id(cfg.vk_token)
        if gid:
            tg.vk_group_id = gid
            vk.vk_group_id = gid
            log.info("Определён group_id сообщества: %s", gid)
        else:
            log.warning("Не удалось определить group_id — видео пойдёт документом")
    if cfg.vk_user_token:
        log.info("User-токен подключён: видео будет нативным")

    tasks = [vk.start(), tg.start()]

    # Discord-наблюдатель (опционально): голосовые каналы -> уведомления и /vc
    # в привязанные пары моста (Telegram + VK). Управление в TG: /ds_connect.
    if cfg.discord_token:
        from .discord_bot import DiscordSide
        discord_side = DiscordSide(cfg, links)
        discord_side.tg_bot = tg.bot
        discord_side.vk_api = vk.api
        discord_side.vk_token = cfg.vk_token
        tg.discord = discord_side
        vk.discord = discord_side

        async def _run_discord() -> None:
            # Падение Discord (плохой токен, не включены интенты) не должно
            # ронять мост TG<->VK.
            try:
                await discord_side.start()
            except Exception:
                log.exception("Discord-наблюдатель остановился — мост работает дальше")

        tasks.append(_run_discord())
        log.info("Discord-наблюдатель подключён")

    logging.getLogger(__name__).info("Мост VK <-> Telegram запускается...")
    await asyncio.gather(*tasks)


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
