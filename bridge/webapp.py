"""Веб-приложение «альты» — Telegram Mini App (команда /alt web).

Открывается из чата кнопкой-ссылкой вида
``https://t.me/<bot>/<app>?startapp=<token>`` (deep-link к Mini App работает и в
группах, в отличие от inline-кнопок web_app). В ``startapp`` зашит ПОДПИСАННЫЙ
токен с tg_chat_id — так страница знает, альты какого чата показывать, и токен
нельзя подделать (HMAC на токене бота). Дополнительно при чтении/отправке
проверяем подпись Telegram WebApp ``initData`` — что запрос от настоящего юзера.

Превью и аватарки НЕ хранятся на диске: байты тянутся по запросу (file_id миниатюры
в TG / url превью в VK; аватарка автора — getUserProfilePhotos или VK photo_100) и
кешируются только в памяти с TTL.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qsl

import aiohttp
from aiohttp import web

from .media import _download_tg

log = logging.getLogger(__name__)

_TOKEN_TTL = 7 * 24 * 3600   # сколько живёт ссылка из /alt web
_CACHE_TTL = 600             # сколько держим картинку в памяти (сек)
_CACHE_MAX = 256             # максимум картинок в памяти


# --- подписанный токен чата (внутри startapp) -------------------------------

def _sign_chat(secret: bytes, chat_id: int, exp: int) -> str:
    msg = f"{chat_id}.{exp}".encode()
    sig = hmac.new(secret, msg, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(msg + sig).rstrip(b"=").decode()


def _verify_chat(secret: bytes, token: str) -> int | None:
    """Вернуть chat_id из токена, если подпись валидна и срок не вышел, иначе None."""
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except Exception:  # noqa: BLE001
        return None
    if len(raw) < 17:
        return None
    msg, sig = raw[:-16], raw[-16:]
    good = hmac.new(secret, msg, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(sig, good):
        return None
    try:
        chat_s, exp_s = msg.decode().split(".")
        chat_id, exp = int(chat_s), int(exp_s)
    except Exception:  # noqa: BLE001
        return None
    if exp < time.time():
        return None
    return chat_id


def _verify_init_data(bot_token: str, init_data: str) -> dict | None:
    """Проверить подпись Telegram WebApp initData. Вернуть поля (dict) или None."""
    if not init_data:
        return None
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:  # noqa: BLE001
        return None
    recv = data.pop("hash", None)
    if not recv:
        return None
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, recv):
        return None
    return data


class WebAppServer:
    """aiohttp-сервер Mini App: список альтов, превью, аватарки, отправка в чат."""

    def __init__(self, cfg, links, alts, bot_username: str):
        self.cfg = cfg
        self.links = links
        self.alts = alts
        self.bot_username = bot_username
        self._secret = cfg.tg_token.encode()
        self._img_cache: dict[str, tuple[float, bytes, str]] = {}
        self._vk_user_cache: dict[int, dict] = {}

    # --- ссылка для кнопки /alt web -----------------------------------------

    def build_start_url(self, chat_id: int) -> str:
        token = _sign_chat(self._secret, chat_id, int(time.time()) + _TOKEN_TTL)
        return (f"https://t.me/{self.bot_username}/"
                f"{self.cfg.webapp_bot_app}?startapp={token}")

    # --- авторизация запросов -----------------------------------------------

    def _chat_from_init(self, request) -> int | None:
        """chat_id из подписанного initData (заголовок X-Init-Data) → start_param."""
        data = _verify_init_data(self.cfg.tg_token,
                                 request.headers.get("X-Init-Data", ""))
        if data is None:
            # fallback для отладки/прямого открытия: токен в ?t=
            return _verify_chat(self._secret, request.query.get("t", ""))
        return _verify_chat(self._secret, data.get("start_param", ""))

    def _chat_from_token(self, request) -> int | None:
        """chat_id из ?t= (для <img>-эндпоинтов, куда заголовок не повесить)."""
        return _verify_chat(self._secret, request.query.get("t", ""))

    # --- кеш картинок в памяти ----------------------------------------------

    def _cache_get(self, key: str):
        item = self._img_cache.get(key)
        if not item:
            return None
        ts, data, ct = item
        if time.time() - ts > _CACHE_TTL:
            self._img_cache.pop(key, None)
            return None
        return data, ct

    def _cache_put(self, key: str, data: bytes, ct: str) -> None:
        if len(self._img_cache) >= _CACHE_MAX:
            # выкинем самый старый
            oldest = min(self._img_cache, key=lambda k: self._img_cache[k][0])
            self._img_cache.pop(oldest, None)
        self._img_cache[key] = (time.time(), data, ct)

    async def _fetch_url(self, url: str):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url) as r:
                    if r.status != 200:
                        return None
                    return await r.read(), r.headers.get("Content-Type", "image/jpeg")
        except Exception:  # noqa: BLE001
            log.debug("webapp: не удалось скачать %s", url, exc_info=True)
            return None

    async def _tg_file_bytes(self, file_id: str):
        try:
            return await _download_tg(self.alts.tg_bot, file_id), "image/jpeg"
        except Exception:  # noqa: BLE001
            log.debug("webapp: не удалось скачать TG-файл", exc_info=True)
            return None

    # --- автор альта (имя + аватарка) ---------------------------------------

    async def _vk_user(self, user_id: int) -> dict:
        if user_id in self._vk_user_cache:
            return self._vk_user_cache[user_id]
        info = {"name": f"id{user_id}", "photo": None}
        try:
            users = await self.alts.vk_api.users.get(
                user_ids=[user_id], fields=["photo_100"])
            if users:
                u = users[0]
                info["name"] = f"{u.first_name} {u.last_name}".strip()
                info["photo"] = getattr(u, "photo_100", None)
        except Exception:  # noqa: BLE001
            log.debug("webapp: VK users.get не удался для %s", user_id, exc_info=True)
        self._vk_user_cache[user_id] = info
        return info

    def _author_name(self, chat_id: int, origin: str, created_by: int) -> str:
        if not created_by:
            return ""
        if origin == "vk":
            cached = self._vk_user_cache.get(created_by)
            return cached["name"] if cached else ""
        return self.links.member_name(chat_id, created_by) or ""

    # --- эндпоинты -----------------------------------------------------------

    async def _index(self, request):
        return web.Response(text=_PAGE, content_type="text/html")

    async def _api_alts(self, request):
        chat_id = self._chat_from_init(request)
        if chat_id is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        rows = self.links.web_alts(chat_id)
        # имена VK-авторов подтянем заранее (для TG берём из chat_members без API)
        vk_ids = {r["created_by"] for r in rows
                  if r["origin"] == "vk" and r["created_by"]}
        for uid in vk_ids:
            await self._vk_user(uid)
        items = [{
            "id": r["id"],
            "name": r["name"] or "",
            "created_at": r["created_at"] or 0,
            "author": self._author_name(chat_id, r["origin"], r["created_by"]),
            "has_preview": bool(r["has_thumb"]),
            "has_avatar": bool(r["created_by"]),
        } for r in rows]
        return web.json_response({"alts": items})

    async def _load_alt(self, request, by_token: bool):
        chat_id = self._chat_from_token(request) if by_token else self._chat_from_init(request)
        if chat_id is None:
            return None, web.json_response({"error": "unauthorized"}, status=401)
        try:
            alt_id = int(request.match_info["alt_id"])
        except (KeyError, ValueError):
            return None, web.json_response({"error": "bad_id"}, status=400)
        row = self.links.get_alt_by_id(alt_id)
        if not row or row["tg_chat_id"] != chat_id:
            return None, web.json_response({"error": "not_found"}, status=404)
        return row, None

    async def _api_preview(self, request):
        row, err = await self._load_alt(request, by_token=True)
        if err is not None:
            return err
        key = f"thumb:{row['id']}"
        cached = self._cache_get(key)
        if cached is None:
            thumb = row["thumb"]
            if not thumb:
                return web.Response(status=404)
            res = (await self._fetch_url(thumb) if thumb.startswith("http")
                   else await self._tg_file_bytes(thumb))
            if res is None:
                return web.Response(status=404)
            self._cache_put(key, *res)
            cached = res
        data, ct = cached
        return web.Response(body=data, content_type=ct.split(";")[0],
                            headers={"Cache-Control": "private, max-age=600"})

    async def _api_avatar(self, request):
        row, err = await self._load_alt(request, by_token=True)
        if err is not None:
            return err
        created_by = row["created_by"]
        if not created_by:
            return web.Response(status=404)
        key = f"avatar:{row['origin']}:{created_by}"
        cached = self._cache_get(key)
        if cached is None:
            res = await self._avatar_bytes(row["origin"], created_by)
            if res is None:
                return web.Response(status=404)
            self._cache_put(key, *res)
            cached = res
        data, ct = cached
        return web.Response(body=data, content_type=ct.split(";")[0],
                            headers={"Cache-Control": "private, max-age=600"})

    async def _avatar_bytes(self, origin: str, user_id: int):
        if origin == "vk":
            info = await self._vk_user(user_id)
            url = info.get("photo")
            return await self._fetch_url(url) if url else None
        # TG: профильное фото автора (если приватность позволяет боту его видеть)
        try:
            photos = await self.alts.tg_bot.get_user_profile_photos(
                user_id=user_id, limit=1)
        except Exception:  # noqa: BLE001
            return None
        if not photos.total_count or not photos.photos:
            return None
        sizes = photos.photos[0]
        if not sizes:
            return None
        return await self._tg_file_bytes(sizes[0].file_id)

    async def _api_send(self, request):
        chat_id = self._chat_from_init(request)
        if chat_id is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
            alt_id = int(body.get("alt_id"))
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "bad_request"}, status=400)
        row = self.links.get_alt_by_id(alt_id)
        if not row or row["tg_chat_id"] != chat_id:
            return web.json_response({"error": "not_found"}, status=404)
        try:
            ok = await self.alts.send_both(row)
        except Exception:  # noqa: BLE001
            log.exception("webapp: отправка альта «%s» не удалась", row["name"])
            ok = False
        return web.json_response({"ok": bool(ok)})

    # --- запуск --------------------------------------------------------------

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/api/alts", self._api_alts)
        app.router.add_get("/api/preview/{alt_id}", self._api_preview)
        app.router.add_get("/api/avatar/{alt_id}", self._api_avatar)
        app.router.add_post("/api/send", self._api_send)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.cfg.webapp_host, self.cfg.webapp_port)
        await site.start()
        log.info("Веб-приложение альтов слушает %s:%s (Mini App %s, %s)",
                 self.cfg.webapp_host, self.cfg.webapp_port,
                 self.cfg.webapp_bot_app, self.cfg.webapp_public_url)
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await runner.cleanup()


_PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>Альты</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root{
    --bg: var(--tg-theme-bg-color, #17212b);
    --sec: var(--tg-theme-secondary-bg-color, #232e3c);
    --text: var(--tg-theme-text-color, #fff);
    --hint: var(--tg-theme-hint-color, #8a9aa9);
    --link: var(--tg-theme-link-color, #6ab3f3);
    --btn: var(--tg-theme-button-color, #50a8eb);
    --btn-text: var(--tg-theme-button-text-color, #fff);
  }
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
       background:var(--bg);color:var(--text);padding:10px 10px 24px}
  .filters{position:sticky;top:0;z-index:5;background:var(--bg);padding-bottom:8px}
  input{width:100%;border:none;border-radius:10px;padding:11px 12px;font-size:15px;
        background:var(--sec);color:var(--text);outline:none}
  input::placeholder{color:var(--hint)}
  .dates{display:flex;gap:8px;margin-top:8px}
  .dates label{flex:1;font-size:11px;color:var(--hint)}
  .dates input{margin-top:3px}
  .count{font-size:12px;color:var(--hint);margin:10px 2px 6px}
  .list{display:flex;flex-direction:column;gap:8px}
  .card{display:flex;align-items:center;gap:10px;background:var(--sec);
        border-radius:12px;padding:8px;cursor:pointer;transition:opacity .15s,transform .05s}
  .card:active{transform:scale(.985)}
  .card.sending{opacity:.5;pointer-events:none}
  .thumb{width:88px;height:62px;flex:none;border-radius:8px;object-fit:cover;
         background:var(--bg);display:flex;align-items:center;justify-content:center;
         font-size:24px;color:var(--hint)}
  .meta{flex:1;min-width:0}
  .name{font-size:15px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .date{font-size:12px;color:var(--hint);margin-top:3px}
  .author{flex:none;display:flex;flex-direction:column;align-items:center;width:46px;gap:3px}
  .ava{width:34px;height:34px;border-radius:50%;object-fit:cover;background:var(--btn);
       display:flex;align-items:center;justify-content:center;color:var(--btn-text);
       font-size:14px;font-weight:600;overflow:hidden}
  .ava-name{font-size:9px;color:var(--hint);max-width:46px;white-space:nowrap;
            overflow:hidden;text-overflow:ellipsis;text-align:center}
  .empty{color:var(--hint);text-align:center;padding:40px 12px}
  .toast{position:fixed;left:50%;bottom:18px;transform:translateX(-50%) translateY(20px);
         background:var(--btn);color:var(--btn-text);padding:10px 18px;border-radius:20px;
         font-size:14px;opacity:0;transition:.25s;pointer-events:none;z-index:20}
  .toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
</style>
</head>
<body>
  <div class="filters">
    <input id="q" type="text" placeholder="🔍 Поиск по названию…">
    <div class="dates">
      <label>С даты<input id="from" type="date"></label>
      <label>По дату<input id="to" type="date"></label>
    </div>
  </div>
  <div class="count" id="count"></div>
  <div class="list" id="list"></div>
  <div class="empty" id="empty" style="display:none">Здесь пока нет альтов.</div>
  <div class="toast" id="toast"></div>

<script>
const tg = window.Telegram ? window.Telegram.WebApp : null;
if (tg) { tg.ready(); tg.expand(); }
const initData = tg ? tg.initData : "";
const token = tg && tg.initDataUnsafe ? (tg.initDataUnsafe.start_param || "") : "";

const listEl = document.getElementById("list");
const emptyEl = document.getElementById("empty");
const countEl = document.getElementById("count");
const qEl = document.getElementById("q");
const fromEl = document.getElementById("from");
const toEl = document.getElementById("to");
const toastEl = document.getElementById("toast");
let ALTS = [];

function toast(msg, ok=true){
  toastEl.textContent = msg;
  toastEl.style.background = ok ? "" : "#c0392b";
  toastEl.classList.add("show");
  setTimeout(()=>toastEl.classList.remove("show"), 1800);
}
function fmtDate(ts){
  if(!ts) return "";
  const d = new Date(ts*1000);
  return d.toLocaleDateString("ru-RU", {day:"2-digit",month:"2-digit",year:"numeric"});
}
function initial(name){ return (name||"?").trim().charAt(0).toUpperCase() || "?"; }

function render(){
  const q = qEl.value.trim().toLowerCase();
  const from = fromEl.value ? new Date(fromEl.value).getTime()/1000 : null;
  const to = toEl.value ? (new Date(toEl.value).getTime()/1000 + 86399) : null;
  const items = ALTS.filter(a=>{
    if(q && !(a.name||"").toLowerCase().includes(q)) return false;
    if(from!==null && (a.created_at||0) < from) return false;
    if(to!==null && (a.created_at||0) > to) return false;
    return true;
  });
  listEl.innerHTML = "";
  countEl.textContent = items.length ? ("Найдено: " + items.length) : "";
  emptyEl.style.display = (ALTS.length===0 || items.length===0) ? "block" : "none";
  emptyEl.textContent = ALTS.length===0 ? "Здесь пока нет альтов." : "Ничего не найдено.";
  for(const a of items) listEl.appendChild(card(a));
}

function card(a){
  const el = document.createElement("div");
  el.className = "card";

  let thumb;
  if(a.has_preview){
    thumb = document.createElement("img");
    thumb.className = "thumb";
    thumb.loading = "lazy";
    thumb.src = "/api/preview/" + a.id + "?t=" + encodeURIComponent(token);
    thumb.onerror = ()=>{ const ph=document.createElement("div"); ph.className="thumb"; ph.textContent="🎬"; el.replaceChild(ph, thumb); };
  } else {
    thumb = document.createElement("div");
    thumb.className = "thumb"; thumb.textContent = "🎬";
  }
  el.appendChild(thumb);

  const meta = document.createElement("div");
  meta.className = "meta";
  const name = document.createElement("div");
  name.className = "name"; name.textContent = a.name || "без названия";
  const date = document.createElement("div");
  date.className = "date"; date.textContent = "📅 " + (fmtDate(a.created_at) || "—");
  meta.appendChild(name); meta.appendChild(date);
  el.appendChild(meta);

  const author = document.createElement("div");
  author.className = "author";
  const ava = document.createElement("div");
  ava.className = "ava";
  if(a.has_avatar){
    const img = document.createElement("img");
    img.className = "ava"; img.loading="lazy";
    img.src = "/api/avatar/" + a.id + "?t=" + encodeURIComponent(token);
    img.onerror = ()=>{ ava.textContent = initial(a.author); img.replaceWith(ava); };
    author.appendChild(img);
  } else {
    ava.textContent = initial(a.author);
    author.appendChild(ava);
  }
  if(a.author){
    const an = document.createElement("div");
    an.className = "ava-name"; an.textContent = a.author;
    author.appendChild(an);
  }
  el.appendChild(author);

  el.onclick = ()=>send(a, el);
  return el;
}

async function send(a, el){
  if(el.classList.contains("sending")) return;
  el.classList.add("sending");
  if(tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred("light");
  try{
    const r = await fetch("/api/send", {
      method:"POST",
      headers:{"Content-Type":"application/json","X-Init-Data":initData},
      body: JSON.stringify({alt_id: a.id})
    });
    const j = await r.json();
    if(r.ok && j.ok) toast("✅ Отправлено: " + (a.name||""));
    else toast("⚠️ Не удалось отправить", false);
  }catch(e){ toast("⚠️ Ошибка сети", false); }
  finally{ el.classList.remove("sending"); }
}

async function load(){
  try{
    const r = await fetch("/api/alts", {headers:{"X-Init-Data":initData}});
    if(!r.ok){ emptyEl.style.display="block"; emptyEl.textContent="Нет доступа."; return; }
    const j = await r.json();
    ALTS = j.alts || [];
    render();
  }catch(e){ emptyEl.style.display="block"; emptyEl.textContent="Ошибка загрузки."; }
}

qEl.addEventListener("input", render);
fromEl.addEventListener("change", render);
toEl.addEventListener("change", render);
load();
</script>
</body>
</html>"""
