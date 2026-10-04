"""Voice meter — конкурс «Мужское / Женское».

Участники по очереди кричат в микрофон. Побеждает тот, чей самый громкий момент
за раунд выше. Шкала условная: 0–100 dB, к реальным децибелам не привязана.

  /         — пульт ведущего (телефон)
  /screen   — экран для гостей (проектор), он же снимает звук с микрофона
  /setup    — выбор аудиовхода и чувствительности (открывать на компьютере с микрофоном)

Микрофон слушает браузер экрана для гостей и присылает уровень на сервер через
Socket.IO. Сервер сам считает раунды и максимум, поэтому пульт и экраны всегда
показывают одно и то же. Все ссылки относительные, поэтому этот же файл работает
и отдельно, и внутри общего сборника (/men/voice/...).
"""
import math
import os
import time
from threading import Lock, Timer

from flask import Flask, redirect, render_template_string, request
from flask_socketio import SocketIO, emit

PREP_SECONDS = int(os.environ.get("PREP_SECONDS", 5))
ROUND_SECONDS = int(os.environ.get("ROUND_SECONDS", 10))
MAX_PARTICIPANTS = 30
LIVE_INTERVAL = 0.04      # не чаще 25 раз в секунду рассылаем живой уровень
ROUND_MIN, ROUND_MAX = 3, 60

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
lock = Lock()

state = {
    "participants": [],   # [{"name": str, "score": float, "done": bool}]
    "current": -1,
    "finished": False,
    "started_at": None,   # момент нажатия «Запустить время»; дальше отсчёт и раунд
    "round": ROUND_SECONDS,
    "bump": 0,
}
# Какой экран сейчас слушает микрофон. Принимаем уровень только от него.
mic = {"sid": None, "label": "", "emit_at": 0.0}


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
    if elapsed < PREP_SECONDS + state["round"]:
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
        "round": state["round"],
        "mic": {"on": mic["sid"] is not None, "label": mic["label"]},
        "server_now": time.time(),
    }


def broadcast_locked():
    socketio.emit("state", snapshot_locked())


def current_player_locked():
    i = state["current"]
    if 0 <= i < len(state["participants"]):
        return state["participants"][i]
    return None


def schedule_round_end_locked():
    """Когда раунд закончится, всем уходит итоговое состояние с финальным максимумом."""
    token = state["started_at"]
    delay = PREP_SECONDS + state["round"] - (time.time() - token) + 0.3
    timer = Timer(max(0.1, delay), round_end, args=(token,))
    timer.daemon = True
    timer.start()


def round_end(token):
    with lock:
        if state["started_at"] == token:
            broadcast_locked()


# ---------- события ----------

@socketio.on("connect")
def on_connect(data=None):
    with lock:
        emit("state", snapshot_locked())


@socketio.on("disconnect")
def on_disconnect(*args):
    with lock:
        if mic["sid"] == request.sid:
            mic.update(sid=None, label="")
            broadcast_locked()


@socketio.on("sync")
def on_sync(data=None):
    with lock:
        emit("state", snapshot_locked())


@socketio.on("mic_claim")
def on_mic_claim(data=None):
    label = " ".join(str((data or {}).get("label", "")).split())[:80]
    with lock:
        mic.update(sid=request.sid, label=label)
        broadcast_locked()


@socketio.on("mic_release")
def on_mic_release(data=None):
    with lock:
        if mic["sid"] == request.sid:
            mic.update(sid=None, label="")
            broadcast_locked()


@socketio.on("mic_level")
def on_mic_level(data=None):
    """Уровень 0–100 от экрана с микрофоном. В раунде запоминаем максимум."""
    try:
        level = float((data or {}).get("level", 0))
    except (TypeError, ValueError):
        return
    if not math.isfinite(level):
        return
    level = round(max(0.0, min(100.0, level)), 1)
    with lock:
        if mic["sid"] != request.sid:
            return
        now = time.time()
        player = current_player_locked()
        if player and phase_locked(now) == "play" and level > player["score"]:
            player["score"] = level
        if now - mic["emit_at"] >= LIVE_INTERVAL:
            mic["emit_at"] = now
            socketio.emit("live", {"level": level, "score": player["score"] if player else 0.0})


@socketio.on("setup")
def on_setup(data=None):
    data = data or {}
    try:
        count = int(data.get("count", 4))
    except (TypeError, ValueError):
        count = 4
    count = max(1, min(MAX_PARTICIPANTS, count))
    with lock:
        try:
            rnd = int(data.get("round", state["round"]))
        except (TypeError, ValueError):
            rnd = state["round"]
        state["round"] = max(ROUND_MIN, min(ROUND_MAX, rnd))
        state.update(
            participants=[{"name": f"Участник {i + 1}", "score": 0.0, "done": False} for i in range(count)],
            current=0, finished=False, started_at=None,
        )
        state["bump"] += 1
        broadcast_locked()


@socketio.on("start_timer")
def on_start_timer(data=None):
    with lock:
        if phase_locked() == "ready":
            state["started_at"] = time.time()
            schedule_round_end_locked()
            broadcast_locked()


@socketio.on("replay")
def on_replay(data=None):
    """Переиграть раунд текущего участника."""
    with lock:
        player = current_player_locked()
        if player and not state["finished"]:
            player.update(score=0.0, done=False)
            state["started_at"] = None
            state["bump"] += 1
            broadcast_locked()


@socketio.on("next")
def on_next(data=None):
    with lock:
        player = current_player_locked()
        if not player or state["finished"] or phase_locked() != "timeup":
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
    # "/" отдельно или "/men/voice/" внутри сборника
    return (request.script_root or "") + "/"


@app.after_request
def no_cache(resp):
    if request.path in ("/", "/screen", "/setup"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def page(template):
    return render_template_string(
        template, base=base_path(), theme_css=THEME_CSS, client_js=CLIENT_JS, audio_js=AUDIO_JS, bg_js=BG_JS)


@app.get("/")
def control():
    return page(CONTROL_HTML)


@app.get("/control")
def control_old():
    return redirect(base_path(), code=302)


@app.get("/screen")
def screen():
    return page(SCREEN_HTML)


@app.get("/setup")
def setup():
    return page(SETUP_HTML)


# ======================================================================
# Общий стиль конкурсов «Мужское / Женское».
# Тема задаётся атрибутом data-theme="men" | "women" на <html>.
# ======================================================================
THEME_CSS = r"""
:root,[data-theme=men]{
  --ink:#06140e; --ink-2:#0a1f16; --surface:#0d261b; --line:#1d4734;
  --signal:#2bf08a; --signal-ink:#02140a; --signal-soft:rgba(43,240,138,.14);
  --chalk:#f1f5ee; --mist:#8da698; --danger:#ff8e9c; --danger-bg:#2d1419;
  --warm:#ffd23f; --hot:#ff5d6c;
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
  /* Шкала громкости: зелёный → жёлтый → красный */
  --scale:linear-gradient(to top,var(--signal) 0%,var(--signal) 50%,var(--warm) 72%,#ff9f43 86%,var(--hot) 100%);
  --scale-x:linear-gradient(to right,var(--signal) 0%,var(--signal) 50%,var(--warm) 72%,#ff9f43 86%,var(--hot) 100%);
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--ink);color:var(--chalk);font-family:var(--ui);-webkit-font-smoothing:antialiased}
body{min-height:100vh;min-height:100dvh}
button{font:inherit;color:inherit;cursor:pointer;-webkit-tap-highlight-color:transparent}
button:focus-visible,a:focus-visible,select:focus-visible,input:focus-visible{outline:3px solid var(--signal);outline-offset:3px}
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
[hidden]{display:none!important}
@media (prefers-reduced-motion:reduce){*,*:before,*:after{animation-duration:.01ms!important;transition-duration:.01ms!important}}
"""

# Общая логика клиента: подключение, синхронизация часов, вычисление фазы, табло.
CLIENT_JS = r"""
const BASE = document.documentElement.dataset.base || '/';
const socket = io({path: BASE + 'socket.io', transports: ['websocket', 'polling']});
let S = null, clockOffset = 0, LIVE = {level: 0, score: 0, at: -1e9};
const offlineBar = document.getElementById('offline');
socket.on('connect', () => offlineBar && offlineBar.classList.remove('on'));
socket.on('disconnect', () => offlineBar && offlineBar.classList.add('on'));
socket.on('state', s => { clockOffset = s.server_now - Date.now() / 1000; S = s; window.onState && window.onState(s); });
// Живой уровень с микрофона. В раунде подтягиваем максимум текущего участника, не дожидаясь полного состояния.
socket.on('live', m => {
  LIVE = {level: m.level, score: m.score, at: performance.now()};
  if (S && phaseOf(S) === 'play') {
    const p = S.participants[S.current];
    if (p && m.score > p.score) p.score = m.score;
  }
  window.onLive && window.onLive(LIVE);
});
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
function fmt1(v){ return (Math.round((+v || 0) * 10) / 10).toFixed(1); }
/* ---------- Табло ----------
   setRoll(el, '87.4') — каждая цифра крутится к новому значению.
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
function esc(v){ return String(v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function ranking(s){ return s.participants.map((p, i) => ({...p, i})).filter(p => p.done).sort((a, b) => b.score - a.score || a.i - b.i); }
"""

# ======================================================================
# Микрофон: общий код для экрана и Setup.
# Настройки хранятся в localStorage браузера: экран и Setup должны быть
# открыты в одном браузере на одном компьютере.
# ======================================================================
AUDIO_JS = r"""
const VM_KEY = 'vm.settings.v1';
const VM_DEFAULT = {deviceId: '', label: '', trim: 0, agc: false, ns: false, ec: false};
function vmLoad(){ try { return {...VM_DEFAULT, ...JSON.parse(localStorage.getItem(VM_KEY) || '{}')}; } catch (e) { return {...VM_DEFAULT}; } }
function vmSave(patch){ const s = {...vmLoad(), ...patch}; try { localStorage.setItem(VM_KEY, JSON.stringify(s)); } catch (e) {} return s; }
/* Шкала 0–100 dB условная: от «почти тишина» (-75 dBFS) до «в потолок» (-5 dBFS).
   Чувствительность (trim) сдвигает всю шкалу вверх или вниз. */
const DB_LO = -75, DB_HI = -5;
function toLevel(dbfs, trim){ return Math.max(0, Math.min(100, (dbfs - DB_LO + trim) / (DB_HI - DB_LO) * 100)); }

const Mic = (() => {
  const TICK = 25, WIN = 8;          // замер каждые 25 мс, усреднение по 8 замерам = 200 мс
  let ctx = null, stream = null, an = null, buf = null, timer = null, retry = null, gen = 0, wanted = false, hist = [], cfg = vmLoad();
  const api = {running: false, error: '', note: '', label: '', deviceId: '', settings: null, sampleRate: 0,
               level: 0, dbfs: -120, suspended: false, onLevel: null, onState: null};
  const fire = () => { api.suspended = !!ctx && ctx.state !== 'running'; api.onState && api.onState(api); };

  function explain(e){
    const n = e && e.name;
    if (n === 'NotAllowedError' || n === 'PermissionDeniedError') return 'Доступ к микрофону запрещён. Разрешите его в настройках сайта (значок замка в адресной строке) и обновите страницу.';
    if (n === 'NotFoundError' || n === 'DevicesNotFoundError') return 'Микрофон не найден. Проверьте подключение звуковой карты.';
    if (n === 'NotReadableError' || n === 'TrackStartError') return 'Микрофон занят другой программой или недоступен.';
    if (n === 'SecurityError') return 'Микрофон работает только по HTTPS или на localhost.';
    return 'Не удалось открыть микрофон: ' + ((e && e.message) || n || 'неизвестная ошибка');
  }
  function teardown(){
    clearInterval(timer); timer = null;
    if (stream) { stream.getTracks().forEach(t => { t.onended = null; t.stop(); }); stream = null; }
    if (ctx) { ctx.onstatechange = null; ctx.close().catch(() => {}); ctx = null; }
    an = null; hist = []; api.running = false; api.level = 0; api.dbfs = -120; api.sampleRate = 0;
  }
  async function getStream(c){
    const audio = {echoCancellation: c.ec, noiseSuppression: c.ns, autoGainControl: c.agc};
    if (c.deviceId) {
      try { return await navigator.mediaDevices.getUserMedia({audio: {...audio, deviceId: {exact: c.deviceId}}}); }
      catch (e) { if (e.name !== 'OverconstrainedError' && e.name !== 'NotFoundError') throw e; }
      // Устройство с таким id не нашли (браузер мог поменять id) — ищем по названию
      const probe = await navigator.mediaDevices.getUserMedia({audio});
      try {
        const list = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === 'audioinput');
        const hit = c.label && list.find(d => d.label === c.label);
        if (hit) {
          const s = await navigator.mediaDevices.getUserMedia({audio: {...audio, deviceId: {exact: hit.deviceId}}});
          probe.getTracks().forEach(t => t.stop());
          return s;
        }
      } catch (e) {}
      api.note = 'Сохранённый вход не найден, используется вход по умолчанию. Выберите нужный в Setup.';
      return probe;
    }
    return navigator.mediaDevices.getUserMedia({audio});
  }
  function scheduleRetry(my, e){
    if (e && (e.name === 'NotAllowedError' || e.name === 'SecurityError')) return;   // повторять бесполезно
    clearTimeout(retry);
    retry = setTimeout(() => { if (wanted && my === gen) api.start(); }, 3000);
  }
  function measure(){
    if (!an) return;
    an.getFloatTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
    hist.push(sum / buf.length); if (hist.length > WIN) hist.shift();
    // Среднее по 200 мс: одиночный щелчок или хлопок по микрофону не даёт высокий результат,
    // а громкий крик держится дольше и проходит почти без потерь.
    let m = 0; for (const x of hist) m += x; m /= WIN;
    api.dbfs = m > 1e-12 ? 10 * Math.log10(m) : -120;
    api.level = toLevel(api.dbfs, cfg.trim);
    api.onLevel && api.onLevel(api.level, api.dbfs);
  }

  api.start = async () => {
    const my = ++gen; wanted = true; clearTimeout(retry);
    cfg = vmLoad(); api.note = ''; api.error = '';
    teardown();
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      api.error = 'Браузер не даёт доступ к микрофону. Нужен HTTPS или адрес localhost.'; fire(); return false;
    }
    let s;
    try { s = await getStream(cfg); }
    catch (e) { if (my !== gen) return false; api.error = explain(e); fire(); scheduleRetry(my, e); return false; }
    if (my !== gen) { s.getTracks().forEach(t => t.stop()); return false; }
    stream = s;
    const track = s.getAudioTracks()[0];
    api.label = track.label || ''; api.settings = track.getSettings ? track.getSettings() : {}; api.deviceId = api.settings.deviceId || '';
    track.onended = () => { if (my !== gen) return; teardown(); api.error = 'Микрофон отключился. Пробую подключить снова…'; fire(); scheduleRetry(my); };
    ctx = new (window.AudioContext || window.webkitAudioContext)({latencyHint: 'interactive'});
    api.sampleRate = ctx.sampleRate; ctx.onstatechange = fire;
    try { await ctx.resume(); } catch (e) {}
    an = ctx.createAnalyser(); an.fftSize = 2048; an.smoothingTimeConstant = 0;
    ctx.createMediaStreamSource(s).connect(an);
    buf = new Float32Array(an.fftSize); hist = [];
    timer = setInterval(measure, TICK);     // таймер, а не кадры: замер не зависит от того, видна ли вкладка
    api.running = true; fire(); return true;
  };
  api.stop = () => { wanted = false; gen++; clearTimeout(retry); teardown(); api.error = ''; fire(); };
  api.resume = async () => {
    if (ctx && ctx.state !== 'running') { try { await ctx.resume(); } catch (e) {} }
    fire(); return !!ctx && ctx.state === 'running';
  };
  api.reload = () => { cfg = vmLoad(); };
  api.setTrim = v => { cfg.trim = v; };
  return api;
})();
"""

# ======================================================================
# Фон: маленькие значки громкости медленно всплывают (как лица в «Хомяке», только мельче)
# ======================================================================
BG_JS = r"""
const Bg = (() => {
  const html = document.documentElement;
  const cv = document.getElementById('bg');
  if (!cv) return {pulse(){}, level(){}, recolor(){}};
  const ctx = cv.getContext('2d');
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  let W = 0, H = 0, dpr = 1, k = 1, flash = 0, flashTarget = 0, energy = 0, energyTarget = 0;
  const t0 = performance.now();
  let rgb = [43, 240, 138];
  function readColor(){
    const c = getComputedStyle(html).getPropertyValue('--signal').trim();
    const m = c.match(/^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i);
    if (m) rgb = m.slice(1).map(h => parseInt(h, 16));
  }
  const rgba = a => `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${Math.max(0, Math.min(1, a))})`;
  function size(){
    dpr = Math.min(2, devicePixelRatio || 1); W = innerWidth; H = innerHeight;
    k = Math.max(.8, Math.min(1.8, Math.min(W, H) / 800));
    cv.width = W * dpr; cv.height = H * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  addEventListener('resize', size); size(); readColor();

  // Значок динамика в единичных координатах (≈ 1,4 × 1,1), центр в (0, 0); waves — число дуг звука.
  function speaker(x, y, s, rot, a, waves){
    ctx.save(); ctx.translate(x, y); ctx.rotate(rot); ctx.scale(s, s);
    ctx.lineCap = ctx.lineJoin = 'round'; ctx.lineWidth = Math.max(.07, 1.3 / s);
    ctx.strokeStyle = rgba(a); ctx.fillStyle = rgba(a * .28);
    ctx.beginPath();
    ctx.moveTo(-.62, -.22); ctx.lineTo(-.34, -.22); ctx.lineTo(.08, -.56);
    ctx.lineTo(.08, .56); ctx.lineTo(-.34, .22); ctx.lineTo(-.62, .22); ctx.closePath();
    ctx.fill(); ctx.stroke();
    for (let i = 1; i <= waves; i++) { ctx.beginPath(); ctx.arc(.1, 0, .2 + i * .2, -.85, .85); ctx.stroke(); }
    ctx.restore();
  }

  const icons = Array.from({length: 12}, () => ({
    x: Math.random(), y: Math.random(), r: 11 + Math.random() * 20,
    v: .012 + Math.random() * .024, a: .09 + Math.random() * .1,
    w: Math.random() * 6, rot: (Math.random() - .5) * .7, rs: .25 + Math.random() * .4,
    waves: 1 + Math.floor(Math.random() * 3),
  }));

  function draw(t){
    icons.forEach(f => {
      const y = ((f.y - t * f.v) % 1 + 1) % 1;
      const fade = Math.min(1, y * 10, (1 - y) * 10);               // у краёв плавно появляются и исчезают
      const px = (f.x + Math.sin(t * .3 + f.w) * .02) * W, py = y * H * 1.15 - H * .07;
      const r = f.r * k * (1 + flash * .14 + energy * .12), rot = f.rot + Math.sin(t * f.rs + f.w) * .18;
      const a = (f.a + flash * .14 + energy * .16) * fade;
      // мягкое свечение позади — как огоньки в «Шариках»
      const g = ctx.createRadialGradient(px, py, 0, px, py, r * 1.7);
      g.addColorStop(0, rgba((f.a * .35 + flash * .07 + energy * .1) * fade)); g.addColorStop(1, rgba(0));
      ctx.globalCompositeOperation = 'lighter'; ctx.fillStyle = g;
      ctx.beginPath(); ctx.arc(px, py, r * 1.7, 0, Math.PI * 2); ctx.fill();
      ctx.globalCompositeOperation = 'source-over';
      speaker(px, py, r, rot, a, f.waves);
    });
  }
  function loop(now){
    const t = reduce ? 0 : (now - t0) / 1000;
    ctx.clearRect(0, 0, W, H);
    draw(t);
    flash += (flashTarget - flash) * .07; flashTarget *= .975;    // ~0,5 с разгорается, ~2 с гаснет
    energy += (energyTarget - energy) * .12;                       // значки светятся ярче, когда в зале шумно
    requestAnimationFrame(loop);
  }
  requestAnimationFrame(loop);
  return {pulse(){ flashTarget = 1; }, level(v){ energyTarget = Math.max(0, Math.min(1, v / 100)); }, recolor: readColor};
})();
"""

FONTS_BASE = """<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Onest:wght@400;600;800&family=Unbounded:wght@600;800;900&family=Nunito:wght@800;900&display=swap" rel="stylesheet">"""
FONTS = FONTS_BASE + """
<script src="https://cdn.socket.io/4.8.1/socket.io.min.js"></script>"""

# ======================================================================
# Пульт ведущего
# ======================================================================
CONTROL_HTML = r"""<!doctype html>
<html lang="ru" data-theme="men" data-base="{{ base }}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#06140e"><title>Voice meter · пульт</title>
""" + FONTS + r"""
<style>{{ theme_css|safe }}
body{background:radial-gradient(120% 60% at 0 0,var(--ink-2),var(--ink) 60%)}
.app{max-width:520px;margin:0 auto;padding:16px 16px calc(24px + env(safe-area-inset-bottom));display:flex;flex-direction:column;gap:14px;min-height:100dvh}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.top .wordmark{font-size:20px}
.links{display:flex;gap:16px}
.link{color:var(--mist);font-weight:600;font-size:14px;text-decoration:underline;text-underline-offset:3px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-l);padding:20px}
h2{margin:0 0 14px;font-size:17px;font-weight:600;color:var(--mist)}
.stepper{display:grid;grid-template-columns:72px 1fr 72px;align-items:center;gap:10px;margin-bottom:16px}
.stepper button{height:72px;border-radius:var(--r-m);border:1px solid var(--line);background:var(--ink-2);font-size:30px;font-weight:600}
.stepper .num{text-align:center;font-size:52px;}
.chips{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:18px}
.chip{border:1px solid var(--line);background:var(--ink-2);border-radius:var(--r-m);padding:12px 0;font-weight:800;font-size:17px}
.chip.on{background:var(--signal);border-color:var(--signal);color:var(--signal-ink)}
.lbl{color:var(--mist);font-weight:600;font-size:14px;margin:0 0 8px}
.btn{display:block;width:100%;border:0;border-radius:var(--r-m);padding:18px;font-size:19px;font-weight:800}
.btn.primary{background:var(--signal);color:var(--signal-ink)}
.btn.quiet{background:transparent;border:1px solid var(--line);color:var(--chalk);font-weight:600}
.btn.danger{background:transparent;border:1px solid var(--danger-bg);color:var(--danger);font-weight:600;font-size:15px;padding:14px}
.btn:disabled{opacity:.4;cursor:default}
.btn:active:not(:disabled){transform:scale(.98)}
/* состояние микрофона */
.mic{padding:14px 16px}
.mic .head{display:flex;align-items:center;gap:12px}
.mic .dot{flex:none;width:12px;height:12px;border-radius:50%;background:var(--danger)}
.mic.ok .dot{background:var(--signal);box-shadow:0 0 12px var(--signal)}
.mic.warn .dot{background:var(--warm);box-shadow:0 0 12px var(--warm)}
.mic .txt{flex:1;min-width:0}
.mic .txt b{display:block;font-weight:800}
.mic .txt span{display:block;color:var(--mist);font-size:13px;line-height:1.35;overflow:hidden;text-overflow:ellipsis}
.mic .lv{font-size:34px;line-height:1}
.mic .lv small{font-family:var(--ui);font-size:13px;color:var(--mist);margin-left:3px;font-weight:600}
.mic .bar{height:10px;border-radius:5px;background:var(--ink-2);margin-top:12px;overflow:hidden;position:relative}
.mic .bar i{position:absolute;left:0;top:0;bottom:0;width:100%;background:var(--scale-x);clip-path:inset(0 100% 0 0)}
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
.stack{display:flex;flex-direction:column;gap:10px}
.rows{display:flex;flex-direction:column}
.row{display:flex;justify-content:space-between;padding:12px 2px;border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:0}
.row b{font-family:var(--digits);font-weight:var(--digits-w);color:var(--signal)}
.row b small{font-family:var(--ui);color:var(--mist);font-weight:600;margin-left:4px}
.row.first b{font-size:20px}
.hint{color:var(--mist);font-size:14px;text-align:center}
.spacer{flex:1}
.namelink{align-self:center;background:none;border:0;color:var(--mist);font-size:14px;font-weight:600;text-decoration:underline;text-underline-offset:3px;padding:8px}
.fields{display:flex;flex-direction:column;gap:8px}
.fields label{display:grid;grid-template-columns:2em 1fr;align-items:center;gap:8px;color:var(--mist);font-weight:600}
.fields input{width:100%;min-width:0;background:var(--ink-2);border:1px solid var(--line);border-radius:var(--r-s);color:var(--chalk);font:inherit;font-weight:600;padding:12px 14px}
.fields input:focus{outline:2px solid var(--signal);outline-offset:1px}
</style></head>
<body>
<div class="offline" id="offline">Нет связи с сервером — переподключаюсь…</div>
<main class="app">
  <div class="top"><span class="wordmark"><i></i>Voice meter</span>
    <span class="links"><a class="link" href="{{ base }}screen" target="_blank" rel="noopener">Экран для гостей</a><a class="link" href="{{ base }}setup" target="_blank" rel="noopener">Setup</a></span></div>

  <section class="card mic" id="mic">
    <div class="head"><span class="dot"></span><div class="txt"><b id="micTitle">Микрофон не подключён</b><span id="micSub"></span></div><div class="lv num"><span id="lvNum">0</span><small>dB</small></div></div>
    <div class="bar"><i id="lvBar"></i></div>
  </section>

  <section class="card" id="setup">
    <h2>Сколько участников</h2>
    <div class="stepper"><button id="minusCount" aria-label="Меньше">−</button><div class="num" id="count">4</div><button id="plusCount" aria-label="Больше">+</button></div>
    <h2>Сколько секунд кричать</h2>
    <div class="chips" id="chips"></div>
    <button class="btn primary" id="begin">Начать конкурс</button>
  </section>

  <section class="card" id="game" hidden>
    <div class="who"><span class="name" id="name"></span><span class="of" id="of"></span></div>
    <div class="status" id="status"><span class="label" id="statusLabel"></span><span class="time num" id="time"></span></div>
    <div class="score"><div class="num" id="score">0.0</div><div class="unit" id="unit">максимум, dB</div></div>
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
let count = 4, roundSec = 10, lastKey = '', lvDisp = 0;
const ROUNDS = [5, 10, 15, 20];
$('chips').innerHTML = ROUNDS.map(n => `<button class="chip${n === roundSec ? ' on' : ''}" data-n="${n}">${n} с</button>`).join('');
$('chips').addEventListener('click', e => {
  const b = e.target.closest('.chip'); if (!b) return;
  roundSec = +b.dataset.n;
  document.querySelectorAll('.chip').forEach(c => c.classList.toggle('on', c === b));
});
$('minusCount').onclick = () => { count = Math.max(1, count - 1); $('count').textContent = count; };
$('plusCount').onclick = () => { count = Math.min(30, count + 1); $('count').textContent = count; };
$('begin').onclick = () => socket.emit('setup', {count, round: roundSec});
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
function micFresh(){ return !!(S && S.mic.on) && performance.now() - LIVE.at < 1500; }

function renderActions(ph, isLast){
  // Перерисовываем кнопки только при смене фазы, чтобы нажатие не «съедалось»
  const key = ph + (isLast ? 'L' : '');
  if (key === lastKey) return;
  lastKey = key;
  const a = $('actions');
  if (ph === 'ready') {
    a.innerHTML = `<div class="stack"><button class="btn primary" data-act="start_timer">Запустить время</button>
      <p class="hint">${S.prep} секунд отсчёта, потом ${S.round} секунд на крик</p></div>`;
  } else if (ph === 'countdown') {
    a.innerHTML = `<p class="hint">Участник готовится…</p>`;
  } else if (ph === 'play') {
    a.innerHTML = `<p class="hint">Идёт замер. Засчитывается самый громкий момент.</p>`;
  } else if (ph === 'timeup') {
    a.innerHTML = `<div class="stack"><button class="btn primary" data-act="next">${isLast ? 'Показать итоги' : 'Следующий участник'}</button>
      <button class="btn quiet" data-act="replay">Переиграть раунд</button></div>`;
  } else a.innerHTML = '';
}
$('actions').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b || b.disabled) return;
  if (b.dataset.act === 'start_timer' && !micFresh() && !confirm('Нет сигнала с микрофона. Всё равно запустить время?')) return;
  if (b.dataset.act === 'replay' && !confirm('Обнулить результат и переиграть раунд?')) return;
  if (b.dataset.act) act(b.dataset.act);
});

function renderMic(){
  const box = $('mic');
  let cls = '', title, sub;
  if (!S || !S.mic.on) { title = 'Микрофон не подключён'; sub = 'Откройте «Экран для гостей» на компьютере с микрофоном'; }
  else if (!micFresh()) { cls = 'warn'; title = 'Нет сигнала с микрофона'; sub = 'Экран подключён, но звук не приходит. Проверьте вход в Setup'; }
  else { cls = 'ok'; title = 'Микрофон работает'; sub = S.mic.label || 'вход по умолчанию'; }
  box.className = 'card mic ' + cls;
  $('micTitle').textContent = title; $('micSub').textContent = sub;
  lvDisp = micFresh() ? Math.max(LIVE.level, lvDisp - 2.5) : Math.max(0, lvDisp - 4);
  $('lvNum').textContent = Math.round(lvDisp);
  $('lvBar').style.clipPath = `inset(0 ${100 - lvDisp}% 0 0)`;
}

function tick(){
  renderMic();
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
      setRoll($('score'), fmt1(p.score));
      const st = $('status'), r = remaining(S);
      st.className = 'status' + (ph === 'play' ? ' hot' : ph === 'timeup' ? ' end' : '');
      $('statusLabel').textContent = {ready:'Ждём старта', countdown:'Отсчёт', play:'Идёт замер', timeup:'Время вышло'}[ph];
      setRoll($('time'), ph === 'countdown' ? String(Math.ceil(r)) : ph === 'timeup' ? '0:00' : fmtTime(r), {up: false});
      $('unit').textContent = ph === 'timeup' ? 'результат, dB' : 'максимум, dB';
      renderActions(ph, isLast);
    } else renderActions(ph, false);
    const done = ranking(S);
    $('results').hidden = !done.length;
    $('resultsTitle').textContent = ph === 'finished' ? 'Итоги' : 'Уже сыграли';
    $('reset').textContent = ph === 'finished' ? 'Начать новый конкурс' : 'Сбросить конкурс';
    $('rows').innerHTML = done.map((p, i) => `<div class="row${i === 0 ? ' first' : ''}"><span>${esc(p.name)}</span><b>${fmt1(p.score)}<small>dB</small></b></div>`).join('');
  }
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);
</script></body></html>"""

# ======================================================================
# Экран для гостей
# ======================================================================
SCREEN_HTML = r"""<!doctype html>
<html lang="ru" data-theme="men" data-base="{{ base }}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Voice meter</title>
""" + FONTS + r"""
<style>{{ theme_css|safe }}
html,body{height:100%;overflow:hidden}
body{background:radial-gradient(60% 70% at 50% 55%,var(--signal-soft),transparent 70%),radial-gradient(90% 80% at 0 0,var(--ink-2),var(--ink) 65%)}
.screen{height:100vh;display:grid;grid-template-rows:auto 1fr;padding:3.2vh 4.5vw 4vh}
.head{display:flex;justify-content:space-between;align-items:center}
.head .wordmark{font-size:clamp(20px,2vw,34px)}
.head .round{color:var(--mist);font-weight:600;font-size:clamp(16px,1.4vw,24px)}
.stage{position:relative;display:flex;min-height:0;--side:clamp(220px,21vw,380px);--mw:clamp(120px,11vw,200px)}
.view{flex:1;min-width:0;min-height:0}
/* --- шкала громкости слева: всегда на экране, пока идёт игра --- */
.meter{position:absolute;left:0;top:0;bottom:0;width:var(--mw);display:flex;flex-direction:column;align-items:center;gap:1.4vh;padding:2.2vh 0 1vh}
.stage.nometer .meter{display:none}
.mread{display:flex;align-items:baseline;gap:.3em;line-height:1}
.mread .num{font-size:clamp(34px,3.6vw,64px);color:var(--chalk)}
.mread small{font-weight:600;color:var(--mist);font-size:clamp(14px,1.2vw,22px)}
.mbox{flex:1;min-height:0;display:flex;gap:.7em;padding:1.2em 0}
.ticks{position:relative;width:2.6em;font-size:clamp(12px,1.05vw,19px);color:var(--mist)}
.ticks span{position:absolute;right:0;transform:translateY(50%);line-height:1;font-family:var(--digits);font-weight:800;font-variant-numeric:tabular-nums}
.ticks span.zero{color:var(--chalk)}
.tubewrap{position:relative;width:clamp(46px,4.4vw,84px)}
.tube{position:absolute;inset:0;border:1px solid var(--line);border-radius:var(--r-m);background:rgba(6,20,14,.55);backdrop-filter:blur(2px)}
.seg{position:absolute;inset:7px;
  -webkit-mask-image:repeating-linear-gradient(to top,#000 0,#000 calc(2% - 3px),transparent calc(2% - 3px),transparent 2%);
  mask-image:repeating-linear-gradient(to top,#000 0,#000 calc(2% - 3px),transparent calc(2% - 3px),transparent 2%)}
.seg .dim,.seg .lit{position:absolute;inset:0;background:var(--scale)}
.seg .dim{opacity:.13}
.seg .lit{clip-path:inset(100% 0 0 0);filter:drop-shadow(0 0 10px var(--signal-soft))}
.pk{position:absolute;left:-7px;right:-7px;height:4px;margin-bottom:-2px;bottom:0;border-radius:2px;background:var(--chalk);box-shadow:0 0 14px rgba(241,245,238,.7);transition:bottom .12s linear}
/* --- игра: имя, огромный уровень, таймер --- */
.play{position:relative;display:flex;min-height:0}
.play>.game{flex:1;min-width:0;padding:0 calc(var(--side) + 2vw)}
.pad{padding:0 calc(var(--side) + 2vw)}
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
.srow .sc{color:var(--chalk);--roll-ms:260ms}
.srow.lead .sc{color:var(--signal)}
.srow.now{border-color:var(--signal);background:rgba(43,240,138,.1)}
.srow.now .pn:before{content:"";flex:none;width:.5em;height:.5em;border-radius:50%;background:var(--signal);box-shadow:0 0 10px var(--signal);animation:live 1.4s ease-in-out infinite}
@keyframes live{50%{opacity:.35}}
.srow.gone{opacity:0}
.side-more{position:absolute;left:0;right:0;text-align:center;color:var(--mist);font-weight:600;line-height:1}
/* кнопка «включить звук и микрофон» и состояние микрофона */
.sound{position:fixed;right:20px;bottom:20px;z-index:50;border:1px solid var(--line);background:rgba(13,38,27,.92);color:var(--chalk);border-radius:999px;padding:12px 20px;font-weight:600;font-size:16px;cursor:pointer;transition:opacity .4s}
.sound:hover{border-color:var(--signal)}
.sound.off{opacity:0;pointer-events:none}
.micchip{position:fixed;left:20px;bottom:16px;z-index:50;max-width:min(60vw,560px);display:flex;align-items:center;gap:.6em;border:0;background:none;color:var(--mist);font:600 clamp(12px,1vw,16px)/1.2 var(--ui);padding:6px 4px;opacity:.55;transition:opacity .3s;text-decoration:none}
.micchip:hover{opacity:1}
.micchip i{flex:none;width:.7em;height:.7em;border-radius:50%;background:var(--mist)}
.micchip span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.micchip.ok i{background:var(--signal);box-shadow:0 0 10px var(--signal)}
.micchip.bad{opacity:1;color:var(--danger)}
.micchip.bad i{background:var(--danger)}
.who{font-family:var(--display);font-weight:800;font-size:clamp(32px,4.2vw,80px);line-height:1}
.count{font-size:min(22vw,36vh);line-height:1;color:var(--signal);margin:1.5vh 0 0;filter:drop-shadow(0 0 40px var(--signal-soft));--roll-ms:1300ms}
.unit{color:var(--mist);font-weight:600;font-size:clamp(20px,2vw,38px);margin-top:1vh}
.peakinfo{margin-top:1.2vh;color:var(--mist);font-weight:600;font-size:clamp(16px,1.6vw,30px);display:flex;align-items:center;gap:.5em;--roll-ms:260ms}
.peakinfo .num{color:var(--chalk);font-size:1.25em}
.timer{margin-top:3vh;width:min(640px,40vw);min-height:clamp(60px,8vh,110px);display:flex;flex-direction:column;align-items:center;justify-content:center}
.timer .t{font-size:clamp(44px,4.6vw,88px);line-height:1;font-weight:800;--roll-ms:420ms}
.timer .bar{width:100%;height:10px;border-radius:5px;background:var(--line);margin-top:1.6vh;overflow:hidden}
.timer .bar i{display:block;height:100%;width:100%;background:var(--signal);border-radius:5px;transform-origin:0 50%}
.timer.last .t{color:var(--chalk);animation:beat 1s ease-in-out infinite}
.timer.last .bar i{background:var(--chalk)}
@keyframes beat{0%,100%{transform:scale(1)}15%{transform:scale(1.12)}}
.timer .msg{font-family:var(--display);font-weight:800;font-size:clamp(40px,4.6vw,90px);line-height:1}
.timer .wait{color:var(--mist);font-weight:600;font-size:clamp(20px,1.8vw,32px)}
/* --- полноэкранные состояния --- */
.full{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;min-height:0}
.full .big{font-family:var(--display);font-weight:900;font-size:clamp(60px,8.4vw,170px);line-height:.95;letter-spacing:-.02em}
.full .sub{color:var(--mist);font-weight:600;font-size:clamp(20px,2vw,36px);margin-top:3vh}
.countnum{font-size:min(32vw,58vh);line-height:1;color:var(--chalk);filter:drop-shadow(0 0 40px var(--signal-soft));--roll-ms:520ms}
/* --- итоги: таблица, подстраивается под число участников --- */
.final{display:flex;flex-direction:column;min-height:0}
.final h1{font-family:var(--display);font-weight:900;font-size:clamp(40px,4.6vw,88px);margin:1.5vh 0 2.5vh;line-height:1}
.board{flex:1;min-height:0;display:grid;grid-auto-flow:column;grid-template-rows:repeat(var(--rows),auto);grid-template-columns:repeat(var(--cols),minmax(0,1fr));column-gap:3vw;align-content:start}
.board{--rh:calc((100vh - 3.2vh*2 - 3vw - 14vh) / var(--rows))}
.trow{display:grid;grid-template-columns:2.2em minmax(0,1fr) auto auto;align-items:center;gap:.8em;padding:0 .9em;height:min(var(--rh) - 6px,14vh);margin-bottom:6px;border-radius:var(--r-m);background:var(--surface);border:1px solid var(--line);font-size:clamp(14px,calc(var(--rh) * .38),46px);opacity:0;animation:rise .45s cubic-bezier(.2,.8,.2,1) forwards}
@keyframes rise{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
.trow .pl{color:var(--mist)}
.trow .pn{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.trow .sc{--roll-ms:900ms}
.trow .u{font-size:.6em;font-weight:600;color:var(--mist)}
.trow.win{background:var(--signal);border-color:var(--signal);color:var(--signal-ink)}
.trow.win .pl,.trow.win .u{color:var(--signal-ink)}
</style></head>
<body>
<canvas id="bg" aria-hidden="true"></canvas>
<div class="offline" id="offline">Нет связи с сервером — переподключаюсь…</div>
<div class="screen">
  <div class="head"><span class="wordmark"><i></i>Voice meter</span><span class="round" id="round"></span></div>

  <div class="stage" id="stage">
    <aside class="meter" id="meter" aria-label="Шкала громкости">
      <div class="mread"><span class="num" id="mNum">0</span><small>dB</small></div>
      <div class="mbox">
        <div class="ticks" id="ticks"></div>
        <div class="tubewrap"><div class="tube"><div class="seg"><div class="dim"></div><div class="lit" id="lit"></div></div></div><div class="pk" id="pk"></div></div>
      </div>
    </aside>

    <div class="view full pad" id="idle"><div class="big">Voice meter</div><div class="sub">Скоро начнём</div></div>

    <div class="view full pad" id="countdown" hidden><div class="sub" id="cdName" style="margin:0 0 2vh"></div><div class="countnum num" id="cdNum"></div></div>

    <div class="view play" id="game" hidden>
      <div class="game">
        <div class="who" id="who"></div>
        <div class="count num" id="live">0</div>
        <div class="count num" id="res" hidden></div>
        <div class="unit" id="unit">dB</div>
        <div class="peakinfo" id="peakInfo">максимум <span class="num" id="peakRoll"></span></div>
        <div class="timer" id="timer"></div>
      </div>
      <aside class="side"><div class="side-h">Результаты</div><div class="side-list" id="sideList"></div></aside>
    </div>

    <div class="view final" id="final" hidden><h1>Итоги</h1><div class="board" id="board"></div></div>
  </div>
</div>
<button class="sound" id="soundBtn">Включить звук и микрофон</button>
<a class="micchip" id="micChip" href="{{ base }}setup" target="_blank" rel="noopener" title="Открыть Setup"><i></i><span id="micText">Микрофон: подключаю…</span></a>
<script>{{ client_js|safe }}</script>
<script>{{ audio_js|safe }}</script>
<script>{{ bg_js|safe }}</script>
<script>
const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.search);
const micEnabled = params.get('mic') !== '0';     // ?mic=0 — второй экран только показывает, микрофон не слушает
let lastView = '', lastFinalKey = '', resFor = -1;
let lastPh = null, lastCd = null, lastTick = null, leadDone = false;
let lvDisp = 0, holdPk = 0, holdAt = 0, lastLocal = -1e9, localPk = 0;

/* ---------- Шкала ---------- */
(() => {
  let h = '';
  for (let v = 0; v <= 100; v += 10) h += `<span class="${v === 0 ? 'zero' : ''}" style="bottom:${v}%">${v}</span>`;
  $('ticks').innerHTML = h;
})();
function currentLevel(now){
  if (micEnabled && Mic.running && now - lastLocal < 400) return Mic.level;        // свой микрофон: без задержки сети
  if (now - LIVE.at < 1500) return LIVE.level;                                     // уровень от другого экрана
  return 0;
}
function updateMeter(now){
  const lv = currentLevel(now);
  lvDisp = Math.max(lv, lvDisp - 1.6);                       // мгновенный подъём, плавное падение: цифры читаются
  const ph = S ? phaseOf(S) : 'idle', p = S && S.participants[S.current];
  // Экран с микрофоном считает максимум раунда сразу у себя: ответ сервера приходит с задержкой,
  // а «максимум» не должен быть ниже числа, которое только что видели на экране.
  if (S && p && ph === 'play' && micEnabled && Mic.running && now - lastLocal < 400) {
    localPk = Math.max(localPk, Math.round(Mic.level * 10) / 10);
    if (localPk > p.score) p.score = localPk;
  } else if (ph !== 'play') localPk = 0;
  let pk;
  if (S && p && (ph === 'play' || ph === 'timeup')) pk = p.score;   // отметка рекорда раунда
  else {                                                     // до начала — удержание пика, как на приборе
    if (lvDisp >= holdPk) { holdPk = lvDisp; holdAt = now; }
    else if (now - holdAt > 1200) holdPk = Math.max(lvDisp, holdPk - .8);
    pk = holdPk;
  }
  $('lit').style.clipPath = `inset(${100 - lvDisp}% 0 0 0)`;
  $('pk').style.bottom = `${Math.max(0, Math.min(100, pk))}%`;
  $('pk').style.opacity = pk > 0.5 ? 1 : 0;
  $('mNum').textContent = Math.round(lvDisp);
  Bg.level(lvDisp);
}

/* ---------- Микрофон ---------- */
let pend = 0, lastSent = 0, wasRunning = false;
Mic.onLevel = lvl => {
  if (Mic.suspended) return;       // браузер приостановил звук: нули не отправляем, ведущий увидит «нет сигнала»
  const now = performance.now();
  lastLocal = now; pend = Math.max(pend, lvl);
  if (now - lastSent >= 40) { lastSent = now; if (socket.connected) socket.emit('mic_level', {level: pend}); pend = 0; }
};
function claim(){ if (micEnabled && Mic.running && socket.connected) socket.emit('mic_claim', {label: Mic.label}); }
socket.on('connect', claim);
function updateChip(){
  const c = $('micChip'), t = $('micText');
  if (!micEnabled) { c.hidden = true; return; }
  let cls = '', txt;
  if (Mic.error) { cls = 'bad'; txt = Mic.error; }
  else if (Mic.running && Mic.suspended) { cls = 'bad'; txt = 'Микрофон: ' + (Mic.label || 'вход по умолчанию') + ' · браузер приостановил звук, щёлкните по экрану'; }
  else if (Mic.running) { cls = 'ok'; txt = 'Микрофон: ' + (Mic.label || 'вход по умолчанию') + (Mic.note ? ' · ' + Mic.note : ''); }
  else txt = 'Микрофон: подключаю…';
  c.className = 'micchip ' + cls; t.textContent = txt;
}
Mic.onState = () => {
  updateChip(); refreshSoundBtn();
  if (Mic.running) claim();
  else if (wasRunning && socket.connected) socket.emit('mic_release');
  wasRunning = Mic.running;
};
addEventListener('storage', e => { if (e.key === VM_KEY && micEnabled) { Mic.reload(); Mic.start(); } });   // сменили вход в Setup — подхватываем сразу
if (micEnabled) Mic.start(); else updateChip();

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
  return {
    get on(){ return !!ctx && ctx.state === 'running'; },
    async enable(){ ensure(); try { await ctx.resume(); } catch (e) {} return this.on; },
    tryAuto(){ ensure(); ctx.resume().catch(() => {}); return this.on; },
    // d — задержка в секундах: звук попадает в момент, когда цифра встаёт на место
    count(d = 0){ tone(660, .16, {gain: .35, at: d}); },                                   // 5…1
    go(d = 0){ tone(990, .5, {type: 'triangle', gain: .4, at: d}); tone(1320, .5, {gain: .2, at: d}); }, // старт
    tick(d = 0){ tone(1150, .05, {type: 'square', gain: .09, at: d}); },                      // последние 5 секунд
    gong(d = 0){ [196, 294, 392, 523].forEach((f, i) => tone(f, 2.6 - i * .3, {gain: .26 - i * .04, attack: .01, at: d})); },
    lead(d = 0){ tone(784, .22, {type: 'triangle', gain: .3, at: d}); tone(1175, .5, {type: 'triangle', gain: .3, at: d + .13}); },  // обогнал лидера
  };
})();
const soundBtn = $('soundBtn');
function refreshSoundBtn(){
  const ok = Snd.on && !Mic.suspended && (Mic.running || !micEnabled);
  soundBtn.classList.toggle('off', ok);
  soundBtn.textContent = Mic.error ? 'Повторить подключение' : 'Включить звук и микрофон';
}
async function enableAll(){
  await Snd.enable();
  if (micEnabled) { if (Mic.running) await Mic.resume(); else await Mic.start(); }
  refreshSoundBtn();
}
soundBtn.onclick = enableAll;
// Любое касание или клавиша тоже включают звук: на проекторе обычно достаточно щёлкнуть мышкой
['pointerdown', 'keydown'].forEach(ev => addEventListener(ev, () => { if (!Snd.on || Mic.suspended) enableAll(); }, {passive: true}));
setTimeout(() => { Snd.tryAuto(); refreshSoundBtn(); }, 300);
setInterval(refreshSoundBtn, 1000);

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
    // обогнал всех, кто уже сыграл: короткий сигнал, один раз за раунд
    const best = Math.max(0, ...S.participants.filter(x => x.done).map(x => x.score));
    if (!leadDone && best > 0 && p && p.score > best) { leadDone = true; Snd.lead(); }
  } else { lastTick = null; if (ph !== 'timeup') leadDone = false; }
  if (ph === 'timeup' && lastPh === 'play') { Snd.gong(); socket.emit('sync'); }   // sync — на случай, если итог ещё в пути
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
    setRoll(el.querySelector('.sc'), fmt1(p.score));
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
  $('stage').classList.toggle('nometer', v === 'final');
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
  t.innerHTML = ph === 'timeup' ? `<div class="msg">Время!</div>` : `<div class="wait">${S.round} секунд, кричите в микрофон</div>`;
}

/* Центр экрана: в раунде живой уровень, после времени — итог участника с прокруткой цифр */
function renderCenter(ph, p){
  const timeup = ph === 'timeup';
  $('live').hidden = timeup; $('res').hidden = !timeup;
  $('peakInfo').hidden = ph !== 'play';
  $('unit').textContent = timeup ? 'dB · результат' : 'dB';
  if (timeup) {
    if (resFor !== S.current) {
      resFor = S.current;
      setRoll($('res'), '0.0', {up: true});
      setTimeout(() => { const q = S && S.participants[S.current]; if (q) setRoll($('res'), fmt1(q.score), {up: true}); }, 350);
    } else setRoll($('res'), fmt1(p.score), {up: true});
  } else {
    resFor = -1;
    $('live').textContent = Math.round(lvDisp);
    setRoll($('peakRoll'), fmt1(p.score));
  }
}

function renderFinal(){
  const list = S.participants.map((p, i) => ({...p, i})).sort((a, b) => b.score - a.score || a.i - b.i);
  const key = JSON.stringify(list.map(p => [p.i, p.score, p.name]));
  if (key === lastFinalKey) return; lastFinalKey = key;
  const n = list.length, cols = n <= 8 ? 1 : n <= 18 ? 2 : 3, rows = Math.ceil(n / cols);
  const b = $('board'); b.style.setProperty('--cols', cols); b.style.setProperty('--rows', rows);
  const top = n ? list[0].score : 0;
  let place = 0, prev = null;
  b.innerHTML = list.map((p, k) => {
    if (p.score !== prev) { place = k + 1; prev = p.score; }
    const win = place === 1 && top > 0;
    return `<div class="trow${win ? ' win' : ''}" style="animation-delay:${Math.min(k, 20) * .06}s">
      <span class="pl num">${place}</span><span class="pn">${esc(p.name)}</span><span class="sc num" data-v="${fmt1(p.score)}"></span><span class="u">dB</span></div>`;
  }).join('');
  b.querySelectorAll('.sc').forEach((el, k) => { setRoll(el, '0.0', {up: true}); setTimeout(() => setRoll(el, el.dataset.v, {up: true}), 250 + Math.min(k, 20) * 60); });
}

function frame(now){
  updateMeter(now);
  if (S) {
    const ph = phaseOf(S), p = S.participants[S.current];
    $('round').textContent = p && !S.finished ? `${S.current + 1} из ${S.participants.length}` : '';
    sounds(ph, p);
    if (ph === 'idle') { setView('idle'); resFor = -1; lastFinalKey = ''; clearSide(); }
    else if (ph === 'finished') { setView('final'); renderFinal(); resFor = -1; clearSide(); }
    else if (ph === 'countdown') {
      setView('countdown'); lastFinalKey = '';
      $('cdName').textContent = p.name + ', приготовьтесь';
      const n = String(Math.ceil(remaining(S)));
      if ($('cdNum')._text !== n) { setRoll($('cdNum'), n, {up: false}); Bg.pulse(); }
    } else {
      setView('game');
      lastFinalKey = '';
      if ($('who').textContent !== p.name) { $('who').textContent = p.name; fitWho(); }
      renderCenter(ph, p);
      renderTimer(ph);
      renderSide();
    }
  }
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
</script></body></html>"""

# ======================================================================
# Setup: выбор аудиовхода и чувствительности
# ======================================================================
SETUP_HTML = r"""<!doctype html>
<html lang="ru" data-theme="men" data-base="{{ base }}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#06140e"><title>Voice meter · Setup</title>
""" + FONTS_BASE + r"""
<style>{{ theme_css|safe }}
body{background:radial-gradient(120% 60% at 0 0,var(--ink-2),var(--ink) 60%)}
.app{max-width:720px;margin:0 auto;padding:20px 16px calc(32px + env(safe-area-inset-bottom));display:flex;flex-direction:column;gap:16px}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.top .wordmark{font-size:20px}
.links{display:flex;gap:16px}
.link{color:var(--mist);font-weight:600;font-size:14px;text-decoration:underline;text-underline-offset:3px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-l);padding:22px}
h2{margin:0 0 14px;font-size:17px;font-weight:600;color:var(--mist)}
.note{margin:0;color:var(--mist);font-size:14px;line-height:1.5}
.note b{color:var(--chalk)}
.btn{display:block;width:100%;border:0;border-radius:var(--r-m);padding:16px;font-size:17px;font-weight:800;margin-top:10px}
.btn.primary{background:var(--signal);color:var(--signal-ink)}
.btn.quiet{background:transparent;border:1px solid var(--line);color:var(--chalk);font-weight:600}
.btn:disabled{opacity:.5;cursor:default}
select{width:100%;background:var(--ink-2);border:1px solid var(--line);border-radius:var(--r-m);color:var(--chalk);font:inherit;font-weight:600;font-size:16px;padding:15px 14px}
select:disabled{opacity:.5}
.info{margin-top:14px;display:grid;grid-template-columns:auto 1fr;gap:6px 14px;font-size:14px}
.info dt{color:var(--mist)} .info dd{margin:0;font-weight:600;min-width:0;overflow-wrap:anywhere}
.msg{margin-top:14px;padding:12px 14px;border-radius:var(--r-s);font-size:14px;line-height:1.45;background:var(--ink-2);color:var(--mist)}
.msg.bad{background:var(--danger-bg);color:var(--danger)}
.msg.ok{color:var(--signal)}
.big{display:flex;align-items:baseline;gap:.4em;flex-wrap:wrap}
.big .num{font-size:84px;line-height:1;color:var(--chalk)}
.big small{color:var(--mist);font-weight:600;font-size:20px}
.big .dbfs{margin-left:auto;color:var(--mist);font-size:13px;font-variant-numeric:tabular-nums}
.hbarwrap{position:relative;margin:16px 0 4px;padding-bottom:26px}
.hbar{position:relative;height:46px;border:1px solid var(--line);border-radius:var(--r-m);background:rgba(6,20,14,.55)}
.hseg{position:absolute;inset:6px;
  -webkit-mask-image:repeating-linear-gradient(to right,#000 0,#000 calc(2% - 3px),transparent calc(2% - 3px),transparent 2%);
  mask-image:repeating-linear-gradient(to right,#000 0,#000 calc(2% - 3px),transparent calc(2% - 3px),transparent 2%)}
.hseg .dim,.hseg .lit{position:absolute;inset:0;background:var(--scale-x)}
.hseg .dim{opacity:.13}
.hseg .lit{clip-path:inset(0 100% 0 0)}
.hpk{position:absolute;top:-4px;bottom:-4px;width:4px;margin-left:-2px;left:0;border-radius:2px;background:var(--chalk);box-shadow:0 0 12px rgba(241,245,238,.7)}
.hticks{position:absolute;left:0;right:0;bottom:0;height:20px;font-family:var(--digits);font-weight:800;font-size:13px;color:var(--mist)}
.hticks span{position:absolute;transform:translateX(-50%);top:2px}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
@media (max-width:520px){.row2{grid-template-columns:1fr}.big .num{font-size:64px}}
.slider{margin-top:20px}
.slider .top2{display:flex;justify-content:space-between;align-items:baseline;gap:10px;font-weight:600}
.slider .top2 span:last-child{font-family:var(--digits);font-weight:900;font-size:22px;color:var(--signal)}
input[type=range]{width:100%;margin:12px 0 4px;accent-color:var(--signal)}
.opt{display:flex;align-items:flex-start;gap:12px;padding:12px 0;border-bottom:1px solid var(--line)}
.opt:last-of-type{border-bottom:0}
.opt input{margin-top:4px;width:20px;height:20px;accent-color:var(--signal);flex:none}
.opt b{display:block;font-weight:600}
.opt span{display:block;color:var(--mist);font-size:13px;line-height:1.4;margin-top:2px}
.saved{color:var(--signal);font-weight:600;font-size:14px;opacity:0;transition:opacity .3s}
.saved.on{opacity:1}
</style></head>
<body>
<main class="app">
  <div class="top"><span class="wordmark"><i></i>Voice meter · Setup</span>
    <span class="links"><a class="link" href="{{ base }}screen" target="_blank" rel="noopener">Экран для гостей</a><a class="link" href="{{ base }}" target="_blank" rel="noopener">Пульт ведущего</a></span></div>

  <p class="note">Микрофон слушает браузер <b>экрана для гостей</b>. Эта страница настраивает вход того компьютера и браузера, где она открыта, поэтому откройте её <b>там же, где открыт экран для гостей</b>. Настройки запоминаются, а открытый экран подхватывает смену входа сразу.</p>

  <section class="card">
    <h2>1. Аудиовход</h2>
    <select id="dev" aria-label="Аудиовход"></select>
    <button class="btn primary" id="allow">Разрешить доступ к микрофону</button>
    <button class="btn quiet" id="refresh">Обновить список устройств</button>
    <dl class="info" id="info"></dl>
    <div class="msg" id="msg" hidden></div>
    <div class="msg" style="margin-top:10px">Браузер видит те входы, которые отдаёт система (в Windows это драйверы MME/WASAPI, на Mac CoreAudio). ASIO-вход напрямую выбрать нельзя: используйте его системный вход или виртуальный кабель (Voicemeeter, ASIO4ALL). Нужного входа нет в списке? Подключите его и нажмите «Обновить».</div>
  </section>

  <section class="card">
    <h2>2. Проверка уровня</h2>
    <div class="big"><span class="num" id="bigNum">0</span><small>dB</small><span class="dbfs" id="dbfs"></span></div>
    <div class="hbarwrap">
      <div class="hbar"><div class="hseg"><div class="dim"></div><div class="lit" id="lit"></div></div><div class="hpk" id="pk"></div></div>
      <div class="hticks" id="hticks"></div>
    </div>
    <div class="row2">
      <button class="btn quiet" id="resetPk">Сбросить пик</button>
      <button class="btn primary" id="calib">Автонастройка</button>
    </div>
    <div class="msg" id="calMsg">Автонастройка: нажмите кнопку и 5 секунд кричите в микрофон так, как будут кричать участники. Чувствительность подберётся сама, чтобы самый громкий крик был около 92.</div>
    <div class="slider">
      <div class="top2"><span>Чувствительность</span><span id="trimVal">0 dB</span></div>
      <input type="range" id="trim" min="-30" max="30" step="1" value="0" aria-label="Чувствительность">
      <p class="note">Обычная речь должна показывать около 50–60, громкий крик 85–95. Шкала не доходит до верха — добавьте, упирается в 100 — убавьте.</p>
    </div>
  </section>

  <section class="card">
    <h2>3. Обработка звука браузером <span class="saved" id="saved" style="margin-left:10px">Сохранено</span></h2>
    <label class="opt"><input type="checkbox" id="agc"><div><b>Автоусиление (AGC)</b><span>Подстраивает громкость сам и сглаживает крики. Для конкурса лучше выключить.</span></div></label>
    <label class="opt"><input type="checkbox" id="ns"><div><b>Шумоподавление</b><span>Может «съедать» резкий крик. Лучше выключить.</span></div></label>
    <label class="opt"><input type="checkbox" id="ec"><div><b>Эхоподавление</b><span>Нужно только если звук из колонок попадает обратно в микрофон.</span></div></label>
  </section>
</main>
<script>{{ audio_js|safe }}</script>
<script>
const $ = id => document.getElementById(id);
const dev = $('dev');
let cfg = vmLoad(), pkDisp = 0, pkAt = 0, lvDisp = 0, calibrating = false, calMax = -120;

(() => {
  let h = '';
  for (let v = 0; v <= 100; v += 10) h += `<span style="left:${v}%">${v}</span>`;
  $('hticks').innerHTML = h;
})();
function flashSaved(){ const s = $('saved'); s.classList.add('on'); clearTimeout(flashSaved.t); flashSaved.t = setTimeout(() => s.classList.remove('on'), 1500); }
function showMsg(text, cls){ const m = $('msg'); m.hidden = !text; m.textContent = text || ''; m.className = 'msg ' + (cls || ''); }

async function refreshDevices(){
  let list = [];
  try { list = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === 'audioinput'); } catch (e) {}
  dev.innerHTML = '';
  if (!list.length) {
    dev.innerHTML = '<option>Аудиовходы не найдены</option>'; dev.disabled = true; $('allow').hidden = false; return;
  }
  dev.disabled = false;
  list.forEach((d, i) => {
    const o = document.createElement('option');
    o.value = d.deviceId; o.textContent = d.label || ('Аудиовход ' + (i + 1));
    dev.appendChild(o);
  });
  const c = vmLoad();
  const pick = list.find(d => d.deviceId && d.deviceId === c.deviceId) || list.find(d => d.label && d.label === c.label)
    || list.find(d => d.deviceId && d.deviceId === Mic.deviceId);
  dev.value = pick ? pick.deviceId : list[0].deviceId;
  $('allow').hidden = list.some(d => d.label);    // названия видны только после разрешения
}
function renderInfo(){
  const s = Mic.settings || {};
  const rows = Mic.running ? [
    ['Подключено', Mic.label || 'вход по умолчанию'],
    ['Каналов', s.channelCount != null ? s.channelCount : '—'],
    ['Частота', (Mic.sampleRate || '—') + ' Гц'],
    ['Автоусиление', s.autoGainControl ? 'включено' : 'выключено'],
    ['Шумоподавление', s.noiseSuppression ? 'включено' : 'выключено'],
    ['Эхоподавление', s.echoCancellation ? 'включено' : 'выключено'],
  ] : [];
  $('info').innerHTML = rows.map(r => `<dt>${r[0]}</dt><dd>${esc(r[1])}</dd>`).join('');
}
function esc(v){ return String(v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

Mic.onState = async () => {
  if (Mic.error) showMsg(Mic.error, 'bad');
  else if (Mic.note) showMsg(Mic.note, 'bad');
  else if (Mic.suspended) showMsg('Звук остановлен браузером. Нажмите в любом месте страницы.', 'bad');
  else if (Mic.running) showMsg('Микрофон работает. Скажите что-нибудь или крикните: шкала ниже должна двигаться.', 'ok');
  renderInfo();
  await refreshDevices();
};
Mic.onLevel = (lvl, dbfs) => {
  if (calibrating) calMax = Math.max(calMax, dbfs);
  const now = performance.now();
  lvDisp = Math.max(lvl, lvDisp - 1.6);
  if (lvDisp >= pkDisp) { pkDisp = lvDisp; pkAt = now; }
  else if (now - pkAt > 1500) pkDisp = Math.max(lvDisp, pkDisp - .6);
  $('bigNum').textContent = Math.round(lvDisp);
  $('dbfs').textContent = dbfs > -119 ? dbfs.toFixed(1) + ' dBFS' : '';
  $('lit').style.clipPath = `inset(0 ${100 - lvDisp}% 0 0)`;
  $('pk').style.left = pkDisp + '%';
};

dev.onchange = async () => {
  const o = dev.selectedOptions[0];
  cfg = vmSave({deviceId: dev.value, label: o ? o.textContent : ''}); flashSaved();
  await Mic.start();
};
$('allow').onclick = async () => { $('allow').disabled = true; await Mic.start(); $('allow').disabled = false; };
$('refresh').onclick = refreshDevices;
navigator.mediaDevices && navigator.mediaDevices.addEventListener && navigator.mediaDevices.addEventListener('devicechange', refreshDevices);
addEventListener('pointerdown', () => { if (Mic.suspended) Mic.resume(); }, {passive: true});

['agc', 'ns', 'ec'].forEach(k => {
  $(k).checked = !!cfg[k];
  $(k).onchange = async () => { cfg = vmSave({[k]: $(k).checked}); flashSaved(); await Mic.start(); };
});

function showTrim(v){ $('trimVal').textContent = (v > 0 ? '+' : v < 0 ? '−' : '') + Math.abs(v) + ' dB'; }
$('trim').value = cfg.trim; showTrim(cfg.trim);
$('trim').oninput = () => { const v = +$('trim').value; showTrim(v); Mic.setTrim(v); };
$('trim').onchange = () => { cfg = vmSave({trim: +$('trim').value}); flashSaved(); };
$('resetPk').onclick = () => { pkDisp = lvDisp; pkAt = performance.now(); };

$('calib').onclick = () => {
  if (!Mic.running || calibrating) return;
  calibrating = true; calMax = -120; $('calib').disabled = true;
  let left = 5;
  const msg = $('calMsg'); msg.className = 'msg ok';
  const step = () => {
    if (left > 0) { msg.textContent = `Кричите в микрофон! Осталось ${left} с`; left--; setTimeout(step, 1000); return; }
    calibrating = false; $('calib').disabled = false;
    if (calMax < -70) { msg.className = 'msg bad'; msg.textContent = 'Сигнала почти нет. Проверьте, что выбран нужный вход, и крикните громче.'; return; }
    // trim подбираем так, чтобы самый громкий момент пришёлся на 92 из 100
    const trim = Math.max(-30, Math.min(30, Math.round(.92 * (DB_HI - DB_LO) + DB_LO - calMax)));
    $('trim').value = trim; showTrim(trim); Mic.setTrim(trim); cfg = vmSave({trim}); flashSaved();
    msg.textContent = `Готово: чувствительность ${trim > 0 ? '+' : ''}${trim} dB. Проверьте ещё раз обычным криком, он должен доходить до 85–95.`;
    pkDisp = 0;
  };
  step();
};

Mic.start().then(() => refreshDevices());
</script></body></html>"""

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), allow_unsafe_werkzeug=True)
