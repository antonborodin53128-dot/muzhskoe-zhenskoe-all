"""«Финал» — общий конкурс Man vs Woman.
5 категорий (Фильмы, Поиск предмета, Загадки, Музыка, Математика): в каждой сначала отвечает Man, потом Woman.
Ведущий выбирает категорию, выбирает, кто отвечает, и жмёт «Зачёт» / «Не зачёт». Правильный ответ виден только ведущему,
гостям он показывается после оценки. За верный ответ — 1 очко. Если счёт равный — «Бонус игра» (два кубика, «21 очко», бросок тряской телефона).
Тексты вопросов и ответов — в блоке CATS ниже, их можно править без изменения остального кода.
"""
import os, re, json, time, random, secrets, tempfile, hashlib
from threading import RLock
from flask import Flask, request, make_response, send_from_directory, abort
from flask_socketio import SocketIO, emit

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(HERE, "final_audio")
STATIC_DIR = os.path.join(HERE, "final_static")
STATE_FILE = os.environ.get("FINAL_STATE_FILE", os.path.join(tempfile.gettempdir(), "final_state.json"))
TTL = 6 * 3600

# ----------------------------------------------------------------------
# Содержимое конкурса. time — секунд на ответ (0 — без таймера).
# ----------------------------------------------------------------------
CATS = [
    {"id": "film", "title": "Фильмы", "icon": "🎬", "time": 0,
     "man": {"q": "Как зовут парня Барби?", "a": "Кен"},
     "woman": {"q": "Какое имя у Железного человека в фильме?", "a": "Тони Старк"}},
    {"id": "find", "title": "Поиск предмета", "icon": "🔎", "time": 25,
     "man": {"q": "Найди предмет в форме треугольника", "a": "Любой предмет-треугольник"},
     "woman": {"q": "Найди предмет в форме цилиндра", "a": "Любой предмет-цилиндр"}},
    {"id": "riddle", "title": "Загадки", "icon": "🧩", "time": 15,
     "man": {"q": "Тихо сзади подошёл, дважды всунул и пошёл", "a": "Тапочки"},
     "woman": {"q": "В тёмной комнате, на белой простыне, два часа удовольствия", "a": "Кино (принимать и «киносеанс»)"}},
    {"id": "music", "title": "Музыка", "icon": "🎧", "time": 0,
     "man": {"q": "Угадай песню. Запись звучит задом наперёд", "a": "Eurythmics — Sweet Dreams (Are Made of This)", "qfile": "m_q.mp3", "afile": "m_a.mp3"},
     "woman": {"q": "Угадай песню. Запись звучит задом наперёд", "a": "Звери — Районы-кварталы", "qfile": "w_q.mp3", "afile": "w_a.mp3"}},
    {"id": "math", "title": "Математика", "icon": "➗", "time": 25,
     "man": {"q": "15% от 240", "a": "36"},
     "woman": {"q": "35% от 120", "a": "42"}},
]
CAT = {c["id"]: c for c in CATS}
WHO = ("man", "woman")
NAME = {"man": "Man", "woman": "Woman"}
AUDIO_NAME = re.compile(r"^[mw]_[qa]\.mp3$")

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading", ping_interval=15, ping_timeout=25)
lock = RLock()
BOOT = secrets.token_hex(4)


def fresh():
    return {"rev": 0, "touched": time.time(), "mode": "menu", "cur": None, "pick": None, "dice": None,
            "results": {c["id"]: {"man": None, "woman": None} for c in CATS}}


state = fresh()


def new_dice():
    return {"turn": None, "armed": False, "shaking": False, "winner": None, "seq": 0, "last": None, "last_who": None, "ev": [],
            "p": {w: {"rolls": [], "total": 0, "stand": False, "bust": False} for w in WHO}}


def dice_recalc(d):
    """Состояние игроков и победитель считаются заново из журнала бросков (так «отменить последнее» не ломает счёт)."""
    d["p"] = {w: {"rolls": [], "total": 0, "stand": False, "bust": False} for w in WHO}
    forced = False
    for kind, w, v in d["ev"]:
        if kind == "f":
            forced = True; continue
        p = d["p"][w]
        if kind == "r":
            v = sum(v); p["rolls"].append(v); p["total"] += v
            if p["total"] >= 21:
                p["stand"] = True; p["bust"] = p["total"] > 21
        else:
            p["stand"] = True
    m, f = d["p"]["man"], d["p"]["woman"]
    if m["bust"] or f["bust"]:
        d["winner"] = "woman" if m["bust"] else "man"
    elif m["stand"] and f["stand"]:
        d["winner"] = "draw" if m["total"] == f["total"] else ("man" if m["total"] > f["total"] else "woman")
    elif forced:                      # «Завершить игру»: победитель по очкам, которые уже есть
        d["winner"] = "draw" if m["total"] == f["total"] else ("man" if m["total"] > f["total"] else "woman")
    else:
        d["winner"] = None
    d["last_who"] = None; d["last"] = None
    for kind, w, v in reversed(d["ev"]):
        if kind == "r":
            d["last_who"], d["last"] = w, list(v); break


def score_locked():
    out = {"man": 0, "woman": 0}
    for r in state["results"].values():
        for w in WHO:
            if r[w] == "ok":
                out[w] += 1
    return out


def snapshot_locked():
    state["rev"] += 1
    cur, pub = state["cur"], None
    if cur:
        side = CAT[cur["cat"]][cur["who"]]
        pub = dict(cur)
        pub.update(q=side["q"], title=CAT[cur["cat"]]["title"], time=CAT[cur["cat"]]["time"],
                   qfile=side.get("qfile"), afile=side.get("afile"))
        if cur["revealed"]:
            pub["a"] = side["a"]
    return {"rev": state["rev"], "boot": BOOT, "ver": VER, "server_now": time.time(), "mode": state["mode"], "pick": state.get("pick"), "cur": pub,
            "dice": state["dice"], "results": state["results"], "score": score_locked(),
            "cats": [{"id": c["id"], "title": c["title"], "icon": c["icon"], "time": c["time"]} for c in CATS]}


def publish(snap):
    socketio.emit("state", snap)


def save_locked():
    state["touched"] = time.time()
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def load_state():
    global state
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
        if time.time() - s.get("touched", 0) < TTL and set(s.get("results", {})) == set(CAT):
            base = fresh(); base.update(s); state = base
    except Exception:
        pass


load_state()


def apply(c):
    a = c.get("a")
    if a == "open":
        if c.get("cat") in CAT:
            state["mode"], state["cur"], state["pick"] = "pick", None, c["cat"]
    elif a == "pick":
        if c.get("cat") in CAT and c.get("who") in WHO:
            state["mode"], state["pick"] = "q", None
            state["cur"] = {"cat": c["cat"], "who": c["who"], "started_at": None, "revealed": False, "audio": {"kind": None, "n": 0}}
    elif a == "back":
        state["mode"], state["cur"], state["pick"] = "menu", None, None
    elif state["mode"] == "q" and state["cur"]:
        cur = state["cur"]
        if a == "mark" and c.get("r") in ("ok", "bad"):
            state["results"][cur["cat"]][cur["who"]] = c["r"]
            cur["revealed"] = True
            cur["audio"] = {"kind": None, "n": cur["audio"]["n"] + 1}
        elif a == "to_pick":                 # вернуться к выбору «Man / Woman» в той же категории
            state["mode"], state["pick"], state["cur"] = "pick", cur["cat"], None
        elif a == "reveal":
            cur["revealed"] = True
        elif a == "timer":
            cur["started_at"] = time.time()
        elif a == "audio" and c.get("k") in ("q", "a", "stop"):
            cur["audio"] = {"kind": None if c["k"] == "stop" else c["k"], "n": cur["audio"]["n"] + 1}
    if a == "dice_start":
        state["mode"], state["cur"], state["pick"], state["dice"] = "dice", None, None, new_dice()
    elif state["mode"] == "dice" and state["dice"]:
        d = state["dice"]
        if a == "dice_pick":
            w = c.get("who")
            if w in WHO and d["winner"] is None and not d["p"][w]["stand"]:
                d["turn"], d["armed"], d["shaking"] = w, False, False
            elif w is None:
                d["turn"], d["armed"], d["shaking"] = None, False, False
        elif a == "dice_arm" and d["turn"] and d["winner"] is None:
            d["armed"], d["shaking"] = True, False
        elif a == "dice_shake" and d["armed"] and d["winner"] is None:
            d["shaking"] = bool(c.get("on", True))
        elif a == "roll" and d["turn"] and d["winner"] is None:
            v = [random.randint(1, 6), random.randint(1, 6)]
            d["ev"].append(["r", d["turn"], v]); d["seq"] += 1
            d["turn"], d["armed"], d["shaking"] = None, False, False
            dice_recalc(d)
        elif a == "stand" and d["turn"] and d["p"][d["turn"]]["total"] > 0 and d["winner"] is None:
            d["ev"].append(["s", d["turn"], 0]); d["turn"], d["armed"], d["shaking"] = None, False, False
            dice_recalc(d)
        elif a == "dice_finish" and d["winner"] is None:
            d["ev"].append(["f", "man", 0]); d["turn"], d["armed"], d["shaking"] = None, False, False
            dice_recalc(d)
        elif a == "undo" and d["ev"]:
            d["ev"].pop(); d["turn"], d["armed"], d["shaking"] = None, False, False
            dice_recalc(d)
    if a == "reset":
        fr = fresh(); fr["rev"] = state["rev"]; state.clear(); state.update(fr)


@socketio.on("connect")
def on_connect(*_):
    with lock:
        snap = snapshot_locked()
    emit("state", snap)


@socketio.on("disconnect")
def on_disconnect(*_):
    with lock:
        pass


@socketio.on("sync")
def on_sync(*_):
    with lock:
        snap = snapshot_locked()
    emit("state", snap)


@socketio.on("cmd")
def on_cmd(c):
    if not isinstance(c, dict):
        return
    with lock:
        apply(c)
        save_locked()
        snap = snapshot_locked()
    publish(snap)          # рассылаем только после выхода из блокировки


def base_path():
    return (request.script_root or "") + "/"


def page(tpl):
    html = (tpl.replace("__VER__", VER).replace("__BASE__", base_path()).replace("__CSS__", FINAL_CSS)
            .replace("__ROLL_CSS__", ROLL_CSS).replace("__ROLL_JS__", ROLL_JS).replace("__NET_JS__", NET_JS)
            .replace("__CATS__", json.dumps(CATS, ensure_ascii=False).replace("</", "<\\/")))
    r = make_response(html)
    r.headers["Content-Type"] = "text/html; charset=utf-8"
    return r


@app.after_request
def no_cache(r):
    if request.path.rstrip("/").split("/")[-1] in ("", "screen", "healthz") or request.path.endswith("/"):
        r.headers["Cache-Control"] = "no-store"
    return r


@app.route("/")
def control():
    return page(CONTROL_HTML)


@app.route("/screen")
def screen():
    return page(SCREEN_HTML)


@app.route("/healthz")
def healthz():
    r = make_response("ok"); r.headers["Cache-Control"] = "no-store"; return r


ICON_NAME = re.compile(r"^[0-9A-F]{4,5}\.svg$")


@app.route("/icons/<name>")
def icon(name):
    if not ICON_NAME.match(name):
        abort(404)
    return send_from_directory(os.path.join(STATIC_DIR, "icons"), name, mimetype="image/svg+xml", max_age=86400, conditional=True)


@app.route("/audio/<name>")
def audio(name):
    if not AUDIO_NAME.match(name):
        abort(404)
    return send_from_directory(AUDIO_DIR, name, mimetype="audio/mpeg", max_age=3600, conditional=True)


# ======================================================================
# Клиент: общий стиль, подключение, табло
# ======================================================================
ROLL_CSS = r""".roll{display:inline-flex;align-items:flex-start;line-height:1;--cell:1.08em;height:var(--cell);vertical-align:top;
  -webkit-mask-image:linear-gradient(transparent,#000 14%,#000 86%,transparent);mask-image:linear-gradient(transparent,#000 14%,#000 86%,transparent)}
.roll .d{display:inline-block;height:var(--cell);overflow:hidden}
.roll .s{display:flex;flex-direction:column;transition:transform var(--roll-ms,560ms) cubic-bezier(.22,1.18,.36,1)}
.roll .s>span{height:var(--cell);line-height:var(--cell);text-align:center}
.roll .sep{height:var(--cell);line-height:var(--cell);padding:0 .02em}
.roll .d.in{animation:digitIn .45s cubic-bezier(.2,.9,.3,1.2)}
@keyframes digitIn{from{transform:translateY(-.4em);opacity:0}}
"""
ROLL_JS = r"""/* ---------- Табло ----------
   setRoll(el, '642') — каждая цифра крутится к новому значению.
   Направление берётся из значения: больше — крутим вверх, меньше — вниз. */
function setRoll(el, text, opts){
  text = String(text);
  if (el._text === text) return;
  const old = el._text, chars = [...text];
  const pattern = chars.map(c => /\d/.test(c) ? 'd' : c).join('');
  const up = (opts && 'up' in opts) ? opts.up : (old == null || parseFloat(text.replace(':', '.')) >= parseFloat(String(old).replace(':', '.')));
  if (el._pattern !== pattern) {
    // число разрядов поменялось — пересобираем, старые цифры выравниваем по правому краю
    const oldDigits = old ? [...old].filter(c => /\d/.test(c)).map(Number) : [];
    const nd = chars.filter(c => /\d/.test(c)).length;
    el.classList.add('roll'); el.innerHTML = ''; el._cells = [];
    let di = 0;
    chars.forEach(c => {
      if (!/\d/.test(c)) { const sp = document.createElement('span'); sp.className = 'sep'; sp.textContent = c; el.appendChild(sp); return; }
      const d = document.createElement('span'); d.className = 'd';
      const st = document.createElement('span'); st.className = 's';
      st.innerHTML = '0123456789'.split('').concat('0').map(n => `<span>${n}</span>`).join('');
      d.appendChild(st); el.appendChild(d);
      const fromOld = oldDigits[oldDigits.length - (nd - di)];
      const start = fromOld != null ? fromOld : (opts && opts.from != null ? opts.from : 0);
      if (old != null && fromOld == null && !(opts && opts.from != null)) d.classList.add('in');
      st.style.transition = 'none'; st.style.transform = `translateY(calc(${-start} * var(--cell)))`;
      el._cells.push({st, cur: start}); di++;
    });
    el._pattern = pattern;
    void el.offsetWidth;
  }
  el._text = text;
  let di = 0;
  chars.forEach(c => { if (/\d/.test(c)) rollCell(el._cells[di++], +c, up, opts && opts.delay); });
}
function rollCell(cell, to, up, delay){
  const st = cell.st, from = cell.cur;
  if (to === from) return;
  st.style.transitionDelay = delay ? delay + 'ms' : '';
  const go = idx => { st.style.transition = ''; st.style.transform = `translateY(calc(${-idx} * var(--cell)))`; };
  const snap = idx => { st.style.transition = 'none'; st.style.transform = `translateY(calc(${-idx} * var(--cell)))`; void st.offsetWidth; };
  if (up && to < from && to === 0) {            // 9 → 0 вверх: докручиваем до нижнего «0» и тихо возвращаемся
    go(10);
    clearTimeout(cell.t); cell.t = setTimeout(() => { if (cell.cur === 0) snap(0); }, 700 + (delay || 0));
  } else if (!up && to > from && from === 0) {   // 0 → 9 вниз: встаём на нижний «0» и крутим вниз
    snap(10); go(to);
  } else go(to);
  cell.cur = to;
}
"""

FINAL_CSS = r"""
:root{
  --g:#2bf08a; --g-ink:#02140a; --g-bg:#06140e; --g-sf:#0d261b; --g-line:#1d4734; --g-soft:rgba(43,240,138,.16);
  --p:#ff4fa8; --p-ink:#22000f; --p-bg:#140710; --p-sf:#26101f; --p-line:#4e1d3b; --p-soft:rgba(255,79,168,.16);
  --chalk:#f4f3ef; --mist:#a3afa9; --danger:#ff8e9c; --danger-bg:#2d1419;
  --display:"Unbounded",system-ui,sans-serif; --ui:"Onest",system-ui,sans-serif; --digits:"Nunito",var(--display); --digits-w:900;
  --r-s:12px; --r-m:18px; --r-l:28px;
}
.m{--c:var(--g);--c-ink:var(--g-ink);--c-sf:var(--g-sf);--c-line:var(--g-line);--c-soft:var(--g-soft)}
.w{--c:var(--p);--c-ink:var(--p-ink);--c-sf:var(--p-sf);--c-line:var(--p-line);--c-soft:var(--p-soft)}
*{box-sizing:border-box}
html,body{margin:0;color:var(--chalk);font-family:var(--ui);-webkit-font-smoothing:antialiased;background:#0a0f0c}
body{min-height:100vh;min-height:100dvh;background:linear-gradient(100deg,var(--g-bg) 0 38%,#0d0b0c 50%,var(--p-bg) 62% 100%)}
button{font:inherit;color:inherit;cursor:pointer;-webkit-tap-highlight-color:transparent}
button:focus-visible,a:focus-visible{outline:3px solid var(--chalk);outline-offset:3px}
.num{font-family:var(--digits);font-weight:var(--digits-w);font-variant-numeric:tabular-nums}
.wordmark{display:inline-flex;align-items:center;gap:.5em;font-family:var(--display);font-weight:800}
.wordmark i{width:.62em;height:.62em;border-radius:50%;background:linear-gradient(90deg,var(--g) 50%,var(--p) 50%)}
.offline{position:fixed;left:0;right:0;top:0;z-index:100;padding:10px 16px;text-align:center;font-weight:600;background:var(--danger-bg);color:var(--danger);transform:translateY(-100%);transition:transform .25s}
.offline.on{transform:none}
.offbtn{margin-left:12px;border:1px solid currentColor;background:none;color:inherit;border-radius:999px;padding:4px 14px;font:inherit;font-weight:700}
[hidden]{display:none!important}
@media (prefers-reduced-motion:reduce){*,*:before,*:after{animation-duration:.01ms!important;transition-duration:.01ms!important}}
"""

NET_JS = r"""
const BASE = document.documentElement.dataset.base || '/';
const socket = io({path: BASE + 'socket.io', transports: ['websocket', 'polling'], tryAllTransports: true,
                   reconnectionDelay: 400, reconnectionDelayMax: 2500, timeout: 8000});
let S = null, clockOffset = 0, bootId = null, downSince = 0;
const offsets = [];
const offlineBar = document.getElementById('offline');
if (offlineBar) { const b = document.createElement('button'); b.className = 'offbtn'; b.textContent = 'Обновить страницу'; b.onclick = () => location.reload(); offlineBar.appendChild(b); }
const setOffline = on => { if (offlineBar) offlineBar.classList.toggle('on', on); };
socket.on('connect', () => { downSince = 0; setOffline(false); });
socket.on('disconnect', () => { if (!downSince) downSince = performance.now(); setOffline(true); });
socket.on('connect_error', () => { if (!downSince) downSince = performance.now(); setOffline(true); });
// После выкладки новой версии открытая страница сама обновляется (иначе у неё остаётся старый код).
function checkVer(s){
  const mine = document.documentElement.dataset.ver;
  if (!s.ver || !mine || s.ver === mine) return false;
  try { const t = +sessionStorage.getItem('finalReloadAt') || 0; if (Date.now() - t < 8000) return false; sessionStorage.setItem('finalReloadAt', String(Date.now())); } catch (e) {}
  location.reload(); return true;
}
socket.on('state', s => {
  if (checkVer(s)) return;
  if (s.boot !== bootId) { bootId = s.boot; S = null; offsets.length = 0; }
  else if (S && s.rev < S.rev) return;
  offsets.push(s.server_now - Date.now() / 1000); if (offsets.length > 12) offsets.shift();
  clockOffset = Math.max(...offsets);
  S = s; window.onState && window.onState(s);
});
const wake = () => { if (document.hidden) return; if (socket.connected) socket.emit('sync'); else { try { socket.connect(); } catch (e) {} } };
document.addEventListener('visibilitychange', wake); addEventListener('online', wake); addEventListener('focus', wake); addEventListener('pageshow', wake);
setInterval(() => { fetch(BASE + 'healthz', {cache: 'no-store'}).catch(() => {}); }, 240000);
setInterval(async () => {
  if (socket.connected || document.hidden) return;
  if (!downSince) downSince = performance.now();
  if (performance.now() - downSince < 20000) return;
  downSince = performance.now();
  try { const r = await fetch(BASE + 'healthz', {cache: 'no-store'}); if (r.ok && !socket.connected) location.reload(); } catch (e) {}
}, 5000);
const serverNow = () => Date.now() / 1000 + clockOffset;
const send = o => { if (socket.connected) socket.emit('cmd', o); };   // при обрыве команды не копим
window.__send = send;
const esc = v => String(v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const WN = {man: 'Man', woman: 'Woman'};
const cls = w => w === 'man' ? 'm' : 'w';
const other = w => w === 'man' ? 'woman' : 'man';
"""

# ======================================================================
# Пульт ведущего
# ======================================================================
CONTROL_HTML = r"""<!doctype html>
<html lang="ru" data-base="__BASE__" data-ver="__VER__">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0d0b0c"><title>Финал · пульт</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Onest:wght@400;600;800&family=Unbounded:wght@600;800;900&family=Nunito:wght@800;900&display=swap" rel="stylesheet">
<script src="https://cdn.socket.io/4.8.1/socket.io.min.js"></script>
<style>__CSS__
.app{max-width:520px;margin:0 auto;padding:16px 16px calc(28px + env(safe-area-inset-bottom));display:flex;flex-direction:column;gap:14px;min-height:100dvh}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px}
.top .wordmark{font-size:20px}
.link{color:var(--mist);font-weight:600;font-size:14px;text-decoration:underline;text-underline-offset:3px}
.board{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.side{border-radius:var(--r-m);padding:12px 16px;background:var(--c-sf);border:1px solid var(--c-line);display:flex;justify-content:space-between;align-items:center}
.side b{font-family:var(--display);color:var(--c)}
.side .num{font-size:40px;line-height:1}
h2{margin:6px 0 0;font-size:16px;font-weight:600;color:var(--mist)}
.cat{display:flex;align-items:center;justify-content:space-between;gap:10px;width:100%;text-align:left;padding:16px 18px;border-radius:var(--r-m);border:1px solid #3a3f3c;background:rgba(255,255,255,.05);font-size:19px;font-weight:700}
.cat .chips{display:flex;gap:6px}
.chip{font-style:normal;font-weight:800;font-size:13px;min-width:30px;text-align:center;padding:4px 8px;border-radius:999px;border:1px solid var(--c-line);color:var(--mist)}
.chip.ok{background:var(--c);border-color:var(--c);color:var(--c-ink)}
.chip.bad{background:var(--danger-bg);border-color:var(--danger);color:var(--danger)}
.cat.dice{border-style:dashed;justify-content:center}
.btn{display:block;width:100%;border:0;border-radius:var(--r-m);padding:18px;font-size:19px;font-weight:800;background:var(--c);color:var(--c-ink)}
.btn.quiet{background:transparent;border:1px solid #4a4f4c;color:var(--chalk);font-weight:600}
.btn.good{background:var(--g);color:var(--g-ink)}
.btn.bad{background:var(--danger);color:#2a0a10}
.btn.sel{outline:4px solid var(--chalk);outline-offset:2px}
.btn.hi{background:var(--chalk);color:#0b0f0d;font-size:21px;padding:20px}
.btn.hi.pulse{animation:hi 1.4s ease-in-out infinite}
@keyframes hi{0%,100%{box-shadow:0 0 0 0 rgba(244,243,239,.0)}50%{box-shadow:0 0 0 8px rgba(244,243,239,.28)}}
.btn:disabled{opacity:.4}
.btn:active:not(:disabled){transform:scale(.98)}
.two{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.qhead{font-family:var(--display);font-weight:800;font-size:18px;display:flex;justify-content:space-between;gap:10px}
.qhead .who{color:var(--c)}
.qbox{border-radius:var(--r-l);padding:20px;background:var(--c-sf);border:1px solid var(--c-line);font-size:24px;font-weight:700;line-height:1.25}
.ans{border-radius:var(--r-m);padding:14px 18px;background:rgba(255,255,255,.07);border:1px dashed var(--mist);color:var(--mist);font-weight:600}
.ans b{display:block;color:var(--chalk);font-size:22px;margin-top:4px}
.timer{display:grid;grid-template-columns:1fr 1fr;gap:10px;align-items:center}
.timer .num{font-size:52px;text-align:center}
.verdict{text-align:center;font-weight:800;font-size:18px}
.verdict.ok{color:var(--g)} .verdict.bad{color:var(--danger)}
.dice-p{border-radius:var(--r-m);padding:12px 16px;background:var(--c-sf);border:1px solid var(--c-line);display:flex;justify-content:space-between;align-items:center}
.dice-p.turn{outline:3px solid var(--c)}
.dice-p .num{font-size:44px;color:var(--c);line-height:1}
.dice-p small{display:block;color:var(--mist);font-weight:600}
.rolls{font-size:30px;font-weight:800;text-align:center;min-height:1.3em}
.shakebox{border-radius:var(--r-l);padding:26px 16px;background:var(--c-sf);border:2px solid var(--c);display:flex;flex-direction:column;align-items:center;gap:8px;text-align:center;color:var(--mist);font-weight:600}
.shakebox b{font-size:32px;color:var(--c)}
.shakeico{font-size:64px}.shakeico.go{animation:hshake .18s linear infinite}
@keyframes hshake{0%,100%{transform:rotate(-14deg) translateX(-5px)}50%{transform:rotate(14deg) translateX(5px)}}
.hint{color:var(--mist);font-size:14px;text-align:center}
.spacer{flex:1}
.resetzone{margin-top:26px;padding-top:18px;border-top:1px dashed #3a3f3c;text-align:center}
.reset-open{background:none;border:0;color:var(--mist);opacity:.75;font-size:14px;font-weight:600;padding:10px 14px;text-decoration:underline;text-underline-offset:3px}
.reset-ask{text-align:left}
.reset-ask p{margin:0 0 14px;line-height:1.4}
.reset-ask .btn{padding:15px 8px;font-size:16px;background:transparent;border:1px solid var(--danger);color:var(--danger)}
.reset-ask .btn.quiet{border-color:#4a4f4c;color:var(--chalk)}
</style></head>
<body>
<div class="offline" id="offline">Нет связи с сервером — переподключаюсь…</div>
<main class="app" id="app"></main>
<div class="hint" style="text-align:center;padding:0 0 18px;font-size:12px;opacity:.6">Значки фона: OpenMoji, CC BY-SA 4.0</div>
<script>
__ROLL_JS__
__NET_JS__
const CATS = __CATS__;
const CATBY = Object.fromEntries(CATS.map(c => [c.id, c]));
const app = document.getElementById('app');
let finAsk = 0, ask = 0, shakeOn = false, shakeNeeded = false, lastKey = '';
const sym = r => r === 'ok' ? '✓' : r === 'bad' ? '✗' : '–';
function chips(r){ return ['man', 'woman'].map(w => `<i class="chip ${cls(w)} ${r[w] || ''}">${w === 'man' ? 'M' : 'W'} ${sym(r[w])}</i>`).join(''); }
function topBar(){ return `<div class="top"><span class="wordmark"><i></i>Финал</span><a class="link" href="${BASE}screen" target="_blank" rel="noopener">Экран для гостей</a></div>`; }
function board(s){ return `<div class="board"><div class="side m"><b>Man</b><span class="num">${s.score.man}</span></div><div class="side w"><b>Woman</b><span class="num">${s.score.woman}</span></div></div>`; }
function resetZone(){
  if (!ask) return `<div class="resetzone"><button class="reset-open" data-act="ask">Сбросить конкурс</button></div>`;
  const ready = Date.now() - ask > 700;
  return `<div class="resetzone"><div class="reset-ask"><p>Сбросить весь конкурс?<br><span class="hint">Счёт и все ответы обнулятся.</span></p>
    <div class="two"><button class="btn quiet" data-act="noask">Отмена</button><button class="btn" data-act="reset" ${ready ? '' : 'disabled'}>Да, сбросить</button></div></div></div>`;
}
function viewMenu(s){
  if (s.mode === 'pick' && s.pick) {
    const c = CATBY[s.pick], r = s.results[s.pick];
    return `${topBar()}<h2>${c.icon} ${esc(c.title)} — кто отвечает?</h2>
      <button class="btn m" style="min-height:96px;font-size:26px" data-act="pick" data-who="man">Man ${r.man ? '<small>(уже ответил: ' + sym(r.man) + ')</small>' : ''}</button>
      <button class="btn w" style="min-height:96px;font-size:26px" data-act="pick" data-who="woman">Woman ${r.woman ? '<small>(уже ответила: ' + sym(r.woman) + ')</small>' : ''}</button>
      <button class="btn quiet" data-act="cancel">Назад</button>`;
  }
  const all = CATS.every(c => s.results[c.id].man && s.results[c.id].woman);
  const tie = all && s.score.man === s.score.woman;
  return `${topBar()}${board(s)}${all ? `<div class="verdict ${tie ? '' : 'ok'}">${tie ? 'Ничья — играйте бонус игру' : 'Победил ' + (s.score.man > s.score.woman ? 'Man' : 'Woman')}</div>` : ''}
    <h2>Категории</h2>${CATS.map(c => `<button class="cat" data-act="cat" data-id="${c.id}"><span>${c.icon} ${esc(c.title)}</span><span class="chips">${chips(s.results[c.id])}</span></button>`).join('')}
    <button class="cat dice" data-act="dice">🎲 Бонус игра (на ничью)</button>${resetZone()}`;
}
function viewQuestion(s){
  const cu = s.cur, c = CATBY[cu.cat], full = c[cu.who], res = s.results[cu.cat][cu.who];
  let h = `${topBar()}<div class="qhead ${cls(cu.who)}"><span>${c.icon} ${esc(c.title)}</span><span class="who">${WN[cu.who]}</span></div>
    <div class="qbox ${cls(cu.who)}">${esc(full.q)}</div>
    <div class="ans">Правильный ответ<b>${esc(full.a)}</b></div>`;
  if (full.qfile) h += `<div class="two"><button class="btn quiet" data-act="audio" data-k="q">▶ Вопрос</button><button class="btn quiet" data-act="audio" data-k="a">▶ Ответ</button></div>
    <button class="btn quiet" data-act="audio" data-k="stop">■ Стоп</button>`;
  if (c.time) h += `<div class="timer"><div class="num" id="tm">${c.time}</div><button class="btn quiet" data-act="timer">${cu.started_at ? 'Заново' : 'Старт ' + c.time + ' с'}</button></div>`;
  h += `<div class="two"><button class="btn good ${res === 'ok' ? 'sel' : ''}" data-act="mark" data-r="ok">Зачёт</button><button class="btn bad ${res === 'bad' ? 'sel' : ''}" data-act="mark" data-r="bad">Не зачёт</button></div>`;
  h += res ? `<div class="verdict ${res}">${res === 'ok' ? 'Зачёт +1' : 'Не зачёт, без очка'}</div>` : `<div class="hint">Результат появится на экране гостей вместе с ответом</div>`;
  h += `<button class="btn hi ${res ? 'pulse' : ''}" data-act="to_pick">← Назад</button>`;
  return h + `<button class="btn quiet" data-act="back">К категориям</button>`;
}
function viewDice(s){
  const d = s.dice;
  const pl = w => { const p = d.p[w]; return `<div class="dice-p ${cls(w)} ${d.turn === w && !d.winner ? 'turn' : ''}"><div><b>${WN[w]}</b><small>${p.bust ? 'перебор' : p.stand ? 'хватит' : d.turn === w ? 'бросает' : 'ждёт'}</small></div><div class="num">${p.total}</div></div>`; };
  let h = `${topBar()}<div class="qhead"><span>🎲 Бонус игра</span></div>`;
  if (d.winner) {
    h += `${pl('man')}${pl('woman')}<div class="verdict ${d.winner === 'draw' ? '' : 'ok'}">${d.winner === 'draw' ? 'Ничья — переигрываем' : 'Победил ' + WN[d.winner]}</div>
      <button class="btn hi" data-act="dice_start">Переиграть</button>`;
  } else if (d.armed) {
    h += `<div class="shakebox ${cls(d.turn)}"><div class="shakeico ${d.shaking ? 'go' : ''}">🎲</div><b>${WN[d.turn]}</b><span>${d.shaking ? 'Кубик трясётся. Остановите телефон — кубик упадёт.' : 'Начинайте трясти телефон — кубик на экране затрясётся.'}</span></div>
      <button class="btn quiet" data-act="roll">Бросить сейчас</button>`;
  } else if (d.turn) {
    const me = d.p[d.turn];
    h += `${pl('man')}${pl('woman')}<div class="qhead ${cls(d.turn)}"><span>Бросает</span><span class="who">${WN[d.turn]}</span></div>
      <button class="btn ${cls(d.turn)}" style="min-height:96px;font-size:26px" data-act="shake">🎲 Трясти кости</button>
      <button class="btn quiet" data-act="roll">Бросить без тряски</button>
      <button class="btn quiet" data-act="stand" ${me.total ? '' : 'disabled'}>Хватит (${me.total})</button>
      <button class="btn quiet" data-act="dice_pick">Выбрать другого</button>`;
  } else {
    h += `${pl('man')}${pl('woman')}${d.last ? `<div class="rolls">${WN[d.last_who]}: выпало ${d.last[0] + d.last[1]} (${d.last[0]} + ${d.last[1]})</div>` : ''}<div class="hint">Кто бросает?</div>
      <button class="btn m" style="min-height:96px;font-size:26px" data-act="dice_pick" data-who="man" ${d.p.man.stand ? 'disabled' : ''}>Man</button>
      <button class="btn w" style="min-height:96px;font-size:26px" data-act="dice_pick" data-who="woman" ${d.p.woman.stand ? 'disabled' : ''}>Woman</button>`;
  }
  if (!d.shaking) h += `<button class="btn quiet" data-act="undo" ${d.ev.length ? '' : 'disabled'}>↶ Отменить последнее</button>
    <button class="btn quiet" data-act="dice_start">Начать заново</button><button class="btn quiet" data-act="back">К категориям</button>`;
  if (!d.shaking && !d.armed && !d.winner) h += `<button class="btn quiet" data-act="dice_finish">${finAsk ? 'Точно завершить? Нажмите ещё раз' : 'Завершить игру'}</button>`;
  return h;
}
function render(){
  if (!S) return;
  try {
    const key = JSON.stringify([S.mode, S.cur && [S.cur.cat, S.cur.who, S.cur.started_at], S.results, S.dice, S.pick, ask > 0, ask && Date.now() - ask > 700, finAsk, shakeOn, shakeNeeded, S.score]);
    if (key === lastKey) return; lastKey = key;
    app.innerHTML = S.mode === 'q' && S.cur ? viewQuestion(S) : S.mode === 'dice' && S.dice ? viewDice(S) : viewMenu(S);
    tick();
  } catch (e) { console.error(e); }
}
function tick(){
  const el = document.getElementById('tm'); if (!el || !S || !S.cur) return;
  const t = S.cur.time, st = S.cur.started_at;
  const v = st ? Math.max(0, Math.ceil(t - (serverNow() - st))) : t;
  if (el.textContent !== String(v)) el.textContent = v;
}
setInterval(tick, 250);
window.onState = s => { render(); };
/* Тряска: «Трясти кости» включает тряску кубика на экране; когда телефон успокоился (после движения), бросок фиксируется. */
let moved = false, lastMove = 0, armedAt = 0, sentShake = false, hasSensor = false;
function onMotion(e){
  const a = e.accelerationIncludingGravity || e.acceleration; if (!a) return;
  const m = Math.hypot(a.x || 0, a.y || 0, a.z || 0), now = Date.now();
  const d = Math.abs(m - (onMotion.prev || m)); onMotion.prev = m; hasSensor = true;
  if (d > 6) {
    moved = true; lastMove = now;
    if (!sentShake && S && S.dice && S.dice.armed) { sentShake = true; send({a: 'dice_shake', on: true}); }   // кубик трясётся, только пока трясётся телефон
  }
}
function rollNow(){ if (S && S.mode === 'dice' && S.dice && S.dice.turn && !S.dice.winner) { moved = false; sentShake = false; armedAt = 0; send({a: 'roll'}); } }
function startShake(){
  if (!(S && S.mode === 'dice' && S.dice && S.dice.turn)) return;
  moved = false; sentShake = false; lastMove = 0; armedAt = Date.now(); send({a: 'dice_arm'});
}
setInterval(() => {
  if (!(S && S.dice && S.dice.armed && armedAt)) return;
  const now = Date.now();
  if (!hasSensor) {                                   // нет датчика: кубик потрясётся 2,5 с и упадёт сам
    if (!sentShake && now - armedAt > 400) { sentShake = true; send({a: 'dice_shake', on: true}); }
    if (now - armedAt > 2900) rollNow();
  } else if (sentShake && moved && now - lastMove > 900) rollNow();   // телефон успокоился — бросок
}, 150);
window.__shake = () => { startShake(); };
function enableShake(then){
  const go = () => { if (!shakeOn) addEventListener('devicemotion', onMotion); shakeOn = true; if (then) then(); };
  const DM = window.DeviceMotionEvent;
  if (DM && typeof DM.requestPermission === 'function') DM.requestPermission().then(r => { if (r === 'granted') go(); else if (then) then(); }).catch(() => { if (then) then(); });
  else if (DM) go(); else if (then) then();
}
if (window.DeviceMotionEvent && typeof DeviceMotionEvent.requestPermission !== 'function') enableShake();
app.addEventListener('click', e => {
  const b = e.target.closest('[data-act]'); if (!b || b.disabled) return;
  const a = b.dataset.act;
  if (a === 'cat') send({a: 'open', cat: b.dataset.id});
  else if (a === 'cancel') send({a: 'back'});
  else if (a === 'pick') send({a: 'pick', cat: S.pick, who: b.dataset.who});
  else if (a === 'to_pick') send({a: 'to_pick'});
  else if (a === 'back') send({a: 'back'});
  else if (a === 'mark') send({a: 'mark', r: b.dataset.r});
  else if (a === 'timer') send({a: 'timer'});
  else if (a === 'audio') send({a: 'audio', k: b.dataset.k});
  else if (a === 'dice') send({a: 'dice_start'});
  else if (a === 'dice_start') send({a: 'dice_start'});
  else if (a === 'dice_pick') send({a: 'dice_pick', who: b.dataset.who || null});
  else if (a === 'undo') send({a: 'undo'});
  else if (a === 'dice_finish') { if (finAsk) { finAsk = 0; send({a: 'dice_finish'}); } else { finAsk = 1; render(); setTimeout(() => { finAsk = 0; render(); }, 4000); } }
  else if (a === 'roll') rollNow();
  else if (a === 'stand') send({a: 'stand'});
  else if (a === 'shake') { if (shakeOn) startShake(); else enableShake(startShake); }
  else if (a === 'ask') { ask = Date.now(); lastKey = ''; render(); setTimeout(render, 760); setTimeout(() => { if (ask && Date.now() - ask >= 10000) { ask = 0; lastKey = ''; render(); } }, 10100); }
  else if (a === 'noask') { ask = 0; lastKey = ''; render(); }
  else if (a === 'reset') { ask = 0; send({a: 'reset'}); }
});
</script>
</body></html>
"""

# ======================================================================
# Экран для гостей
# ======================================================================
SCREEN_HTML = r"""<!doctype html>
<html lang="ru" data-base="__BASE__" data-ver="__VER__">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Финал</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Onest:wght@400;600;800&family=Unbounded:wght@600;800;900&family=Nunito:wght@800;900&display=swap" rel="stylesheet">
<script src="https://cdn.socket.io/4.8.1/socket.io.min.js"></script>
<style>__CSS__
__ROLL_CSS__
html,body{height:100%;overflow:hidden}
#bg{position:fixed;inset:0;width:100%;height:100%;z-index:0;pointer-events:none}
.screen{position:relative;z-index:1;height:100vh;display:grid;grid-template-columns:minmax(150px,19vw) 1fr minmax(150px,19vw);gap:2vw;padding:3vh 2.4vw 4vh}
.sidecol{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:1.4vh;border-radius:var(--r-l);background:rgba(0,0,0,.34);border:1px solid var(--c-line);backdrop-filter:blur(6px);transition:box-shadow .4s,border-color .4s}
.sidecol.on{border-color:var(--c);box-shadow:0 0 60px var(--c-soft)}
.sidecol .nm{font-family:var(--display);font-weight:900;font-size:clamp(22px,2.6vw,50px);color:var(--c)}
.sidecol .num{font-size:clamp(70px,12vw,230px);line-height:1;color:var(--c);filter:drop-shadow(0 0 28px var(--c-soft));--roll-ms:800ms}
.sidecol .un{text-align:center;padding:0 .8vw;line-height:1.35;color:var(--mist);font-weight:600;font-size:clamp(13px,1.2vw,22px)}
.stage{display:flex;flex-direction:column;min-height:0;align-items:center;justify-content:center;gap:2vh;text-align:center}
.stage .wordmark{font-size:clamp(20px,2.2vw,40px)}
.tiles{display:flex;flex-direction:column;gap:1.8vh;width:min(780px,100%)}
.tile{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:1.4vw;padding:1.8vh 2vw;border-radius:var(--r-l);background:rgba(10,12,11,.66);border:1px solid #3a3f3c;backdrop-filter:blur(6px);font-family:var(--display);font-weight:800;font-size:clamp(20px,2.6vw,48px)}
.tile .l{justify-self:start;display:flex;gap:.4em;font-size:.5em}
.tile .r{justify-self:end;display:flex;gap:.4em;font-size:.5em}
.tile .t{white-space:nowrap}
.dot{width:1.6em;height:1.6em;border-radius:50%;border:2px solid var(--c-line);display:grid;place-items:center;font-size:.8em;font-family:var(--ui);color:var(--mist)}
.dot.ok{background:var(--c);border-color:var(--c);color:var(--c-ink)}
.dot.bad{background:var(--danger-bg);border-color:var(--danger);color:var(--danger)}
.tile.now{border-color:var(--chalk);box-shadow:0 0 50px rgba(255,255,255,.18)}
.tile.done{opacity:.62}
.final{font-family:var(--display);font-weight:900;font-size:clamp(26px,3.4vw,64px);padding:1.2vh 2vw;border-radius:999px;background:rgba(255,255,255,.1)}
.final.m{color:var(--g)} .final.w{color:var(--p)}
.cattitle .ic{position:absolute;right:100%;margin-right:.35em}
.cattitle{position:relative;display:inline-block;font-family:var(--display);font-weight:900;font-size:clamp(22px,2.6vw,48px);color:var(--mist)}
.badge{display:inline-block;padding:.35em 1.1em;border-radius:999px;font-family:var(--display);font-weight:900;font-size:clamp(24px,3.2vw,60px);background:var(--c);color:var(--c-ink)}
.q{font-weight:800;font-size:clamp(30px,4.6vw,88px);line-height:1.15;max-width:90%;text-wrap:balance}
.big{font-size:min(15vw,30vh);line-height:1;color:var(--chalk);--roll-ms:520ms}
.big.low{color:var(--danger)}
.ansbox{font-family:var(--display);font-weight:900;font-size:clamp(28px,4vw,76px);color:var(--c);line-height:1.15;max-width:92%}
.vd{font-weight:800;font-size:clamp(22px,2.6vw,48px)}
.vd.ok{color:var(--g)} .vd.bad{color:var(--danger)}
.pickrow{display:flex;gap:3vw;align-items:center;justify-content:center}
.stage.enter>*{animation:popin .55s cubic-bezier(.2,1.3,.3,1) both}
@keyframes popin{from{opacity:0;transform:translateY(2.2vh) scale(.92)}to{opacity:1;transform:none}}
.tile{transition:transform .25s,border-color .15s,background .15s,box-shadow .25s}
.tile.sweep{border-color:var(--chalk);background:rgba(255,255,255,.14)}
.tile.chosen{transform:scale(1.08);border-color:var(--chalk);background:rgba(255,255,255,.18);box-shadow:0 0 70px rgba(255,255,255,.4)}
.pickside{transition:transform .3s cubic-bezier(.2,1.3,.3,1),opacity .3s}
.pickside.chosen{transform:scale(1.28)}
.pickside.chosen .badge{box-shadow:0 0 80px var(--c)}
.pickside.dim{opacity:.22;transform:scale(.88)}
.pickside{display:flex;align-items:center;gap:1vw;font-size:clamp(14px,1.6vw,30px)}
.pickside .badge{font-size:clamp(20px,2.4vw,44px);width:clamp(150px,15vw,300px);text-align:center;padding-left:0;padding-right:0}
.pickside .dot{font-size:clamp(14px,1.6vw,28px)}
.listen{font-size:clamp(20px,2.4vw,44px);color:var(--mist);font-weight:600}
.eq{display:inline-flex;gap:.2em;align-items:flex-end;height:1.4em;margin-right:.5em}
.eq i{width:.22em;background:var(--chalk);border-radius:2px;animation:eq 1s ease-in-out infinite}
.eq i:nth-child(2){animation-delay:.2s}.eq i:nth-child(3){animation-delay:.4s}.eq i:nth-child(4){animation-delay:.1s}
@keyframes eq{0%,100%{height:20%}50%{height:100%}}
.duel{display:grid;grid-template-columns:1fr 1fr;gap:2vw;width:min(1000px,100%)}
.pl{border-radius:var(--r-l);padding:2vh 1.6vw;background:rgba(10,12,11,.66);border:2px solid var(--c-line)}
.pl.turn{border-color:var(--c);box-shadow:0 0 50px var(--c-soft)}
.pl .nm{font-family:var(--display);font-weight:900;color:var(--c);font-size:clamp(20px,2.2vw,40px)}
.pl .sum{font-size:clamp(60px,9vw,170px);line-height:1;color:var(--c)}
.pl .st{color:var(--mist);font-weight:600;font-size:clamp(14px,1.4vw,26px);min-height:1.3em}
.pl .hist{font-size:clamp(22px,2.6vw,48px);min-height:1.3em;word-break:break-all}
.duel{transition:opacity .35s}.stage .duel.fade{opacity:0 !important;animation:none}
.duel .pl .sum{font-size:clamp(44px,6.5vh,110px)}.duel .pl .hist{min-height:1.2em}
.box{flex:none;--bx:min(70vw,38vh);--s:calc(var(--bx) * .21);width:var(--bx);height:var(--bx);position:relative;margin:1.4vh 0;border-radius:2%;border:4px solid rgba(255,255,255,.55);background:rgba(10,12,11,.5);box-shadow:0 0 40px rgba(0,0,0,.4);transition:box-shadow .08s,border-color .08s}
.box.hit{border-color:#fff;box-shadow:0 0 50px rgba(255,255,255,.55)}
.slot{position:absolute;left:50%;top:50%;transform-origin:center;width:var(--s);height:var(--s);margin:calc(var(--s) / -2) 0 0 calc(var(--s) / -2)}
.die3{width:100%;height:100%;perspective:900px;position:relative}
.tilt{width:100%;height:100%;transform-style:preserve-3d}
.cube3{width:100%;height:100%;position:relative;transform-style:preserve-3d;transition:transform 1.3s cubic-bezier(.12,.75,.2,1)}
.face{position:absolute;inset:0;border-radius:14%;background:linear-gradient(145deg,#fffdf6,#e6e0d2);box-shadow:inset 0 0 18px rgba(0,0,0,.18),inset 0 0 0 2px rgba(255,255,255,.7);display:grid;grid-template:repeat(3,1fr)/repeat(3,1fr);padding:13%;backface-visibility:hidden}
.face i{border-radius:50%;margin:12%}
.face i.on{background:radial-gradient(circle at 35% 30%,#555,#0d0d0d 70%);box-shadow:inset 0 2px 3px rgba(0,0,0,.6)}
.shadow{position:absolute;left:10%;right:10%;bottom:-9%;height:11%;border-radius:50%;background:radial-gradient(rgba(0,0,0,.55),transparent 70%);filter:blur(4px)}
.die3.drop{animation:drop 1.3s cubic-bezier(.3,.6,.4,1)}
@keyframes drop{0%{transform:translateY(-130%) scale(1.3)}38%{transform:translateY(0) scale(1)}52%{transform:translateY(-32%)}66%{transform:translateY(0)}78%{transform:translateY(-10%)}100%{transform:translateY(0)}}
@keyframes jit{0%{transform:translate(-14px,6px)}25%{transform:translate(12px,-10px)}50%{transform:translate(-8px,-6px)}75%{transform:translate(14px,9px)}100%{transform:translate(-14px,6px)}}
@keyframes spin3{0%{transform:rotateX(20deg) rotateY(0) rotateZ(0)}33%{transform:rotateX(140deg) rotateY(110deg) rotateZ(40deg)}66%{transform:rotateX(250deg) rotateY(230deg) rotateZ(-30deg)}100%{transform:rotateX(380deg) rotateY(360deg) rotateZ(0)}}
.dres{font-family:var(--display);font-weight:900;font-size:clamp(30px,4.6vw,90px);line-height:1;white-space:nowrap;color:var(--chalk);text-align:center}
.dres span{color:var(--c);font-size:.55em;margin-right:.2em}
.dres small{display:block;font-size:.4em;color:var(--mist);font-weight:700;margin-top:.1em}
.dres b{font-size:1.5em;color:var(--c);text-shadow:0 0 40px var(--c-soft)}
.dres,.final{transition:opacity .35s,transform .45s cubic-bezier(.2,1.4,.3,1)}
.stage .dres.hold,.stage .final.hold{opacity:0 !important;transform:scale(.6);animation:none}
.dstat{font-family:var(--display);font-weight:800;font-size:clamp(22px,3vw,56px);color:var(--c);text-align:center}
.startov{position:fixed;inset:0;z-index:200;display:grid;place-items:center;background:radial-gradient(80% 80% at 50% 50%,rgba(6,12,9,.9),rgba(8,6,8,.97));backdrop-filter:blur(10px);transition:opacity .5s}
.startbox{display:flex;flex-direction:column;align-items:center;gap:3.2vh;text-align:center;padding:0 5vw}
.startbox .wordmark{font-size:clamp(28px,4vw,72px)}
.startbtn{border:0;border-radius:999px;padding:.7em 1.6em;font-family:var(--display);font-weight:800;font-size:clamp(22px,2.8vw,52px);background:linear-gradient(100deg,var(--g),var(--p));color:#10120f;box-shadow:0 0 70px rgba(255,255,255,.12);animation:startpulse 1.8s ease-in-out infinite}
@keyframes startpulse{0%,100%{transform:scale(1)}50%{transform:scale(1.035)}}
.starthint{color:var(--mist);font-weight:600;font-size:clamp(14px,1.4vw,26px);max-width:34em}
@media (max-width:900px){.screen{grid-template-columns:1fr;grid-template-rows:auto 1fr;gap:1vh}.sidecol{flex-direction:row;padding:1vh 4vw;justify-content:space-around}.sidecol .num{font-size:12vw}}
</style></head>
<body>
<canvas id="bg"></canvas>
<div class="offline" id="offline">Нет связи с сервером — переподключаюсь…</div>
<main class="screen">
  <div class="sidecol m" id="sm"><div class="nm">Man</div><div class="num" id="nm">0</div><div class="un" id="um">очков</div></div>
  <section class="stage" id="stage"></section>
  <div class="sidecol w" id="sw"><div class="nm">Woman</div><div class="num" id="nw">0</div><div class="un" id="uw">очков</div></div>
</main>
<div class="startov" id="startov"><div class="startbox">
  <div class="wordmark"><i></i>Финал</div>
  <button class="startbtn" id="snd">🔊 Включить звук и начать</button>
  <div class="starthint">Нажмите один раз: после этого звук и музыка в конкурсе будут играть сами</div>
</div></div>
<audio id="au" preload="auto"></audio>
<script>
__ROLL_JS__
__NET_JS__
/* ---------- Фон «Огоньки»: слева зелёные значки «мужского» (молоток, галстук, мяч…), справа розовые «женского» (помада, туфля, платье…) ---------- */
const Bg = (() => {
  const cv = document.getElementById('bg'), ctx = cv.getContext('2d');
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  let W = 0, H = 0, dpr = 1, k = 1;
  const GREEN = [43, 240, 138], PINK = [255, 79, 168];
  const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${Math.max(0, Math.min(1, a))})`;
  function size(){
    dpr = Math.min(2, devicePixelRatio || 1); W = innerWidth; H = innerHeight;
    k = Math.max(.8, Math.min(1.8, Math.min(W, H) / 800));
    cv.width = W * dpr; cv.height = H * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  addEventListener('resize', size); size();
  // вертикальная «капсула» (палец, ручка): левый верх x,y, ширина w, высота h
  function cap(x, y, w, h){
    ctx.beginPath(); ctx.arc(x + w / 2, y + w / 2, w / 2, Math.PI, 0); ctx.lineTo(x + w, y + h - w / 2);
    ctx.arc(x + w / 2, y + h - w / 2, w / 2, 0, Math.PI); ctx.closePath();
  }
  // Значки на тему «мужское / женское» (OpenMoji, контурные, CC BY-SA 4.0). Красим в цвет половины экрана.
  const SETS = {
    man: ['1F528', '1F454', '26BD', '1F697', '1F37A', '1F527', '1F4AA', '231A'],      // молоток, галстук, мяч, машина, пиво, ключ, бицепс, часы
    woman: ['1F484', '1F460', '1F457', '1F485', '1F45C', '1F48D', '1F338', '1F48B'],  // помада, туфля, платье, лак, сумочка, кольцо, цветок, поцелуй
  };
  const TINT = {man: [], woman: []};
  function tinted(img, col){
    const c = document.createElement('canvas'); c.width = c.height = 128; const x = c.getContext('2d');
    x.drawImage(img, 0, 0, 128, 128); x.globalCompositeOperation = 'source-in'; x.fillStyle = `rgb(${col[0]},${col[1]},${col[2]})`; x.fillRect(0, 0, 128, 128);
    return c;
  }
  ['man', 'woman'].forEach(w => SETS[w].forEach((code, i) => {
    const img = new Image(); img.onload = () => { TINT[w][i] = tinted(img, w === 'man' ? GREEN : PINK); };
    img.src = BASE + 'icons/' + code + '.svg';
  }));
  function face(x, y, s, rot, a, man, kind){
    const im = TINT[man ? 'man' : 'woman'][kind]; if (!im) return;
    ctx.save(); ctx.translate(x, y); ctx.rotate(rot); ctx.globalAlpha = Math.max(0, Math.min(1, a));
    ctx.drawImage(im, -s * .8, -s * .8, s * 1.6, s * 1.6);
    ctx.restore();
  }
  const rnd = () => Math.random();
  const icons = Array.from({length: 12}, (_, i) => ({
    man: i % 2 === 0, x: rnd(), y: rnd(), r: 20 + rnd() * 22, v: .012 + rnd() * .024, a: .14 + rnd() * .14,
    w: rnd() * 6, rot: (rnd() - .5) * .6, rs: .25 + rnd() * .4, kind: Math.floor(rnd() * 8),
  }));
  const t0 = performance.now();
  function frame(now){
    const t = reduce ? 0 : (now - t0) / 1000;
    ctx.clearRect(0, 0, W, H);
    icons.forEach(f => {
      const y = ((f.y - t * f.v) % 1 + 1) % 1, fade = Math.min(1, y * 10, (1 - y) * 10);
      const half = f.man ? 0 : .5;
      const px = (half + .03 + f.x * .44 + Math.sin(t * .3 + f.w) * .012) * W, py = y * H * 1.15 - H * .07;
      const r = f.r * k, rot = f.rot + Math.sin(t * f.rs + f.w) * .18, a = f.a * fade, col = f.man ? GREEN : PINK;
      const g = ctx.createRadialGradient(px, py, 0, px, py, r * 1.7);
      g.addColorStop(0, rgba(col, f.a * .35 * fade)); g.addColorStop(1, rgba(col, 0));
      ctx.globalCompositeOperation = 'lighter'; ctx.fillStyle = g;
      ctx.beginPath(); ctx.arc(px, py, r * 1.7, 0, Math.PI * 2); ctx.fill();
      ctx.globalCompositeOperation = 'source-over';
      face(px, py, r * 1.15, rot, a * 2.3, f.man, f.kind);
    });
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
})();

/* ---------- Звук (только эффекты, музыка идёт через <audio>) ---------- */
let AC = null, unlocked = false;
const au = document.getElementById('au'), snd = document.getElementById('snd'), startov = document.getElementById('startov');
function tone(f, t0, d, type, g){
  if (!AC) return; const o = AC.createOscillator(), v = AC.createGain();
  o.type = type || 'sine'; o.frequency.value = f; v.gain.setValueAtTime(0, AC.currentTime + t0);
  v.gain.linearRampToValueAtTime(g || .15, AC.currentTime + t0 + .02); v.gain.exponentialRampToValueAtTime(.001, AC.currentTime + t0 + d);
  o.connect(v); v.connect(AC.destination); o.start(AC.currentTime + t0); o.stop(AC.currentTime + t0 + d + .05);
}
const sfx = {
  ok(){ tone(523, 0, .25); tone(659, .12, .25); tone(784, .24, .45); },
  bad(){ tone(196, 0, .4, 'sawtooth', .1); tone(147, .18, .55, 'sawtooth', .1); },
  rattle(){ for (let i = 0; i < 4; i++) tone(180 + Math.random() * 600, i * .05, .04, 'square', .05); },
  roll(){ for (let i = 0; i < 7; i++) tone(300 + Math.random() * 500, i * .07, .06, 'square', .06); },
  win(){ [523, 659, 784, 1047].forEach((f, i) => tone(f, i * .13, .4)); },
  time(){ tone(220, 0, .6, 'sawtooth', .12); },
  tick(i){ tone(520 + (i % 8) * 40, 0, .05, 'square', .05); },
  pick(){ tone(660, 0, .12, 'triangle', .14); tone(990, .09, .28, 'triangle', .14); }
};
// Тишина в формате WAV (44 байта данных): нужна, чтобы «разбудить» <audio> внутри нажатия — иначе Safari/планшеты потом не дают играть сами.
const SILENT = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAESsAABErAAABAAgAZGF0YQAAAAA=';
function showSndBtn(text){ startov.hidden = false; startov.style.opacity = 1; snd.textContent = text || '🔊 Включить звук и начать'; }
async function enableSound(){
  if (unlocked) return;
  try { AC = AC || new (window.AudioContext || window.webkitAudioContext)(); await AC.resume(); } catch (e) {}
  try { const keep = au.src; au.muted = true; au.src = SILENT; await au.play(); au.pause(); au.muted = false; au.removeAttribute('src'); au.load(); if (keep && keep !== location.href) au.src = keep; } catch (e) { try { au.muted = false; } catch (e2) {} }
  unlocked = true; startov.style.opacity = 0; setTimeout(() => { if (unlocked) startov.hidden = true; }, 520);
  sfx.ok();                                   // короткий сигнал: слышно — значит звук включён
  if (S) playFrom(S, true);
}
snd.onclick = enableSound;
// Любое первое нажатие по экрану тоже включает звук (кнопку искать не обязательно).
['pointerdown', 'keydown', 'touchstart'].forEach(ev => addEventListener(ev, () => { if (!unlocked) enableSound(); }, {once: false, passive: true}));
addEventListener('keydown', e => { if (e.key === 'f' || e.key === 'F') { document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen().catch(() => {}); } });
let audioN = -1, lastAudioKey = '';
function playFrom(s, force){
  const cu = s.cur; const a = cu && cu.audio;
  const key = cu ? cu.cat + cu.who + ':' + a.n : '';
  if (!force && key === lastAudioKey) return; lastAudioKey = key;
  if (!a || !a.kind) { try { au.pause(); } catch (e) {} return; }
  const f = a.kind === 'q' ? cu.qfile : cu.afile; if (!f) return;
  au.src = BASE + 'audio/' + f; au.currentTime = 0;
  au.play().catch(e => { if (e && e.name === 'NotAllowedError') { unlocked = false; showSndBtn('🔊 Нажмите, чтобы включить звук'); } });
}

/* ---------- Отрисовка ---------- */
const stage = document.getElementById('stage');
let rattle = 0, lastKey = '', prevRes = '', prevRoll = -1, rollingUntil = 0, prevMode = '', timeFired = '';
const dot = r => `<span class="dot ${r || ''}">${r === 'ok' ? '✓' : r === 'bad' ? '✗' : ''}</span>`;
const FACE = '⚀⚁⚂⚃⚄⚅';
function menuHtml(s){
  const all = s.cats.every(c => s.results[c.id].man && s.results[c.id].woman);
  let fin = '';
  if (all) fin = s.score.man === s.score.woman ? `<div class="final">Ничья! Решает бонус игра</div>` : `<div class="final ${s.score.man > s.score.woman ? 'm' : 'w'}">Побеждает ${s.score.man > s.score.woman ? 'Man' : 'Woman'}!</div>`;
  return `<div class="wordmark"><i></i>Финал</div><div class="tiles">${s.cats.map(c => { const r = s.results[c.id], done = r.man && r.woman;
    return `<div class="tile ${done ? 'done' : ''}"><span class="l m">${dot(r.man)}</span><span class="t">${c.icon} ${esc(c.title)}</span><span class="r w">${dot(r.woman)}</span></div>`; }).join('')}</div>${fin}`;
}
function pickHtml(s){
  const c = s.cats.find(x => x.id === s.pick), r = s.results[s.pick];
  const side = (w, cl) => `<div class="pickside ${cl}">${w === 'man' ? dot(r[w]) : ''}<span class="badge ${cl}">${WN[w]}</span>${w === 'woman' ? dot(r[w]) : ''}</div>`;
  return `<div class="cattitle"><span class="ic">${c.icon}</span>Категория</div><div class="q">${esc(c.title)}</div>
    <div class="pickrow">${side('man', 'm')}${side('woman', 'w')}</div><div class="listen">Кто отвечает?</div>`;
}
function questionHtml(s){
  const cu = s.cur, res = s.results[cu.cat][cu.who], c = s.cats.find(x => x.id === cu.cat);
  let h = `<div class="cattitle"><span class="ic">${c.icon}</span>${esc(cu.title)}</div><div class="badge ${cls(cu.who)}">${WN[cu.who]}</div>`;
  if (cu.qfile) h += cu.revealed ? '' : `<div class="listen"><span class="eq"><i></i><i></i><i></i><i></i></span>Слушаем и угадываем</div>`;
  h += `<div class="q">${esc(cu.q)}</div>`;
  if (cu.time && !cu.revealed) h += `<div class="num big" id="big"></div>`;
  if (cu.revealed) h += `<div class="vd ${res || ''}">${res === 'ok' ? 'Зачёт! +1 очко' : res === 'bad' ? 'Не зачёт' : ''}</div><div class="ansbox ${cls(cu.who)}">${esc(cu.a)}</div>`;
  return h;
}
const PIPS = {1: [4], 2: [0, 8], 3: [0, 4, 8], 4: [0, 2, 6, 8], 5: [0, 2, 4, 6, 8], 6: [0, 2, 3, 5, 6, 8]};
const POSE = {1: [0, 0], 2: [0, -90], 3: [-90, 0], 4: [90, 0], 5: [0, 90], 6: [0, 180]};   // [rotateX, rotateY], чтобы нужная грань смотрела на зрителя
const FACE_T = {1: 'rotateY(0deg)', 2: 'rotateY(90deg)', 3: 'rotateX(90deg)', 4: 'rotateX(-90deg)', 5: 'rotateY(-90deg)', 6: 'rotateY(180deg)'};
const poseCss = (v, k = 0) => `rotateX(${POSE[v][0] + k * 720}deg) rotateY(${POSE[v][1] + k * 540}deg) rotateZ(${k * 360}deg)`;
function cubeHtml(vals){
  const face = n => `<div class="face" style="transform:${FACE_T[n]} translateZ(calc(var(--s) / 2))">${Array.from({length: 9}, (_, i) => `<i class="${PIPS[n].includes(i) ? 'on' : ''}"></i>`).join('')}</div>`;
  const busy = phys.mode !== 'idle';
  const one = (v, o) => `<div class="slot" style="transform:translate(calc(${o.x} * var(--bx)),calc(${o.y} * var(--bx)))"><div class="die3"><div class="cube3" data-v="${v}" style="transform:${busy ? dPose(o) : poseCss(v)};${busy ? 'transition:none' : ''}">${[1, 2, 3, 4, 5, 6].map(face).join('')}</div><div class="shadow"></div></div></div>`;
  return `<div class="box">${one(vals[0], phys.dice[0])}${one(vals[1], phys.dice[1])}</div>`;
}
/* Два кубика в квадрате: пока трясут телефон, они летают и бьются о стенки и друг о друга; когда телефон затих — замедляются и ложатся на грань. */
const mkDie = (x, y) => ({x, y, vx: 0, vy: 0, ax: 20, ay: 0, az: 0, wx: 0, wy: 0, wz: 0, final: 5, ease: false, done: false});
const phys = {mode: 'idle', dice: [mkDie(-.2, .05), mkDie(.2, -.05)], loop: 0, last: 0, doneAt: 0, unit: true};   // пока unit — x,y в долях квадрата
const nearest = (cur, base) => base + 360 * Math.round((cur - base) / 360);
const dPose = o => `rotateX(${o.ax}deg) rotateY(${o.ay}deg) rotateZ(${o.az}deg)`;
function physApply(){
  const slots = document.querySelectorAll('.slot'), cubes = document.querySelectorAll('.cube3'), box = document.querySelector('.box');
  if (!box) return;
  phys.dice.forEach((o, i) => {
    if (!slots[i]) return;
    slots[i].style.transform = `translate(${o.x * box.clientWidth}px,${o.y * box.clientWidth}px)`;
    if (!o.ease && cubes[i]) cubes[i].style.transform = dPose(o);
  });
}
function physLoop(t){
  if (phys.mode === 'idle') { phys.loop = 0; return; }
  const box = document.querySelector('.box'), slot = document.querySelector('.slot'), cubes = document.querySelectorAll('.cube3');
  const dt = Math.min(.04, (t - phys.last) / 1000); phys.last = t;
  if (box && slot) {
    const W = box.clientWidth, sz = slot.offsetWidth / W;           // всё считаем в долях стороны квадрата
    const lim = .5 - sz * .75, rest = phys.mode === 'settle' ? .62 : 1, live = phys.mode === 'live';
    let alldone = true;
    phys.dice.forEach((o, i) => {
      if (phys.mode === 'settle') { const k = Math.exp(-2.4 * dt), kw = Math.exp(-1.7 * dt); o.vx *= k; o.vy *= k; o.wx *= kw; o.wy *= kw; o.wz *= kw; }
      o.x += o.vx * dt; o.y += o.vy * dt; let hit = false;
      if (o.x > lim) { o.x = lim; o.vx = -Math.abs(o.vx) * rest; hit = true; }
      if (o.x < -lim) { o.x = -lim; o.vx = Math.abs(o.vx) * rest; hit = true; }
      if (o.y > lim) { o.y = lim; o.vy = -Math.abs(o.vy) * rest; hit = true; }
      if (o.y < -lim) { o.y = -lim; o.vy = Math.abs(o.vy) * rest; hit = true; }
      if (hit) {
        if (live) { o.vx += (Math.random() - .5) * .6; o.vy += (Math.random() - .5) * .6; const sp = Math.hypot(o.vx, o.vy); if (sp < 1) { o.vx /= sp; o.vy /= sp; } if (sp > 2) { o.vx *= 2 / sp; o.vy *= 2 / sp; } }
        box.classList.add('hit'); setTimeout(() => box.classList.remove('hit'), 90);
        const sp = Math.hypot(o.vx, o.vy); if (unlocked && sp > .2) tone(80 + Math.random() * 60, 0, .09, 'triangle', Math.min(.22, sp / 8));
        o.wx += (Math.random() - .5) * 400; o.wy += (Math.random() - .5) * 400;
      }
      if (!o.ease) { o.ax += o.wx * dt; o.ay += o.wy * dt; o.az += o.wz * dt; }
    });
    const [a, b] = phys.dice, dx = b.x - a.x, dy = b.y - a.y, dist = Math.hypot(dx, dy), min = sz * 1.15;
    if (dist < min && dist > 1e-4) {                                  // кубики сталкиваются
      const nx = dx / dist, ny = dy / dist, push = (min - dist) / 2;
      a.x -= nx * push; a.y -= ny * push; b.x += nx * push; b.y += ny * push;
      const va = a.vx * nx + a.vy * ny, vb = b.vx * nx + b.vy * ny;
      if (va - vb > 0) { a.vx += (vb - va) * nx; a.vy += (vb - va) * ny; b.vx += (va - vb) * nx; b.vy += (va - vb) * ny; if (unlocked && va - vb > .3) tone(300 + Math.random() * 120, 0, .05, 'square', .08); }
    }
    phys.dice.forEach((o, i) => {
      const sp = Math.hypot(o.vx, o.vy);
      if (phys.mode === 'settle' && !o.ease && sp < .28 && cubes[i]) {   // почти встал — доворачиваем на ближайшую грань
        const [px, py] = POSE[o.final];
        o.ease = true; cubes[i].style.transition = 'transform .75s cubic-bezier(.2,.9,.3,1.15)';
        o.ax = nearest(o.ax, px); o.ay = nearest(o.ay, py); o.az = nearest(o.az, 0);
        cubes[i].style.transform = dPose(o);
        setTimeout(() => { if (unlocked) tone(110, 0, .12, 'triangle', .2); }, 450);
      }
      if (!(o.ease && sp < .012)) alldone = false;
    });
    if (phys.mode === 'settle' && alldone) {
      phys.dice.forEach(o => { o.vx = o.vy = 0; });
      if (!phys.doneAt) phys.doneAt = t;
      if (t - phys.doneAt > 800) { phys.mode = 'idle'; phys.doneAt = 0; phys.loop = 0; physApply(); return; }
    }
    physApply();
  }
  phys.loop = requestAnimationFrame(physLoop);
}
function physRun(){ if (!phys.loop) { phys.last = performance.now(); phys.loop = requestAnimationFrame(physLoop); } }
function physSync(d){
  if (d.shaking) {
    if (phys.mode !== 'live') {
      phys.mode = 'live'; phys.doneAt = 0;
      phys.dice.forEach((o, i) => {
        o.ease = false; o.vx = (i ? -1 : 1) * (1 + Math.random() * .4); o.vy = (Math.random() < .5 ? -1 : 1) * (.8 + Math.random() * .5);
        o.wx = 500 + Math.random() * 300; o.wy = 600 + Math.random() * 300; o.wz = 150 + Math.random() * 150;
      });
      document.querySelectorAll('.cube3').forEach(c => c.style.transition = 'none');
    }
    physRun();
  } else if (phys.mode === 'live') {
    phys.mode = 'settle'; phys.doneAt = 0;
    phys.dice.forEach((o, i) => { o.final = (d.last && d.last[i]) || 1; o.ease = false; });
    document.querySelectorAll('.cube3').forEach(c => c.style.transition = 'none');
    physRun();
  } else if (phys.mode === 'settle') physRun();
}
let dropUntil = 0, sideTimer = 0, winKey = '';
function diceHtml(s){
  const d = s.dice, hold = (prevRoll !== -1 && d.seq !== prevRoll) || phys.mode !== 'idle' || performance.now() < dropUntil;
  let info = '';
  if (d.armed) info = `<div class="dstat ${cls(d.turn)}">${WN[d.turn]} бросает кости</div>`;
  else if (d.last && !d.turn) info = `<div class="dres ${cls(d.last_who)} ${hold ? 'hold' : ''}"><span>${WN[d.last_who]}</span> выпало <b>${d.last[0] + d.last[1]}</b><small>${d.last[0]} + ${d.last[1]}</small></div>`;
  else if (d.turn) info = `<div class="dstat ${cls(d.turn)}">${WN[d.turn]} бросает кости</div>`;
  const win = d.winner ? `<div class="final ${d.winner === 'man' ? 'm' : d.winner === 'woman' ? 'w' : ''} ${hold ? 'hold' : ''}">${d.winner === 'draw' ? 'Ничья — переигрываем' : 'Победил ' + WN[d.winner] + '!'}</div>` : '';
  return `<div class="cattitle"><span class="ic">🎲</span>Бонус игра</div>${cubeHtml(d.last || [5, 3])}${d.last && d.winner ? info + win : info || win}`;
}
/* В бонус игре боковые табло показывают результаты кубиков, а не общий счёт; новый бросок появляется, когда кубики улеглись. */
function sideUpdate(){
  const s = S; if (!s) return;
  const nm = document.getElementById('nm'), nw = document.getElementById('nw'), um = document.getElementById('um'), uw = document.getElementById('uw');
  clearTimeout(sideTimer);
  if (s.mode === 'dice' && s.dice) {
    const d = s.dice;
    if (phys.mode !== 'idle' || performance.now() < dropUntil) { sideTimer = setTimeout(sideUpdate, 250); return; }
    stage.querySelectorAll('.hold').forEach(e => e.classList.remove('hold'));
    const txt = (p, t) => `${p.bust ? 'ПЕРЕБОР!' : p.stand ? 'хватит' : t ? 'бросает…' : 'ждёт'}<br>${p.rolls.join(' + ')}`;
    setRoll(nm, d.p.man.total); setRoll(nw, d.p.woman.total);
    um.innerHTML = txt(d.p.man, d.turn === 'man'); uw.innerHTML = txt(d.p.woman, d.turn === 'woman');
    const wk = d.winner ? d.winner + ':' + d.ev.length : '';
    if (wk && wk !== winKey && unlocked) sfx.win();
    winKey = wk;
  } else {
    setRoll(nm, s.score.man); setRoll(nw, s.score.woman); um.textContent = uw.textContent = 'очков'; winKey = '';
  }
}
/* Анимация выбора: категория «пробегает» по плиткам и останавливается на выбранной; затем выбранный игрок (Man/Woman) вспыхивает. */
let busyUntil = 0, busyTimer = null, lastView = 'menu', sweptKey = '', choseKey = '';
const viewOf = s => s.mode === 'pick' && s.pick ? 'pick' : s.mode === 'q' && s.cur ? 'q' : s.mode === 'dice' && s.dice ? 'dice' : 'menu';
function later(ms){ busyUntil = performance.now() + ms; clearTimeout(busyTimer); busyTimer = setTimeout(render, ms + 30); }
function sweepTiles(idx){
  const tiles = [...stage.querySelectorAll('.tile')]; if (!tiles.length || idx < 0) return 0;
  const seq = []; for (let i = 0; i < tiles.length; i++) seq.push(i); for (let i = 0; i <= idx; i++) seq.push(i);
  const step = 80; let k = 0;
  const t = setInterval(() => {
    tiles.forEach(x => x.classList.remove('sweep'));
    if (k >= seq.length) { clearInterval(t); tiles[idx].classList.add('chosen'); if (unlocked) sfx.pick(); return; }
    tiles[seq[k]].classList.add('sweep'); if (unlocked) sfx.tick(k); k++;
  }, step);
  return seq.length * step + 700;
}
function choseWho(who){
  const el = stage.querySelector('.pickside.' + cls(who)); if (!el) return 0;
  stage.querySelectorAll('.pickside').forEach(x => x.classList.add(x === el ? 'chosen' : 'dim'));
  if (unlocked) sfx.pick();
  return 900;
}
function render(){
  const s = S; if (!s) return;
  try {
    document.getElementById('sm').classList.toggle('on', !!(s.cur && s.cur.who === 'man') || (s.dice && !s.dice.winner && s.dice.turn === 'man'));
    document.getElementById('sw').classList.toggle('on', !!(s.cur && s.cur.who === 'woman') || (s.dice && !s.dice.winner && s.dice.turn === 'woman'));
    sideUpdate();
    const key = JSON.stringify([s.mode, s.pick, s.cur && [s.cur.cat, s.cur.who, s.cur.revealed, s.cur.started_at, s.cur.audio && s.cur.audio.kind], s.results, s.dice, s.score]);
    if (performance.now() < busyUntil) return;                 // идёт анимация выбора: итог покажем сразу после неё
    if (key !== lastKey) {
      const v = viewOf(s);
      if (v === 'menu') sweptKey = ''; if (v === 'pick') choseKey = '';
      if (v === 'pick' && lastView === 'menu' && sweptKey !== s.pick) {
        sweptKey = s.pick; const ms = sweepTiles(s.cats.findIndex(c => c.id === s.pick)); if (ms) { later(ms); return; }
      }
      if (v === 'q' && lastView === 'pick' && choseKey !== s.cur.cat + s.cur.who) {
        choseKey = s.cur.cat + s.cur.who; const ms = choseWho(s.cur.who); if (ms) { later(ms); return; }
      }
      lastKey = key;
      const changed = v !== lastView; lastView = v;
      stage.innerHTML = s.mode === 'pick' && s.pick ? pickHtml(s) : s.mode === 'q' && s.cur ? questionHtml(s) : s.mode === 'dice' && s.dice ? diceHtml(s) : menuHtml(s);
      if (changed) { stage.classList.remove('enter'); void stage.offsetWidth; stage.classList.add('enter'); }
      // звуки событий
      const res = s.cur ? s.results[s.cur.cat][s.cur.who] : null;
      const rk = s.cur ? s.cur.cat + s.cur.who + ':' + res : '';
      if (unlocked && rk !== prevRes && res) (res === 'ok' ? sfx.ok : sfx.bad)();
      prevRes = rk;
      if (s.dice) {
        const d = s.dice, wasLive = phys.mode === 'live';
        if (d.seq !== prevRoll) {
          if (prevRoll !== -1 && d.last && !wasLive) {          // бросок без тряски: кубик падает в квадрат
            stage.querySelectorAll('.cube3').forEach((cube, i) => { cube.style.transition = 'none'; cube.style.transform = poseCss(d.last[i], -1); void cube.offsetWidth; cube.style.transition = ''; cube.style.transform = poseCss(d.last[i]); });
            stage.querySelectorAll('.die3').forEach((die, i) => { die.style.animationDelay = (i * .12) + 's'; die.classList.add('drop'); });
            dropUntil = performance.now() + 1400;
            if (unlocked) { sfx.roll(); setTimeout(sfx.roll, 450); }
          }
          prevRoll = d.seq;
        }
        physSync(d);
        if (d.shaking && !rattle) rattle = setInterval(() => { if (unlocked) sfx.rattle(); }, 230);
        if (!d.shaking && rattle) { clearInterval(rattle); rattle = 0; }
        sideUpdate();
      } else { prevRoll = -1; if (rattle) { clearInterval(rattle); rattle = 0; } }
    }
    playFrom(s);
    tick();
  } catch (e) { console.error(e); }
}
function tick(){
  const el = document.getElementById('big'); if (!el || !S || !S.cur) return;
  const t = S.cur.time, st = S.cur.started_at;
  const v = st ? Math.max(0, Math.ceil(t - (serverNow() - st))) : t;
  setRoll(el, String(v), {up: false});
  el.classList.toggle('low', !!st && v <= 5);
  if (st && v === 0 && timeFired !== String(st)) { timeFired = String(st); if (unlocked) sfx.time(); }
}
setInterval(tick, 200);
window.onState = render;
</script>
</body></html>
"""


# Версия страниц: меняется при любой правке кода/вёрстки, клиенты по ней понимают, что пора обновиться.
VER = hashlib.md5((CONTROL_HTML + SCREEN_HTML + FINAL_CSS + NET_JS + ROLL_JS + ROLL_CSS + json.dumps(CATS, ensure_ascii=False)).encode("utf-8")).hexdigest()[:8]
