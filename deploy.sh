#!/usr/bin/env bash
# Auto-deploy: синхронизируем рабочую копию с origin/main и перезапускаем бота,
# если РАБОТАЮЩИЙ код устарел.
#
# Решение о рестарте принимается по штампу коммита, который реально запущен
# (.deployed_sha), а НЕ по сравнению local-vs-remote. Это важно: правки часто
# коммитятся прямо в этой рабочей копии, тогда после push local уже == remote,
# и старая проверка «local != remote» ошибочно решала бы, что всё актуально, и
# не перезапускала сервис. Штамп же меняется только при фактическом рестарте.
set -euo pipefail
cd /opt/tg-vk-bot

STAMP=/opt/tg-vk-bot/.deployed_sha

git fetch --quiet origin main
git reset --hard --quiet origin/main
HEAD=$(git rev-parse HEAD)
RUNNING=$(cat "$STAMP" 2>/dev/null || true)

if [ "$HEAD" = "$RUNNING" ]; then
  logger -t tgvk-deploy "up-to-date ($HEAD)"
  exit 0
fi

logger -t tgvk-deploy "deploying ${RUNNING:-<unknown>} -> $HEAD"
# Переустанавливаем зависимости, только если requirements.txt изменился
# относительно запущенного коммита (при неизвестном RUNNING — пропускаем:
# окружение уже рабочее).
if [ -n "$RUNNING" ] && \
   ! git diff --quiet "$RUNNING" "$HEAD" -- requirements.txt 2>/dev/null; then
  /opt/tg-vk-bot/.venv/bin/pip install -q -r requirements.txt \
    || logger -t tgvk-deploy "pip install failed"
fi
systemctl restart tgvk-bot
echo "$HEAD" > "$STAMP"
logger -t tgvk-deploy "deployed $HEAD, service restarted"
