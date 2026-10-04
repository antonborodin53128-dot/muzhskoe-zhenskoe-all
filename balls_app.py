"""Шарики — конкурс «Мужское / Женское».

Участник бьёт теннисным шариком об пол и ловит его стаканом, затем вставляет
сверху новый стакан и повторяет. Ведущий нажимает +1 за каждый пойманный шарик.

  /         — пульт ведущего (телефон)
  /screen   — экран для гостей (проектор)

Обновления идут мгновенно через Socket.IO. Все ссылки относительные, поэтому
этот же файл работает и отдельно, и внутри общего сборника (/men/balls/...).
"""
import os
import time
from threading import Lock

from flask import Flask, render_template_string, request
from flask_socketio import SocketIO, emit

PREP_SECONDS = int(os.environ.get("PREP_SECONDS", 5))
ROUND_SECONDS = int(os.environ.get("ROUND_SECONDS", 40))
MAX_PARTICIPANTS = 30

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
lock = Lock()

state = {
    "participants": [],   # [{"name": str, "score": int, "done": bool}]
    "current": -1,
    "finished": False,
    "started_at": None,   # момент нажатия «Запустить время»; дальше отсчёт и раунд
    "bump": 0,            # счётчик изменений очков, чтобы экран знал, что анимировать
}


def phase_locked(now=None):
    now = now or time.time()
    if not state["participants"]:
        return "idle"
    if state["finished"]:
        return "finished"
    if state["started_at"] is None:
        return "ready"
    elapsed = now - state["started_at"]
    if elapsed < PREP_SECONDS:
        return "countdown"
    if elapsed < PREP_SECONDS + ROUND_SECONDS:
        return "play"
    return "timeup"


def snapshot_locked():
    return {
        "participants": [dict(p) for p in state["participants"]],
        "current": state["current"],
        "finished": state["finished"],
        "started_at": state["started_at"],
        "bump": state["bump"],
        "prep": PREP_SECONDS,
        "round": ROUND_SECONDS,
        "server_now": time.time(),
    }


def broadcast_locked():
    socketio.emit("state", snapshot_locked())


def current_player_locked():
    i = state["current"]
    if 0 <= i < len(state["participants"]):
        return state["participants"][i]
    return None


# ---------- события от пульта ----------

@socketio.on("connect")
def on_connect(data=None):
    with lock:
        emit("state", snapshot_locked())


@socketio.on("sync")
def on_sync(data=None):
    with lock:
        emit("state", snapshot_locked())


@socketio.on("setup")
def on_setup(data=None):
    try:
        count = int((data or {}).get("count", 4))
    except (TypeError, ValueError):
        count = 4
    count = max(1, min(MAX_PARTICIPANTS, count))
    with lock:
        state.update(
            participants=[{"name": f"Участник {i + 1}", "score": 0, "done": False} for i in range(count)],
            current=0, finished=False, started_at=None,
        )
        state["bump"] += 1
        broadcast_locked()


@socketio.on("start_timer")
def on_start_timer(data=None):
    with lock:
        if phase_locked() == "ready":
            state["started_at"] = time.time()
            broadcast_locked()


@socketio.on("score")
def on_score(data=None):
    try:
        delta = int((data or {}).get("delta", 0))
    except (TypeError, ValueError):
        return
    if delta not in (1, -1):
        return
    with lock:
        player = current_player_locked()
        if not player or state["started_at"] is None:
            return
        if phase_locked() not in ("play", "timeup"):
            return
        player["score"] = max(0, player["score"] + delta)
        state["bump"] += 1
        broadcast_locked()


@socketio.on("replay")
def on_replay(data=None):
    """Переиграть раунд текущего участника (например, время запустили случайно)."""
    with lock:
        player = current_player_locked()
        if player and not state["finished"]:
            player.update(score=0, done=False)
            state["started_at"] = None
            state["bump"] += 1
            broadcast_locked()


@socketio.on("next")
def on_next(data=None):
    with lock:
        player = current_player_locked()
        if not player or state["finished"]:
            return
        player["done"] = True
        if state["current"] < len(state["participants"]) - 1:
            state["current"] += 1
        else:
            state["finished"] = True
        state["started_at"] = None
        broadcast_locked()


@socketio.on("rename")
def on_rename(data=None):
    data = data or {}
    try:
        i = int(data.get("index", -1))
    except (TypeError, ValueError):
        return
    name = " ".join(str(data.get("name", "")).split())[:32]
    with lock:
        if 0 <= i < len(state["participants"]):
            state["participants"][i]["name"] = name or f"Участник {i + 1}"
            broadcast_locked()


@socketio.on("reset")
def on_reset(data=None):
    with lock:
        state.update(participants=[], current=-1, finished=False, started_at=None)
        state["bump"] += 1
        broadcast_locked()


# ---------- страницы ----------

def base_path():
    # "/" отдельно или "/men/balls/" внутри сборника
    return (request.script_root or "") + "/"


@app.after_request
def no_cache(resp):
    if request.path in ("/", "/screen"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/")
def control():
    return render_template_string(CONTROL_HTML, base=base_path(), theme_css=THEME_CSS, client_js=CLIENT_JS)


@app.get("/screen")
def screen():
    return render_template_string(SCREEN_HTML, base=base_path(), theme_css=THEME_CSS, client_js=CLIENT_JS)


# ======================================================================
# Общий стиль конкурсов «Мужское / Женское».
# Тема задаётся атрибутом data-theme="men" | "women" на <html>.
# ======================================================================
THEME_CSS = r"""
:root,[data-theme=men]{
  --ink:#06140e; --ink-2:#0a1f16; --surface:#0d261b; --line:#1d4734;
  --signal:#2bf08a; --signal-ink:#02140a; --signal-soft:rgba(43,240,138,.14);
  --chalk:#f1f5ee; --mist:#8da698; --danger:#ff8e9c; --danger-bg:#2d1419;
}
[data-theme=women]{
  --ink:#140710; --ink-2:#1d0b17; --surface:#26101f; --line:#4e1d3b;
  --signal:#ff4fa8; --signal-ink:#22000f; --signal-soft:rgba(255,79,168,.14);
  --chalk:#f8eff4; --mist:#b394a6; --danger:#ffb08e; --danger-bg:#2d1714;
}
:root{
  --display:"Unbounded",system-ui,sans-serif;
  --ui:"Onest",system-ui,sans-serif;
  --r-s:12px; --r-m:18px; --r-l:28px;
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--ink);color:var(--chalk);font-family:var(--ui);-webkit-font-smoothing:antialiased}
body{min-height:100vh;min-height:100dvh}
button{font:inherit;color:inherit;cursor:pointer;-webkit-tap-highlight-color:transparent}
button:focus-visible,a:focus-visible{outline:3px solid var(--signal);outline-offset:3px}
.num{font-family:var(--digits);font-weight:var(--digits-w);font-variant-numeric:tabular-nums;font-feature-settings:"tnum"}
/* Шрифт цифр — Nunito: скруглённые края */
:root{--digits:"Nunito",var(--display);--digits-w:900}
/* Табло: каждая цифра прокручивается в своём окошке */
.roll{display:inline-flex;align-items:flex-start;line-height:1;--cell:1.08em;height:var(--cell);vertical-align:top;
  -webkit-mask-image:linear-gradient(transparent,#000 14%,#000 86%,transparent);mask-image:linear-gradient(transparent,#000 14%,#000 86%,transparent)}
.roll .d{display:inline-block;height:var(--cell);overflow:hidden}
.roll .s{display:flex;flex-direction:column;transition:transform var(--roll-ms,560ms) cubic-bezier(.22,1.18,.36,1)}
.roll .s>span{height:var(--cell);line-height:var(--cell);text-align:center}
.roll .sep{height:var(--cell);line-height:var(--cell);padding:0 .02em}
.roll .d.in{animation:digitIn .45s cubic-bezier(.2,.9,.3,1.2)}
@keyframes digitIn{from{transform:translateY(-.4em);opacity:0}}
/* Фон экрана для гостей */
#bg{position:fixed;inset:0;width:100%;height:100%;z-index:0;pointer-events:none}
.screen{position:relative;z-index:1}
.wordmark{display:inline-flex;align-items:center;gap:.5em;font-family:var(--display);font-weight:800;letter-spacing:.02em}
.wordmark i{width:.62em;height:.62em;border-radius:50%;background:var(--signal);box-shadow:0 0 18px var(--signal)}
.offline{position:fixed;left:0;right:0;top:0;z-index:100;padding:10px 16px;text-align:center;font-weight:600;background:var(--danger-bg);color:var(--danger);transform:translateY(-100%);transition:transform .25s}
.offline.on{transform:none}
@media (prefers-reduced-motion:reduce){*,*:before,*:after{animation-duration:.01ms!important;transition-duration:.01ms!important}}
"""

# Общая логика клиента: подключение, синхронизация часов, вычисление фазы.
CLIENT_JS = r"""
const BASE = document.documentElement.dataset.base || '/';
const socket = io({path: BASE + 'socket.io', transports: ['websocket', 'polling']});
let S = null, clockOffset = 0;
const offlineBar = document.getElementById('offline');
socket.on('connect', () => offlineBar && offlineBar.classList.remove('on'));
socket.on('disconnect', () => offlineBar && offlineBar.classList.add('on'));
socket.on('state', s => { clockOffset = s.server_now - Date.now() / 1000; S = s; window.onState && window.onState(s); });
document.addEventListener('visibilitychange', () => { if (!document.hidden) socket.emit('sync'); });
function serverNow(){ return Date.now() / 1000 + clockOffset; }
function phaseOf(s){
  if (!s || !s.participants.length) return 'idle';
  if (s.finished) return 'finished';
  if (s.started_at == null) return 'ready';
  const t = serverNow() - s.started_at;
  if (t < s.prep) return 'countdown';
  if (t < s.prep + s.round) return 'play';
  return 'timeup';
}
function remaining(s){
  const ph = phaseOf(s), t = s.started_at == null ? 0 : serverNow() - s.started_at;
  if (ph === 'countdown') return s.prep - t;
  if (ph === 'play') return s.prep + s.round - t;
  if (ph === 'ready') return s.round;
  return 0;
}
function fmtTime(sec){ sec = Math.max(0, Math.ceil(sec)); return Math.floor(sec / 60) + ':' + String(sec % 60).padStart(2, '0'); }
/* ---------- Табло ----------
   setRoll(el, '0:27') — каждая цифра крутится к новому значению.
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

/* ---------- Фон ---------- */
const Bg = (() => {
  const params = new URLSearchParams(location.search);
  const html = document.documentElement;
  const kind = params.get('bg') || html.dataset.bg || 'bokeh';
  const cv = document.getElementById('bg');
  if (!cv) return {pulse(){}};
  const ctx = cv.getContext('2d');
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  let W = 0, H = 0, dpr = 1, energy = 0, t0 = performance.now();
  const color = () => getComputedStyle(html).getPropertyValue('--signal').trim() || '#2bf08a';
  let rgb = [43, 240, 138];
  function readColor(){ const c = color(); const m = c.match(/^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i); if (m) rgb = m.slice(1).map(h => parseInt(h, 16)); }
  const rgba = a => `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${a})`;
  function size(){ dpr = Math.min(2, devicePixelRatio || 1); W = innerWidth; H = innerHeight; cv.width = W * dpr; cv.height = H * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0); }
  addEventListener('resize', size); size(); readColor();

  // «Студия»: скруглённые рамки уходят вглубь, как светодиодные контуры на сцене
  function studio(t){
    const cx = W / 2, cy = H * .5, N = 16, speed = .045;
    for (let i = 0; i < N; i++) {
      const k = ((i / N) + t * speed) % 1;                   // 0 — в глубине, 1 — у зрителя
      const z = Math.pow(k, 2.2);
      const w = 60 + z * W * 1.35, h = 34 + z * H * 1.35, r = 14 + z * 120;
      const wave = Math.max(0, 1 - Math.abs(k - (1 - energyPhase)) * 6) * energy;
      ctx.strokeStyle = rgba(Math.min(.55, .05 + k * .16 + wave * .5));
      ctx.lineWidth = .8 + z * 2.2 + wave * 3;
      ctx.beginPath(); ctx.roundRect(cx - w / 2, cy - h / 2, w, h, r); ctx.stroke();
    }
  }
  // «Волны»: тонкие линии плавно текут через экран
  function waves(t){
    const N = 11;
    for (let i = 0; i < N; i++) {
      const y0 = H * (.12 + i * .078), amp = 18 + 22 * Math.sin(i * 1.7) + energy * 60;
      ctx.strokeStyle = rgba(.13 + .14 * (i % 3 === 1) + energy * .3);
      ctx.lineWidth = 1.4 + (i % 3 === 1) * 1.2;
      ctx.beginPath();
      for (let x = -10; x <= W + 10; x += 14) {
        const y = y0 + Math.sin(x * .0042 + t * (.35 + i * .03) + i) * amp + Math.sin(x * .011 - t * .6 + i * 2) * amp * .25;
        x < 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
  }
  // «Сетка»: пол сцены в перспективе уходит к горизонту
  function grid(t){
    const hz = H * .56, cx = W / 2;
    const g = ctx.createLinearGradient(0, hz - 2, 0, hz + 2); 
    ctx.strokeStyle = rgba(.35 + energy * .4); ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(0, hz); ctx.lineTo(W, hz); ctx.stroke();
    for (let i = -14; i <= 14; i++) {
      ctx.strokeStyle = rgba(.08 + energy * .2); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(cx + i * 22, hz); ctx.lineTo(cx + i * W * .16, H + 40); ctx.stroke();
    }
    const N = 14;
    for (let j = 0; j < N; j++) {
      const k = ((j / N) + t * .08) % 1, y = hz + Math.pow(k, 2.4) * (H - hz + 40);
      ctx.strokeStyle = rgba(.04 + k * .18 + energy * .25); ctx.lineWidth = .8 + k * 1.6;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    }
    // мягкое свечение над горизонтом
    const glow = ctx.createRadialGradient(cx, hz, 0, cx, hz, W * .5);
    glow.addColorStop(0, rgba(.12 + energy * .15)); glow.addColorStop(1, rgba(0));
    ctx.fillStyle = glow; ctx.fillRect(0, 0, W, H);
  }
  // «Прожекторы»: лучи сверху качаются над сценой
  function spots(t){
    const beams = [[.12, .9, 0], [.38, 1.2, 1.7], [.62, 1.05, 3.1], [.88, .8, 4.6]];
    ctx.globalCompositeOperation = 'lighter';
    beams.forEach(([x, sp, ph], i) => {
      const ox = W * x, oy = -H * .08, ang = Math.sin(t * .35 * sp + ph) * .42 + (x - .5) * -.5;
      const len = H * 1.35, half = .13 + energy * .05;
      const ex = ox + Math.sin(ang) * len, ey = oy + Math.cos(ang) * len;
      const g = ctx.createLinearGradient(ox, oy, ex, ey);
      g.addColorStop(0, rgba(.22 + energy * .25)); g.addColorStop(.7, rgba(.05 + energy * .08)); g.addColorStop(1, rgba(0));
      ctx.fillStyle = g; ctx.beginPath(); ctx.moveTo(ox, oy);
      ctx.lineTo(ox + Math.sin(ang - half) * len, oy + Math.cos(ang - half) * len);
      ctx.lineTo(ox + Math.sin(ang + half) * len, oy + Math.cos(ang + half) * len);
      ctx.closePath(); ctx.fill();
      // пятно света на полу
      const fx = ox + Math.tan(ang) * (H * .92 - oy), fy = H * .92;
      const pg = ctx.createRadialGradient(fx, fy, 0, fx, fy, W * .12);
      pg.addColorStop(0, rgba(.16 + energy * .2)); pg.addColorStop(1, rgba(0));
      ctx.fillStyle = pg; ctx.beginPath(); ctx.ellipse(fx, fy, W * .12, H * .04, 0, 0, Math.PI * 2); ctx.fill();
    });
    ctx.globalCompositeOperation = 'source-over';
  }
  // «Круги»: от центра расходятся кольца, как от удара шарика; на +1 — новая волна
  const rings = [];
  let nextRing = 0;
  function ripples(t){
    const cx = W / 2, cy = H * .48, maxR = Math.hypot(W, H) * .6;
    if (t > nextRing) { rings.push({t0: t, strong: 0}); nextRing = t + 1.6; }
    if (energy > .95 && (!rings.length || rings[rings.length - 1].t0 < t - .1)) rings.push({t0: t, strong: 1});
    for (let i = rings.length - 1; i >= 0; i--) {
      const r = rings[i], k = (t - r.t0) / (r.strong ? 2.2 : 7);
      if (k > 1) { rings.splice(i, 1); continue; }
      const rad = 40 + Math.pow(k, r.strong ? .7 : 1) * maxR;
      ctx.strokeStyle = rgba((r.strong ? .55 : .2) * (1 - k));
      ctx.lineWidth = r.strong ? 6 * (1 - k) + 1 : 1.5;
      ctx.beginPath(); ctx.arc(cx, cy, rad, 0, Math.PI * 2); ctx.stroke();
    }
  }
  // «Огоньки»: размытые огни медленно всплывают; на +1 — вспыхивают ярче
  const dots = Array.from({length: 18}, (_, i) => ({x: Math.random(), y: Math.random(), r: 10 + Math.random() * 34, v: .01 + Math.random() * .025, a: .1 + Math.random() * .2, w: Math.random() * 6}));
  function bokeh(t){
    ctx.globalCompositeOperation = 'lighter';
    dots.forEach(d => {
      const y = ((d.y - t * d.v) % 1 + 1) % 1, x = d.x + Math.sin(t * .3 + d.w) * .015;
      const px = x * W, py = y * H * 1.1 - H * .05, rr = d.r * (1 + flash * .35);
      const g = ctx.createRadialGradient(px, py, 0, px, py, rr);
      g.addColorStop(0, rgba(d.a + flash * .3)); g.addColorStop(1, rgba(0));
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(px, py, rr, 0, Math.PI * 2); ctx.fill();
    });
    ctx.globalCompositeOperation = 'source-over';
  }
  // «Лучи»: классическое «солнце» игрового шоу медленно вращается за цифрой
  function burst(t){
    const cx = W / 2, cy = H * .48, R = Math.hypot(W, H), N = 24, rot = t * .06;
    for (let i = 0; i < N; i++) {
      const a0 = rot + i * Math.PI * 2 / N, a1 = a0 + Math.PI / N;
      const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, R * .55);
      g.addColorStop(0, rgba(.14 + energy * .2)); g.addColorStop(1, rgba(.015));
      ctx.fillStyle = g; ctx.beginPath(); ctx.moveTo(cx, cy);
      ctx.arc(cx, cy, R, a0, a1); ctx.closePath(); ctx.fill();
    }
    const v = ctx.createRadialGradient(cx, cy, Math.min(W, H) * .15, cx, cy, R * .6);
    v.addColorStop(0, 'rgba(0,0,0,0)'); v.addColorStop(1, 'rgba(0,0,0,.55)');
    ctx.fillStyle = v; ctx.fillRect(0, 0, W, H);
  }
  const draw = {studio, waves, grid, spots, ripples, bokeh, burst}[kind] || studio;
  let energyPhase = 0, flash = 0, flashTarget = 0;
  function loop(now){
    const t = reduce ? 0 : (now - t0) / 1000;
    ctx.clearRect(0, 0, W, H);
    draw(t);
    energy *= .94; energyPhase = Math.min(1, energyPhase + .03);
    flash += (flashTarget - flash) * .07; flashTarget *= .975;   // ~0,5 с разгорается, ~2 с гаснет
    requestAnimationFrame(loop);
  }
  requestAnimationFrame(loop);
  return { pulse(){ energy = 1; energyPhase = 0; flashTarget = 1; }, recolor: readColor };
})();

function esc(v){ return String(v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function ranking(s){ return s.participants.map((p, i) => ({...p, i})).filter(p => p.done).sort((a, b) => b.score - a.score || a.i - b.i); }
function cupsWord(n){ const a = n % 10, b = n % 100; if (a === 1 && b !== 11) return 'шарик'; if (a >= 2 && a <= 4 && (b < 12 || b > 14)) return 'шарика'; return 'шариков'; }
"""

FONTS = """<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Onest:wght@400;600;800&family=Unbounded:wght@600;800;900&family=Nunito:wght@800;900&display=swap" rel="stylesheet">
<script src="https://cdn.socket.io/4.8.1/socket.io.min.js"></script>"""

# ======================================================================
# Пульт ведущего
# ======================================================================
CONTROL_HTML = r"""<!doctype html>
<html lang="ru" data-theme="men" data-base="{{ base }}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#06140e"><title>Шарики · пульт</title>
""" + FONTS + r"""
<style>{{ theme_css|safe }}
body{background:radial-gradient(120% 60% at 0 0,var(--ink-2),var(--ink) 60%)}
.app{max-width:520px;margin:0 auto;padding:16px 16px calc(24px + env(safe-area-inset-bottom));display:flex;flex-direction:column;gap:14px;min-height:100dvh}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px}
.top .wordmark{font-size:20px}
.link{color:var(--mist);font-weight:600;font-size:14px;text-decoration:underline;text-underline-offset:3px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-l);padding:20px}
h2{margin:0 0 14px;font-size:17px;font-weight:600;color:var(--mist)}
.stepper{display:grid;grid-template-columns:72px 1fr 72px;align-items:center;gap:10px;margin-bottom:16px}
.stepper button{height:72px;border-radius:var(--r-m);border:1px solid var(--line);background:var(--ink-2);font-size:30px;font-weight:600}
.stepper .num{text-align:center;font-size:52px;}
.btn{display:block;width:100%;border:0;border-radius:var(--r-m);padding:18px;font-size:19px;font-weight:800}
.btn.primary{background:var(--signal);color:var(--signal-ink)}
.btn.quiet{background:transparent;border:1px solid var(--line);color:var(--chalk);font-weight:600}
.btn.danger{background:transparent;border:1px solid var(--danger-bg);color:var(--danger);font-weight:600;font-size:15px;padding:14px}
.btn:disabled{opacity:.4;cursor:default}
.btn:active:not(:disabled){transform:scale(.98)}
.who{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.who .name{font-family:var(--display);font-size:26px;font-weight:800}
.who .of{color:var(--mist);font-weight:600;white-space:nowrap}
.status{display:flex;justify-content:space-between;align-items:center;margin:16px 0 12px;padding:14px 16px;border-radius:var(--r-m);background:var(--ink-2)}
.status .label{color:var(--mist);font-weight:600}
.status .time{font-size:30px;}
.status.hot .time{color:var(--signal)}
.status.end .time{color:var(--chalk)}
.score{text-align:center;padding:4px 0 14px}
.score .num{font-size:96px;line-height:1;color:var(--signal)}
.score .unit{color:var(--mist);font-weight:600;margin-top:4px}
.pad{display:grid;grid-template-columns:1fr 92px;gap:10px}
.plus{height:min(34vh,260px);border:0;border-radius:var(--r-l);background:var(--signal);color:var(--signal-ink);font-family:var(--display);font-size:64px;font-weight:900;touch-action:manipulation}
.minus{border-radius:var(--r-l);border:1px solid var(--line);background:var(--ink-2);font-family:var(--display);font-size:28px;font-weight:800;touch-action:manipulation}
.plus:disabled,.minus:disabled{opacity:.3}
.plus:active:not(:disabled){transform:scale(.97);filter:brightness(1.1)}
.stack{display:flex;flex-direction:column;gap:10px}
.rows{display:flex;flex-direction:column}
.row{display:flex;justify-content:space-between;padding:12px 2px;border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:0}
.row b{font-family:var(--digits);font-weight:var(--digits-w);color:var(--signal)}
.row.first b{font-size:20px}
.empty{color:var(--mist)}
.hint{color:var(--mist);font-size:14px;text-align:center}
.spacer{flex:1}
.namelink{align-self:center;background:none;border:0;color:var(--mist);font-size:14px;font-weight:600;text-decoration:underline;text-underline-offset:3px;padding:8px}
.fields{display:flex;flex-direction:column;gap:8px}
.fields label{display:grid;grid-template-columns:2em 1fr;align-items:center;gap:8px;color:var(--mist);font-weight:600}
.fields input{width:100%;min-width:0;background:var(--ink-2);border:1px solid var(--line);border-radius:var(--r-s);color:var(--chalk);font:inherit;font-weight:600;padding:12px 14px}
.fields input:focus{outline:2px solid var(--signal);outline-offset:1px}
[hidden]{display:none!important}
</style></head>
<body>
<div class="offline" id="offline">Нет связи с сервером — переподключаюсь…</div>
<main class="app">
  <div class="top"><span class="wordmark"><i></i>Шарики</span><a class="link" href="{{ base }}screen" target="_blank" rel="noopener">Экран для гостей</a></div>

  <section class="card" id="setup">
    <h2>Сколько участников</h2>
    <div class="stepper"><button id="minusCount" aria-label="Меньше">−</button><div class="num" id="count">4</div><button id="plusCount" aria-label="Больше">+</button></div>
    <button class="btn primary" id="begin">Начать конкурс</button>
  </section>

  <section class="card" id="game" hidden>
    <div class="who"><span class="name" id="name"></span><span class="of" id="of"></span></div>
    <div class="status" id="status"><span class="label" id="statusLabel"></span><span class="time num" id="time"></span></div>
    <div class="score"><div class="num" id="score">0</div><div class="unit" id="unit">шариков</div></div>
    <div id="actions"></div>
  </section>

  <section class="card" id="final" hidden>
    <div class="who"><span class="name">Конкурс завершён</span></div>
    <p class="hint" style="text-align:left;margin:8px 0 0">Итоги уже на экране для гостей.</p>
  </section>

  <section class="card" id="results" hidden><h2 id="resultsTitle">Уже сыграли</h2><div class="rows" id="rows"></div></section>

  <section class="card" id="names" hidden>
    <h2>Имена участников</h2>
    <div class="fields" id="nameFields"></div>
    <p class="hint" style="text-align:left;margin:10px 0 0">Пустое поле — снова «Участник N». Имя сохраняется сразу.</p>
  </section>

  <div class="spacer"></div>
  <button class="namelink" id="namesToggle" hidden>Имена участников</button>
  <button class="btn danger" id="reset" hidden>Сбросить конкурс</button>
</main>
<script>{{ client_js|safe }}</script>
<script>
const $ = id => document.getElementById(id);
let count = 4, lastPhase = null, lastKey = '';
$('minusCount').onclick = () => { count = Math.max(1, count - 1); $('count').textContent = count; };
$('plusCount').onclick = () => { count = Math.min(30, count + 1); $('count').textContent = count; };
$('begin').onclick = () => socket.emit('setup', {count});
let namesOpen = false, namesKey = '';
$('namesToggle').onclick = () => { namesOpen = !namesOpen; namesKey = ''; $('names').hidden = !namesOpen; $('namesToggle').textContent = namesOpen ? 'Скрыть имена' : 'Имена участников'; if (namesOpen) $('names').scrollIntoView({behavior: 'smooth', block: 'start'}); };
function renderNames(){
  if (!namesOpen || !S) return;
  const key = S.participants.length + ':' + S.current;
  const box = $('nameFields');
  if (key !== namesKey) {   // поля пересобираются только при смене состава, чтобы не мешать вводу
    namesKey = key;
    box.innerHTML = S.participants.map((p, i) => `<label><span class="num">${i + 1}</span><input data-i="${i}" maxlength="32" placeholder="Участник ${i + 1}" value="${esc(/^Участник \d+$/.test(p.name) ? '' : p.name)}"></label>`).join('');
  }
  box.querySelectorAll('input').forEach(inp => {
    if (document.activeElement === inp) return;
    const p = S.participants[+inp.dataset.i]; const v = /^Участник \d+$/.test(p.name) ? '' : p.name;
    if (inp.value !== v) inp.value = v;
  });
}
$('nameFields').addEventListener('change', e => { const inp = e.target.closest('input'); if (inp) socket.emit('rename', {index: +inp.dataset.i, name: inp.value}); });
$('nameFields').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); e.target.blur(); } });
$('reset').onclick = () => { if (confirm('Сбросить конкурс? Все результаты удалятся.')) socket.emit('reset'); };

function act(name, data){ socket.emit(name, data || {}); }
function scoreTap(delta){
  if (!['play', 'timeup'].includes(phaseOf(S))) return;
  if (navigator.vibrate) navigator.vibrate(delta > 0 ? 18 : [10, 40, 10]);
  act('score', {delta});
}
document.addEventListener('keydown', e => {
  if (e.repeat || !S || e.target.closest('input')) return;
  if (e.code === 'Space' || e.key === '+' || e.key === '=') { e.preventDefault(); scoreTap(1); }
  if (e.key === '-' || e.key === '_') { e.preventDefault(); scoreTap(-1); }
});

function renderActions(ph, isLast){
  // Перерисовываем кнопки только при смене фазы, чтобы нажатие не «съедалось»
  const key = ph + (isLast ? 'L' : '');
  if (key === lastKey) return;
  lastKey = key;
  const a = $('actions');
  if (ph === 'ready') {
    a.innerHTML = `<div class="stack"><button class="btn primary" data-act="start_timer">Запустить время</button>
      <p class="hint">${S.prep} секунд отсчёта, потом ${S.round} секунд игры</p></div>`;
  } else if (ph === 'countdown' || ph === 'play') {
    a.innerHTML = `<div class="pad"><button class="plus" data-score="1" aria-label="Плюс один шарик">+1</button><button class="minus" data-score="-1" aria-label="Минус один">−1</button></div>`;
  } else if (ph === 'timeup') {
    a.innerHTML = `<div class="pad" style="margin-bottom:10px"><button class="plus" data-score="1" style="height:84px;font-size:32px" aria-label="Плюс один шарик">+1</button><button class="minus" data-score="-1" aria-label="Минус один">−1</button></div>
      <p class="hint" style="margin:0 0 10px">Можно поправить счёт до перехода к следующему</p><div class="stack"><button class="btn primary" data-act="next">${isLast ? 'Показать итоги' : 'Следующий участник'}</button>
      <button class="btn quiet" data-act="replay">Переиграть раунд</button></div>`;
  } else a.innerHTML = '';
}
$('actions').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b || b.disabled) return;
  if (b.dataset.score) scoreTap(+b.dataset.score);
  if (b.dataset.act === 'replay' && !confirm('Обнулить счёт и переиграть раунд?')) return;
  if (b.dataset.act) act(b.dataset.act);
});

function tick(){
  if (S) {
    const ph = phaseOf(S), p = S.participants[S.current];
    $('setup').hidden = ph !== 'idle';
    $('game').hidden = !(p && !S.finished);
    $('final').hidden = ph !== 'finished';
    $('reset').hidden = ph === 'idle';
    $('namesToggle').hidden = ph === 'idle';
    if (ph === 'idle') { namesOpen = false; $('names').hidden = true; $('namesToggle').textContent = 'Имена участников'; }
    renderNames();
    if (p && !S.finished) {
      const isLast = S.current === S.participants.length - 1;
      $('name').textContent = p.name;
      $('of').textContent = (S.current + 1) + ' из ' + S.participants.length;
      setRoll($('score'), p.score);
      $('unit').textContent = cupsWord(p.score);
      const st = $('status'), r = remaining(S);
      st.className = 'status' + (ph === 'play' ? ' hot' : ph === 'timeup' ? ' end' : '');
      $('statusLabel').textContent = {ready:'Ждём старта', countdown:'Отсчёт', play:'Идёт время', timeup:'Время вышло'}[ph];
      setRoll($('time'), ph === 'countdown' ? String(Math.ceil(r)) : ph === 'timeup' ? '0:00' : fmtTime(r), {up: false});
      renderActions(ph, isLast);
      document.querySelectorAll('[data-score]').forEach(b => b.disabled = ph === 'countdown');
    } else renderActions(ph, false);
    const done = ranking(S);
    $('results').hidden = !done.length;
    $('resultsTitle').textContent = ph === 'finished' ? 'Итоги' : 'Уже сыграли';
    $('reset').textContent = ph === 'finished' ? 'Начать новый конкурс' : 'Сбросить конкурс';
    $('rows').innerHTML = done.map((p, i) => `<div class="row${i === 0 ? ' first' : ''}"><span>${esc(p.name)}</span><b>${p.score}</b></div>`).join('');
  }
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);
</script></body></html>"""

# ======================================================================
# Экран для гостей
# ======================================================================
SCREEN_HTML = r"""<!doctype html>
<html lang="ru" data-theme="men" data-bg="bokeh" data-base="{{ base }}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Шарики</title>
""" + FONTS + r"""
<style>{{ theme_css|safe }}
html,body{height:100%;overflow:hidden}
body{background:radial-gradient(60% 70% at 50% 55%,var(--signal-soft),transparent 70%),radial-gradient(90% 80% at 0 0,var(--ink-2),var(--ink) 65%)}
.screen{height:100vh;display:grid;grid-template-rows:auto 1fr;padding:3.2vh 4.5vw 4vh}
.head{display:flex;justify-content:space-between;align-items:center}
.head .wordmark{font-size:clamp(20px,2vw,34px)}
.head .round{color:var(--mist);font-weight:600;font-size:clamp(16px,1.4vw,24px)}
/* --- игра: имя, огромный счёт, таймер --- */
.play{position:relative;display:flex;min-height:0;--side:clamp(220px,21vw,380px)}
/* счёт строго по центру: слева и справа одинаковые поля шириной с рейтинг */
.play>.game{flex:1;min-width:0;padding:0 calc(var(--side) + 2vw)}
.play .who{max-width:100%;overflow-wrap:anywhere}
.game{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;min-height:0}
/* результаты справа: сыгравшие + текущий участник вживую */
.side{position:absolute;right:0;top:50%;transform:translateY(-50%);width:var(--side);display:flex;flex-direction:column}
.side-h{color:var(--mist);font-weight:600;font-size:clamp(15px,1.3vw,24px);margin:0 0 1.4vh .2em}
.side-list{position:relative;--rh:clamp(38px,6.6vh,64px)}
.srow{position:absolute;left:0;right:0;top:0;height:var(--rh);display:grid;grid-template-columns:1.6em minmax(0,1fr) auto;align-items:center;gap:.7em;padding:0 .9em;border-radius:var(--r-m);
  background:rgba(13,38,27,.82);border:1px solid var(--line);font-size:clamp(14px,calc(var(--rh) * .38),26px);
  transition:transform .7s cubic-bezier(.2,.8,.2,1),opacity .4s,border-color .3s,background .3s;backdrop-filter:blur(2px)}
.srow .pl{color:var(--mist)}
.srow .pn{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:flex;align-items:center;gap:.5em}
.srow .sc{color:var(--chalk)}
.srow.lead .sc{color:var(--signal)}
.srow.now{border-color:var(--signal);background:rgba(43,240,138,.1)}
.srow.now .pn:before{content:"";flex:none;width:.5em;height:.5em;border-radius:50%;background:var(--signal);box-shadow:0 0 10px var(--signal);animation:live 1.4s ease-in-out infinite}
@keyframes live{50%{opacity:.35}}
.srow.gone{opacity:0}
.side-more{position:absolute;left:0;right:0;text-align:center;color:var(--mist);font-weight:600;line-height:1}
/* кнопка звука */
.sound{position:fixed;right:20px;bottom:20px;z-index:50;border:1px solid var(--line);background:rgba(13,38,27,.9);color:var(--chalk);border-radius:999px;padding:12px 20px;font-weight:600;font-size:16px;cursor:pointer;transition:opacity .4s}
.sound:hover{border-color:var(--signal)}
.sound.off{opacity:0;pointer-events:none}
.who{font-family:var(--display);font-weight:800;font-size:clamp(32px,4.2vw,80px);line-height:1}
.count{font-size:min(27vw,42vh);line-height:1;color:var(--signal);margin:1.5vh 0 0;filter:drop-shadow(0 0 40px var(--signal-soft))}
.unit{color:var(--mist);font-weight:600;font-size:clamp(20px,2vw,38px);margin-top:1vh}
.timer{margin-top:4vh;width:min(640px,46vw);min-height:clamp(60px,8vh,110px);display:flex;flex-direction:column;align-items:center;justify-content:center}
.timer .t{font-size:clamp(44px,4.6vw,88px);line-height:1;font-weight:800;--roll-ms:420ms}
.timer .bar{width:100%;height:10px;border-radius:5px;background:var(--line);margin-top:1.6vh;overflow:hidden}
.timer .bar i{display:block;height:100%;width:100%;background:var(--signal);border-radius:5px;transform-origin:0 50%}
.timer.last .t{color:var(--chalk);animation:beat 1s ease-in-out infinite}
.timer.last .bar i{background:var(--chalk)}
@keyframes beat{0%,100%{transform:scale(1)}15%{transform:scale(1.12)}}
.timer .msg{font-family:var(--display);font-weight:800;font-size:clamp(40px,4.6vw,90px);line-height:1}
.timer .wait{color:var(--mist);font-weight:600;font-size:clamp(20px,1.8vw,32px)}
/* --- полоса результатов --- */
.chip{display:flex;gap:12px;align-items:baseline;padding:10px 16px;border:1px solid var(--line);border-radius:var(--r-m);background:var(--surface);font-size:clamp(15px,1.3vw,22px);font-weight:600}
.chip b{font-family:var(--digits);font-weight:var(--digits-w);color:var(--signal)}
/* --- полноэкранные состояния --- */
.full{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;min-height:0}
.full .big{font-family:var(--display);font-weight:900;font-size:clamp(70px,10vw,190px);line-height:.95;letter-spacing:-.02em}
.full .sub{color:var(--mist);font-weight:600;font-size:clamp(20px,2vw,36px);margin-top:3vh}
.countnum{font-size:min(32vw,58vh);line-height:1;color:var(--chalk);filter:drop-shadow(0 0 40px var(--signal-soft));--roll-ms:520ms}
.countnum.go{animation:countPulse .95s cubic-bezier(.18,.8,.2,1) both}
@keyframes countPulse{0%{opacity:0;transform:scale(.55)}28%{opacity:1;transform:scale(1.1)}55%{transform:scale(1)}85%{opacity:1}100%{opacity:0;transform:scale(1.25)}}
/* --- итоги: таблица, подстраивается под число участников --- */
.final{display:flex;flex-direction:column;min-height:0}
.final h1{font-family:var(--display);font-weight:900;font-size:clamp(40px,4.6vw,88px);margin:1.5vh 0 2.5vh;line-height:1}
.board{flex:1;min-height:0;display:grid;grid-auto-flow:column;grid-template-rows:repeat(var(--rows),auto);grid-template-columns:repeat(var(--cols),minmax(0,1fr));column-gap:3vw;align-content:start}
.board{--rh:calc((100vh - 3.2vh*2 - 3vw - 14vh) / var(--rows))}
.trow{display:grid;grid-template-columns:2.2em minmax(0,1fr) auto;align-items:center;gap:.8em;padding:0 .9em;height:min(var(--rh) - 6px,14vh);margin-bottom:6px;border-radius:var(--r-m);background:var(--surface);border:1px solid var(--line);font-size:clamp(14px,calc(var(--rh) * .38),46px);opacity:0;animation:rise .45s cubic-bezier(.2,.8,.2,1) forwards}
@keyframes rise{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
.trow .pl{color:var(--mist)}
.trow .pn{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.trow .sc{--roll-ms:900ms}
.trow.win{background:var(--signal);border-color:var(--signal);color:var(--signal-ink)}
.trow.win .pl{color:var(--signal-ink)}
[hidden]{display:none!important}
</style></head>
<body>
<canvas id="bg" aria-hidden="true"></canvas>
<div class="offline" id="offline">Нет связи с сервером — переподключаюсь…</div>
<div class="screen">
  <div class="head"><span class="wordmark"><i></i>Шарики</span><span class="round" id="round"></span></div>

  <div class="full" id="idle"><div class="big">Шарики</div><div class="sub">Скоро начнём</div></div>

  <div class="full" id="countdown" hidden><div class="sub" id="cdName" style="margin:0 0 2vh"></div><div class="countnum num" id="cdNum"></div></div>

  <div class="play" id="game" hidden>
    <div class="game">
      <div class="who" id="who"></div>
      <div class="count num" id="count"></div>
      <div class="unit" id="unit">шариков</div>
      <div class="timer" id="timer"></div>
    </div>
    <aside class="side"><div class="side-h">Результаты</div><div class="side-list" id="sideList"></div></aside>
  </div>

  <div class="final" id="final" hidden><h1>Итоги</h1><div class="board" id="board"></div></div>
</div>
<button class="sound" id="soundBtn">Включить звук</button>
<script>{{ client_js|safe }}</script>
<script>
const $ = id => document.getElementById(id);
let shownScore = null, shownPlayer = null, lastView = '', lastFinalKey = '';
let lastPh = null, lastCd = null, lastTick = null;

/* ---------- Звуки: синтез в браузере, файлы не нужны ---------- */
const Snd = (() => {
  let ctx = null, master = null;
  function ensure(){
    if (!ctx) { ctx = new (window.AudioContext || window.webkitAudioContext)(); master = ctx.createGain(); master.gain.value = .8; master.connect(ctx.destination); }
    return ctx;
  }
  function tone(freq, dur, {type = 'sine', gain = .3, to = null, at = 0, attack = .005} = {}){
    if (!ctx || ctx.state !== 'running') return;
    const t = ctx.currentTime + at, o = ctx.createOscillator(), g = ctx.createGain();
    o.type = type; o.frequency.setValueAtTime(freq, t);
    if (to) o.frequency.exponentialRampToValueAtTime(to, t + dur);
    g.gain.setValueAtTime(0, t); g.gain.linearRampToValueAtTime(gain, t + attack);
    g.gain.exponentialRampToValueAtTime(.0001, t + dur);
    o.connect(g); g.connect(master); o.start(t); o.stop(t + dur + .05);
  }
  // короткий сухой щелчок: всплеск шума через полосовой фильтр
  let noise = null;
  function click(at, gain = .7, freq = 3000){
    if (!ctx || ctx.state !== 'running') return;
    if (!noise) { noise = ctx.createBuffer(1, Math.floor(ctx.sampleRate * .05), ctx.sampleRate); const ch = noise.getChannelData(0); for (let i = 0; i < ch.length; i++) ch[i] = Math.random() * 2 - 1; }
    const t = ctx.currentTime + at, src = ctx.createBufferSource(), bp = ctx.createBiquadFilter(), g = ctx.createGain();
    src.buffer = noise; bp.type = 'bandpass'; bp.frequency.value = freq; bp.Q.value = 1.4;
    g.gain.setValueAtTime(gain, t); g.gain.exponentialRampToValueAtTime(.001, t + .028);
    src.connect(bp); bp.connect(g); g.connect(master); src.start(t); src.stop(t + .05);
  }
  return {
    get on(){ return !!ctx && ctx.state === 'running'; },
    async enable(){ ensure(); try { await ctx.resume(); } catch (e) {} return this.on; },
    tryAuto(){ ensure(); ctx.resume().catch(() => {}); return this.on; },
    // d — задержка в секундах: звук попадает в момент, когда цифра встаёт на место
    count(d = 0){ tone(660, .16, {gain: .35, at: d}); },                                   // 5…1
    go(d = 0){ tone(990, .5, {type: 'triangle', gain: .4, at: d}); tone(1320, .5, {gain: .2, at: d}); }, // старт
    tick(d = 0){ tone(1150, .05, {type: 'square', gain: .09, at: d}); },                      // последние 5 секунд
    gong(d = 0){ [196, 294, 392, 523].forEach((f, i) => tone(f, 2.6 - i * .3, {gain: .26 - i * .04, attack: .01, at: d})); },
    plus(d = 0){ click(d); tone(1800, .018, {type: 'square', gain: .05, at: d}); },          // щелчок счётчика
    minus(d = 0){ tone(440, .18, {type: 'triangle', gain: .18, to: 300, at: d}); },
  };
})();
const soundBtn = $('soundBtn');
soundBtn.onclick = async () => { if (await Snd.enable()) { soundBtn.classList.add('off'); Snd.plus(); } };
setTimeout(() => { if (Snd.tryAuto()) soundBtn.classList.add('off'); }, 300);

/* Прокрутка цифры: новая цифра встаёт на место примерно на 40% длительности
   (кривая с лёгким «перелётом»). Звук ставим ровно на этот момент. */
const LAND = .4;
function landDelay(el){ const ms = parseFloat(getComputedStyle(el).getPropertyValue('--roll-ms')) || 560; return ms / 1000 * LAND; }
function sounds(ph, p){
  if (ph === 'countdown') {
    const n = Math.ceil(remaining(S));
    if (n !== lastCd) { lastCd = n; if (lastPh) Snd.count(landDelay($('cdNum'))); }
  } else lastCd = null;
  if (ph === 'play' && lastPh === 'countdown') { const t = document.querySelector('#timer .t'); Snd.go(t ? landDelay(t) : .17); }
  if (ph === 'play') {
    const sec = Math.ceil(remaining(S));
    if (sec <= 5 && sec > 0 && sec !== lastTick) { lastTick = sec; const t = document.querySelector('#timer .t'); Snd.tick(t ? landDelay(t) : 0); }
  } else lastTick = null;
  if (ph === 'timeup' && lastPh === 'play') Snd.gong();
  lastPh = ph;
}

/* ---------- Результаты справа ---------- */
const sideRows = new Map();
function renderSide(){
  const list = $('sideList');
  const rowH = list.querySelector('.srow') ? list.querySelector('.srow').offsetHeight : Math.max(38, Math.min(innerHeight * .066, 64));
  const step = rowH + 8, maxRows = Math.max(3, Math.floor((innerHeight * .68) / step));
  const ranked = S.participants.map((p, i) => ({...p, i})).filter(p => p.done || p.i === S.current)
    .sort((a, b) => b.score - a.score || (b.done - a.done) || a.i - b.i);
  let place = 0, prev = null;
  ranked.forEach((p, k) => { if (p.score !== prev) { place = k + 1; prev = p.score; } p.place = place; });
  let shown = ranked;
  if (ranked.length > maxRows) {
    shown = ranked.slice(0, maxRows - 1);
    const cur = ranked.find(p => p.i === S.current);
    shown.push(cur && !shown.includes(cur) ? cur : ranked[maxRows - 1]);
  }
  const visible = new Set(shown.map(p => p.i));
  shown.forEach((p, k) => {
    let el = sideRows.get(p.i);
    if (!el) {
      el = document.createElement('div'); el.className = 'srow gone';
      el.innerHTML = `<span class="pl num"></span><span class="pn"></span><span class="sc num"></span>`;
      el.style.transform = `translateY(${k * step}px)`;
      list.appendChild(el); sideRows.set(p.i, el); void el.offsetWidth;
    }
    el.classList.remove('gone');
    el.classList.toggle('now', p.i === S.current && !p.done);
    el.classList.toggle('lead', p.place === 1 && p.score > 0);
    el.style.transform = `translateY(${k * step}px)`; el.style.zIndex = p.i === S.current ? 2 : 1;
    el.querySelector('.pl').textContent = p.place;
    el.querySelector('.pn').textContent = p.name;
    setRoll(el.querySelector('.sc'), p.score);
  });
  sideRows.forEach((el, i) => { if (!visible.has(i)) el.classList.add('gone'); });
  list.style.height = (shown.length * step) + 'px';
}
/* длинное имя уменьшаем, чтобы оно влезло в колонку не больше чем в две строки */
function fitWho(){
  const el = $('who'); el.style.fontSize = '';
  let size = parseFloat(getComputedStyle(el).fontSize), lh = size * 1.05;
  for (let k = 0; k < 30 && (el.scrollWidth > el.clientWidth + 1 || el.offsetHeight > lh * 2.2); k++) {
    size *= .92; el.style.fontSize = size + 'px'; lh = size * 1.05;
  }
}
addEventListener('resize', () => { if (!$('game').hidden) fitWho(); });
function clearSide(){ sideRows.forEach(el => el.remove()); sideRows.clear(); }

function setView(v){
  if (v === lastView) return; lastView = v;
  ['idle','countdown','game','final'].forEach(id => $(id).hidden = id !== v);
}

function renderTimer(ph){
  const r = remaining(S), t = $('timer');
  if (ph === 'play') {
    if (t.dataset.k !== 'play') { t.innerHTML = `<div class="t num"></div><div class="bar"><i></i></div>`; t.dataset.k = 'play'; }
    t.classList.toggle('last', Math.ceil(r) <= 5);
    setRoll(t.querySelector('.t'), fmtTime(r), {up: false});
    t.querySelector('.bar i').style.transform = `scaleX(${Math.max(0, r / S.round)})`;
    return;
  }
  t.classList.remove('last');
  if (t.dataset.k === ph) return;
  t.dataset.k = ph;
  t.innerHTML = ph === 'timeup' ? `<div class="msg">Время!</div>` : `<div class="wait">${S.round} секунд на игру</div>`;
}

function renderFinal(){
  const list = S.participants.map((p, i) => ({...p, i})).sort((a, b) => b.score - a.score || a.i - b.i);
  const key = JSON.stringify(list.map(p => [p.i, p.score]));
  if (key === lastFinalKey) return; lastFinalKey = key;
  const n = list.length, cols = n <= 8 ? 1 : n <= 18 ? 2 : 3, rows = Math.ceil(n / cols);
  const b = $('board'); b.style.setProperty('--cols', cols); b.style.setProperty('--rows', rows);
  const top = n ? list[0].score : 0;
  let place = 0, prev = null;
  b.innerHTML = list.map((p, k) => {
    if (p.score !== prev) { place = k + 1; prev = p.score; }
    const win = place === 1 && top > 0;
    return `<div class="trow${win ? ' win' : ''}" style="animation-delay:${Math.min(k, 20) * .06}s">
      <span class="pl num">${place}</span><span class="pn">${esc(p.name)}</span><span class="sc num" data-v="${p.score}"></span></div>`;
  }).join('');
  b.querySelectorAll('.sc').forEach((el, k) => { setRoll(el, 0, {up: true}); setTimeout(() => setRoll(el, el.dataset.v, {up: true}), 250 + Math.min(k, 20) * 60); });
}


function frame(){
  if (S) {
    const ph = phaseOf(S), p = S.participants[S.current];
    $('round').textContent = p && !S.finished ? `${S.current + 1} из ${S.participants.length}` : '';
    sounds(ph, p);
    if (ph === 'idle') { setView('idle'); shownScore = shownPlayer = null; lastFinalKey = ''; clearSide(); }
    else if (ph === 'finished') { setView('final'); renderFinal(); shownScore = shownPlayer = null; clearSide(); }
    else if (ph === 'countdown') {
      setView('countdown');
      $('cdName').textContent = p.name + ', приготовьтесь';
      const n = String(Math.ceil(remaining(S)));
      if ($('cdNum')._text !== n) { setRoll($('cdNum'), n, {up: false}); Bg.pulse(); }
    } else {
      setView('game');
      lastFinalKey = '';
      if ($('who').textContent !== p.name) { $('who').textContent = p.name; fitWho(); }
      if (shownPlayer !== S.current || shownScore !== p.score) {
        const same = shownPlayer === S.current && shownScore !== null;
        setRoll($('count'), p.score);
        $('unit').textContent = cupsWord(p.score);
        if (same && p.score > shownScore) { Bg.pulse(); Snd.plus(landDelay($('count'))); }
        if (same && p.score < shownScore) Snd.minus(landDelay($('count')));
        shownScore = p.score; shownPlayer = S.current;
      }
      renderTimer(ph);
      renderSide();
    }
  }
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
</script></body></html>"""

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), allow_unsafe_werkzeug=True)
