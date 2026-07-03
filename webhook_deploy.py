#!/usr/bin/env python3
"""GitHub webhook auto-deploy for tgvk-bot with Telegram notifications.

On push to main: pulls origin/main, reinstalls deps if requirements changed,
restarts the service, and reports progress to a personal Telegram chat.
On any failure: sends the detailed command output to the same chat.
"""
import hashlib
import hmac
import html
import json
import logging
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = "/opt/tg-vk-bot"
ENV_FILE = APP_DIR + "/.env"
VENV_PIP = APP_DIR + "/.venv/bin/pip"
SERVICE = "tgvk-bot"
BRANCH = "refs/heads/main"
# Коммит, который РЕАЛЬНО запущен (общий штамп с deploy.sh). Нужен потому, что
# правки часто коммитятся прямо в этой рабочей копии: тогда к приходу вебхука
# HEAD уже == origin/main, и сравнение before-vs-after ошибочно решило бы, что
# деплоить нечего. Штамп меняется только при фактическом рестарте сервиса.
STAMP = APP_DIR + "/.deployed_sha"

SECRET = os.environ.get("WEBHOOK_SECRET", "").encode()
PORT = int(os.environ.get("WEBHOOK_PORT", "9000"))
NOTIFY_CHAT_ID = os.environ.get("NOTIFY_CHAT_ID", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("webhook")


def _read_env(path):
    data = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip().strip("\"\x27")
    except OSError:
        pass
    return data


_ENV = _read_env(ENV_FILE)
TG_TOKEN = _ENV.get("TG_BOT_TOKEN", "")
TG_API_BASE = (_ENV.get("TG_API_URL", "") or "https://api.telegram.org").rstrip("/")


def run(cmd):
    r = subprocess.run(cmd, shell=True, cwd=APP_DIR, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("$ %s\n%s\n%s" % (cmd, r.stdout, r.stderr))
    return r.stdout.strip()


def tg(text, msg_id=None):
    """Send or edit a Telegram message. Returns message_id."""
    if not (TG_TOKEN and NOTIFY_CHAT_ID):
        return None
    method = "editMessageText" if msg_id else "sendMessage"
    url = "%s/bot%s/%s" % (TG_API_BASE, TG_TOKEN, method)
    payload = {"chat_id": NOTIFY_CHAT_ID, "text": text[:4000],
               "parse_mode": "HTML", "disable_web_page_preview": True}
    if msg_id:
        payload["message_id"] = msg_id
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
        return body.get("result", {}).get("message_id")
    except Exception as e:  # noqa: BLE001
        log.error("telegram send failed: %s", e)
        return None


def esc(s):
    return html.escape(s or "")


def _read_stamp():
    try:
        with open(STAMP) as f:
            return f.read().strip()
    except OSError:
        return ""


def _write_stamp(sha):
    try:
        with open(STAMP, "w") as f:
            f.write(sha + "\n")
    except OSError as e:  # noqa: BLE001
        log.error("stamp write failed: %s", e)


def _valid(ref):
    """Есть ли такой коммит локально (штамп мог указывать на неизвестный SHA)."""
    r = subprocess.run("git cat-file -e " + ref + "^{commit}", shell=True,
                       cwd=APP_DIR, capture_output=True, text=True)
    return r.returncode == 0


def deploy():
    run("git fetch --quiet origin main")
    after = run("git rev-parse origin/main")
    running = _read_stamp()
    if after == running:
        tg("ℹ️ <b>Push в main</b> — уже актуально, деплой не требуется.")
        return

    base = running if (running and _valid(running)) else ""
    fmt = chr(37)+"h "+chr(37)+"s ("+chr(37)+"an)"
    if base:
        commits = run("git --no-pager log --pretty=format:'" + fmt + "' " + base + ".." + after)
        diff_stat = run("git --no-pager diff --stat " + base + " " + after)
        changed = run("git --no-pager diff --name-only " + base + " " + after)
    else:
        # Штамп неизвестен (первый деплой после ввода штампа) — показываем
        # только вершину и не трогаем зависимости.
        commits = run("git --no-pager log -1 --pretty=format:'" + fmt + "' " + after)
        diff_stat = ""
        changed = ""

    head = ("\U0001f4bb <b>Новый коммит в main</b>\n<blockquote>%s</blockquote>\n\n"
            "<b>Изменения:</b>\n<blockquote>%s</blockquote>" % (esc(commits), esc(diff_stat)))
    mid = tg(head + "\n\n<b>Статус:</b> обновляю код…")

    run("git reset --hard origin/main")

    if "requirements.txt" in changed.split("\n"):
        tg(head + "\n\n<b>Статус:</b> ставлю зависимости…", mid)
        run(VENV_PIP + " install -q -r requirements.txt")

    tg(head + "\n\n<b>Статус:</b> перезапускаю сервис…", mid)
    run("systemctl restart " + SERVICE)
    _write_stamp(after)
    tg(head + "\n\n<b>Статус:</b> ✅ задеплоено (%s)" % after[:7], mid)
    log.info("deployed %s -> %s", (base or "<unknown>")[:7], after[:7])


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code, msg):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(msg.encode())

    def do_GET(self):
        self._reply(200, "ok")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        if not SECRET:
            return self._reply(500, "secret not configured")
        expected = "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(self.headers.get("X-Hub-Signature-256", ""), expected):
            log.warning("bad signature from %s", self.client_address[0])
            return self._reply(403, "bad signature")
        event = self.headers.get("X-GitHub-Event", "")
        if event == "ping":
            return self._reply(200, "pong")
        if event != "push":
            return self._reply(200, "ignored")
        try:
            payload = json.loads(body)
        except ValueError:
            return self._reply(400, "bad json")
        if payload.get("ref") != BRANCH:
            return self._reply(200, "ignored ref")
        try:
            deploy()
            return self._reply(200, "deployed")
        except Exception as e:  # noqa: BLE001
            detail = str(e)
            log.error("deploy failed: %s", detail)
            tg("❌ <b>Ошибка деплоя</b>\n<pre>%s</pre>" % esc(detail[:3500]))
            return self._reply(500, "deploy error")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if not SECRET:
        log.error("WEBHOOK_SECRET not set")
        sys.exit(1)
    log.info("listening on :%d, notify chat=%s, api=%s", PORT, NOTIFY_CHAT_ID, TG_API_BASE)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
