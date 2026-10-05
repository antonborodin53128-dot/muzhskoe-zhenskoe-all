"""Точно в ноту — конкурс «Мужское / Женское» (розовая тема).

Участник поёт в микрофон. Справа налево едут стены, в каждой есть щель на высоте нужной ноты.
Шарик показывает высоту голоса. Стена засчитана, только если в момент касания шарик проходит в щель.
Не попала — шарик врезается, мир откатывается назад на расстояние между стенами и стена подъезжает снова
(новая попытка через GATE_RETRY секунд: отброс от быстрого к медленному, затем разгон до обычной скорости). Время забега ограничено, стен может быть сколько угодно:
очко за каждую пройденную. Ноты идут в случайном порядке, шесть нот перемешиваются кругами без повторов.
Нет голоса — шарик опускается вниз, и стена его не пускает.

  /         — пульт ведущего (телефон)
  /screen   — экран для гостей (проектор), он же слушает микрофон и определяет высоту тона
  /setup    — выбор аудиовхода, тюнер и проверка попадания (открывать на компьютере с микрофоном)

Высоту тона считает браузер экрана для гостей (алгоритм YIN) и присылает на сервер через Socket.IO.
Попадания судит сервер по своим часам, поэтому пульт и все экраны всегда показывают одно и то же.
Ноты сравниваются по названию, без привязки к октаве: любой голос поёт в своей октаве.
Все ссылки относительные, поэтому этот же файл работает и отдельно, и внутри общего сборника.
"""
import json
import math
import os
import random
import secrets
import tempfile
import time
from threading import RLock, Timer

from flask import Flask, redirect, render_template_string, request
from flask_socketio import SocketIO, emit, join_room


def _env_num(name, default, lo, hi, cast=float):
    try:
        return max(lo, min(hi, cast(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


PREP_SECONDS = _env_num("PREP_SECONDS", 5, 1, 30, int)
ROUND_SECONDS = _env_num("NOTE_ROUND", 60, 20, 300, int)   # сколько секунд длится забег одного участника
GATE_LEAD = 3.0        # когда первая стена доезжает до шарика (секунд после начала замера)
GATE_SPACING = 3.0     # промежуток между стенами, пока шарик проходит их без остановок
ATT_PRE = 0.10         # попытка начинается чуть раньше касания (запас на задержку связи)
ATT_POST = 0.12        # и заканчивается чуть позже
GATE_KNOCK = 0.6       # длится отброс назад после удара (от быстрого к медленному)
GATE_RAMP = 0.8        # за это время стена разгоняется от нуля до обычной скорости на обратном пути
GATE_NEED = 0.10       # столько секунд (в сумме) шарик должен быть в щели внутри этого окна
TOL = 0.5              # допуск в шагах равномерной шкалы нот (половина шага в обе стороны): щели у всех нот одной высоты
MAX_DT = 0.12          # один отсчёт высоты не может «стоить» больше этого времени (защита от пауз связи)
SCALE = (0, 2, 4, 5, 7, 9)                      # до ре ми фа соль ля (номер ноты от «до»)
KNOTS = (0, 2, 4, 5, 7, 9, 11, 12)              # ноты подряд; шаг шкалы = переход к следующей (ми–фа и си–до — полутон)
SEQ_LEN = int(math.ceil(ROUND_SECONDS / GATE_SPACING)) + 4   # столько стен хватит для самой быстрой игры
# Новая попытка: отброс назад на расстояние между стенами (быстро, затем всё медленнее), потом стена
# разгоняется от нуля до обычной скорости и подъезжает. Путь назад = путь вперёд, поэтому время считается так.
GATE_RETRY = ATT_POST + GATE_KNOCK + GATE_SPACING + GATE_RAMP / 2
MAX_PARTICIPANTS = 30
LIVE_INTERVAL = 0.08      # живая высота тона для других экранов — не чаще 12 раз в секунду
# Состояние игры переживает перезапуск процесса: ведущий не теряет конкурс из-за сбоя или перезагрузки сервера.
STATE_FILE = os.environ.get("NOTE_STATE_FILE", os.path.join(tempfile.gettempdir(), "note_contest_state.json"))
STATE_TTL = 6 * 3600
BOOT = secrets.token_hex(4)   # меняется при каждом запуске: клиенты понимают, что номера состояний начались заново

app = Flask(__name__)
import rules; rules.install(app, "note")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading", ping_interval=15, ping_timeout=25)

# ВАЖНО. socketio.emit() может прямо внутри себя закрыть «протухшее» соединение (например, свёрнутое окно)
# и тут же вызвать on_disconnect в том же потоке. Если emit() вызван под этой блокировкой, а on_disconnect
# тоже берёт её, поток зависает сам на себе, и весь сервер перестаёт отвечать.
# Поэтому: под блокировкой только меняем состояние и собираем снимок, а рассылаем всегда ПОСЛЕ неё.
lock = RLock()

state = {
    "participants": [],   # [{"name": str, "score": int, "done": bool}]; score — сколько стен пройдено (по очку за стену)
    "current": -1,
    "finished": False,
    "started_at": None,   # момент нажатия «Запустить время»; дальше отсчёт и забег
    "round": ROUND_SECONDS,
    "bump": 0,
    "rev": 0,             # номер снимка: клиент игнорирует снимок, который пришёл позже более нового
    "seed": 0,
    "seq": [],            # ноты стен по порядку (номер от «до»), одинаковые для всех участников
}
# Накопленное «держание» ноты в текущем забеге. Хранится только в памяти.
# blocked — сколько секунд забега ушло на повторные попытки (время перед стеной стоит). att — номер попытки.
run = {"token": None, "hold": 0.0, "att": 0, "last": None, "blocked": 0.0}
# Какой экран сейчас слушает микрофон. Принимаем высоту тона только от него.
mic = {"sid": None, "label": "", "emit_at": 0.0}


def now():
    return time.time()


def make_sequence(seed, n=SEQ_LEN):
    """Ноты стен: шесть нот перемешиваются кругами, поэтому одна нота не повторяется, пока не спеты остальные."""
    rnd = random.Random(seed)
    seq = []
    while len(seq) < n:
        bag = list(SCALE)
        rnd.shuffle(bag)
        if seq and bag[0] == seq[-1]:
            bag[0], bag[-1] = bag[-1], bag[0]          # на стыке кругов одинаковые ноты подряд не ставим
        seq.extend(bag)
    return seq[:n]


def pos_of(v):
    """Высота в полутонах -> позиция на равномерной шкале нот (между соседними нотами один шаг)."""
    k = math.floor(v / 12.0)
    r = v - 12.0 * k
    i = 0
    while i < len(KNOTS) - 2 and r >= KNOTS[i + 1]:
        i += 1
    return 7 * k + i + (r - KNOTS[i]) / (KNOTS[i + 1] - KNOTS[i])


def pdist(m, target):
    """Разница высот в шагах шкалы по кругу (от -3.5 до +3.5), октава не важна."""
    return ((pos_of(m) - pos_of(target) + 3.5) % 7.0) - 3.5


def circ(d):
    """Разница высот в полутонах по кругу: от -6 до +6, октава не важна."""
    return (d + 6.0) % 12.0 - 6.0


def gate_time(i):
    """Когда i-я стена касается шарика (в секундах забега без учёта остановок)."""
    return GATE_LEAD + i * GATE_SPACING


def phase_locked(t=None):
    t = now() if t is None else t
    if not state["participants"]:
        return "idle"
    if state["finished"]:
        return "finished"
    if state["started_at"] is None:
        return "ready"
    elapsed = t - state["started_at"]
    if elapsed < PREP_SECONDS:
        return "countdown"
    if elapsed < PREP_SECONDS + state["round"]:
        return "play"
    return "timeup"


def current_player_locked():
    i = state["current"]
    if 0 <= i < len(state["participants"]):
        return state["participants"][i]
    return None


def new_player(i):
    return {"name": f"Участник {i + 1}", "score": 0, "done": False}


def reset_run_locked():
    run.update(token=state["started_at"], hold=0.0, att=0, last=None, blocked=0.0)


def clear_player_locked(player):
    player.update(score=0, done=False)


def snapshot_locked():
    t = now()
    state["rev"] += 1
    return {
        "participants": [dict(p) for p in state["participants"]],
        "current": state["current"],
        "finished": state["finished"],
        "started_at": state["started_at"],
        "bump": state["bump"],
        "prep": PREP_SECONDS,
        "round": state["round"],
        "seq": list(state["seq"]),
        "game": {"lead": GATE_LEAD, "spacing": GATE_SPACING, "retry": GATE_RETRY, "knock": GATE_KNOCK, "ramp": GATE_RAMP, "pre": ATT_PRE, "post": ATT_POST, "need": GATE_NEED, "tol": TOL},
        "blocked": round(run["blocked"], 3) if run["token"] == state["started_at"] else 0.0,
        "mic": {"on": mic["sid"] is not None, "label": mic["label"]},
        "server_now": t,
        "rev": state["rev"],
        "boot": BOOT,
    }


def publish(snap):
    """Разослать снимок всем. Вызывать только когда блокировка уже отпущена."""
    socketio.emit("state", snap)


def save_locked():
    data = {k: state[k] for k in ("participants", "current", "finished", "started_at", "round", "bump", "seed", "seq")}
    data["saved_at"] = now()
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass   # диск недоступен — игра всё равно идёт, просто без сохранения


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if now() - float(data["saved_at"]) > STATE_TTL:
            return
        parts = []
        for p in data["participants"][:MAX_PARTICIPANTS]:
            parts.append({"name": str(p["name"])[:32], "score": max(0, int(p["score"])), "done": bool(p["done"])})
        if not parts:
            return
        cur, finished = int(data["current"]), bool(data["finished"])
        if not finished and not 0 <= cur < len(parts):
            return
        seed = int(data["seed"])
        state.update(participants=parts, current=cur, finished=finished, round=ROUND_SECONDS,
                     seed=seed, seq=make_sequence(seed), bump=int(data.get("bump", 0)) + 1,
                     started_at=None if data["started_at"] is None else float(data["started_at"]))
        # Забег, который шёл в момент остановки сервера, продолжить нельзя: накопленное «держание» потеряно.
        # Участник играет заново, ведущий снова нажимает «Запустить время».
        if not finished and state["started_at"] is not None and phase_locked() in ("countdown", "play"):
            clear_player_locked(parts[cur])
            state["started_at"] = None
    except (OSError, ValueError, KeyError, TypeError):
        pass


load_state()


def schedule_round_end_locked():
    """Когда забег закончится, всем уходит итоговое состояние."""
    token = state["started_at"]
    delay = PREP_SECONDS + state["round"] - (now() - token) + 0.3
    timer = Timer(max(0.1, delay), round_end, args=(token,))
    timer.daemon = True
    timer.start()


def round_end(token):
    with lock:
        if state["started_at"] != token:
            return
        save_locked()
        snap = snapshot_locked()
    publish(snap)


# ---------- события ----------

@socketio.on("connect")
def on_connect(data=None):
    with lock:
        snap = snapshot_locked()
    emit("state", snap)


@socketio.on("disconnect")
def on_disconnect(*args):
    snap = None
    with lock:
        if mic["sid"] == request.sid:
            mic.update(sid=None, label="")
            snap = snapshot_locked()
    if snap:
        publish(snap)


@socketio.on("sync")
def on_sync(data=None):
    with lock:
        snap = snapshot_locked()
    emit("state", snap)


@socketio.on("watch")
def on_watch(data=None):
    """Экран для гостей просит живую высоту тона. Пульт ведущего её не получает."""
    join_room("screens")


@socketio.on("mic_claim")
def on_mic_claim(data=None):
    label = " ".join(str((data or {}).get("label", "")).split())[:80]
    with lock:
        mic.update(sid=request.sid, label=label)
        snap = snapshot_locked()
    publish(snap)


@socketio.on("mic_release")
def on_mic_release(data=None):
    snap = None
    with lock:
        if mic["sid"] == request.sid:
            mic.update(sid=None, label="")
            snap = snapshot_locked()
    if snap:
        publish(snap)


@socketio.on("pitch")
def on_pitch(data=None):
    """Высота тона от экрана с микрофоном: m — номер ноты от «до» (0–12, дробный) или None, s — звук устойчивый."""
    data = data or {}
    m = data.get("m")
    if m is not None:
        try:
            m = float(m)
        except (TypeError, ValueError):
            return
        if not math.isfinite(m):
            return
    stable = bool(data.get("s"))
    snap = live = None
    with lock:
        if mic["sid"] != request.sid:
            return
        t = now()
        player = current_player_locked()
        hold, gi = 0.0, -1
        if player and phase_locked(t) == "play" and run["token"] == state["started_at"]:
            wi = player["score"]                                   # стена, которая сейчас перед шариком
            raw = t - state["started_at"] - PREP_SECONDS           # время забега без остановок
            u = raw - run["blocked"] - gate_time(wi)               # сколько секунд прошло от первого касания стены
            last, run["last"] = run["last"], t
            dt = 0.0 if last is None else max(0.0, min(t - last, MAX_DT))
            k = max(0, int(math.floor((u + ATT_PRE) / GATE_RETRY))) if u >= -ATT_PRE else -1
            if k != run["att"]:
                run["att"], run["hold"] = max(k, 0), 0.0           # началась новая попытка: счёт сначала
            phase = u - k * GATE_RETRY if k >= 0 else None
            if k >= 0 and -ATT_PRE <= phase <= ATT_POST:          # идёт окно попытки: шарик должен быть в щели
                gi = wi
                if m is not None and stable and abs(pdist(m, state["seq"][wi])) <= TOL:
                    run["hold"] += dt
                if run["hold"] >= GATE_NEED:                       # прошёл в щель
                    run["blocked"] += k * GATE_RETRY               # время неудачных попыток не засчитывается как движение
                    player["score"] += 1                           # одна стена = одно очко
                    run["hold"], run["att"] = 0.0, 0
                    snap = snapshot_locked()
                hold = run["hold"]
        if t - mic["emit_at"] >= LIVE_INTERVAL:
            mic["emit_at"] = t
            live = {"m": None if m is None else round(m % 12.0, 2), "s": int(stable), "h": round(hold, 2), "i": gi}
    if snap:
        publish(snap)
    if live:
        socketio.emit("live", live, to="screens")


@socketio.on("setup")
def on_setup(data=None):
    data = data or {}
    try:
        count = int(data.get("count", 4))
    except (TypeError, ValueError):
        count = 4
    count = max(1, min(MAX_PARTICIPANTS, count))
    with lock:
        seed = secrets.randbelow(10 ** 9)
        state.update(participants=[new_player(i) for i in range(count)], current=0, finished=False,
                     started_at=None, round=ROUND_SECONDS, seed=seed, seq=make_sequence(seed))
        state["bump"] += 1
        reset_run_locked()
        save_locked()
        snap = snapshot_locked()
    publish(snap)


@socketio.on("start_timer")
def on_start_timer(data=None):
    with lock:
        if phase_locked() != "ready":
            return
        player = current_player_locked()
        clear_player_locked(player)
        state["started_at"] = now()
        reset_run_locked()
        schedule_round_end_locked()
        save_locked()
        snap = snapshot_locked()
    publish(snap)


@socketio.on("replay")
def on_replay(data=None):
    """Переиграть забег текущего участника. Ноты те же, что у всех."""
    with lock:
        player = current_player_locked()
        if not player or state["finished"]:
            return
        clear_player_locked(player)
        state["started_at"] = None
        state["bump"] += 1
        reset_run_locked()
        save_locked()
        snap = snapshot_locked()
    publish(snap)


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
        reset_run_locked()
        save_locked()
        snap = snapshot_locked()
    publish(snap)


@socketio.on("rename")
def on_rename(data=None):
    data = data or {}
    try:
        i = int(data.get("index", -1))
    except (TypeError, ValueError):
        return
    name = " ".join(str(data.get("name", "")).split())[:32]
    with lock:
        if not 0 <= i < len(state["participants"]):
            return
        state["participants"][i]["name"] = name or f"Участник {i + 1}"
        save_locked()
        snap = snapshot_locked()
    publish(snap)


@socketio.on("reset")
def on_reset(data=None):
    with lock:
        state.update(participants=[], current=-1, finished=False, started_at=None, seq=[])
        state["bump"] += 1
        reset_run_locked()
        save_locked()
        snap = snapshot_locked()
    publish(snap)


# ---------- страницы ----------

def base_path():
    # "/" отдельно или "/women/note/" внутри сборника
    return (request.script_root or "") + "/"


@app.after_request
def no_cache(resp):
    if request.path in ("/", "/screen", "/setup"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def page(template):
    return render_template_string(
        template, base=base_path(), round=ROUND_SECONDS,
        theme_css=THEME_CSS, client_js=CLIENT_JS, audio_js=PITCH_JS + AUDIO_JS, bg_js=BG_JS)


@app.get("/")
def control():
    return page(CONTROL_HTML)


@app.get("/healthz")
def healthz():
    # Пинг от открытых страниц: не даёт бесплатному хостингу «заснуть» и позволяет странице понять, жив ли сервер
    return "ok", 200, {"Cache-Control": "no-store", "Content-Type": "text/plain"}


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
# Тема задаётся атрибутом data-theme="men" | "women" на <html>. Здесь везде women (розовая).
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
  --glow:#ff9bd0; --warm:#ffe3f1;
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
.offbtn{margin-left:12px;border:1px solid currentColor;background:none;color:inherit;border-radius:999px;padding:4px 14px;font:inherit;font-weight:700;cursor:pointer}
[hidden]{display:none!important}
@media (prefers-reduced-motion:reduce){*,*:before,*:after{animation-duration:.01ms!important;transition-duration:.01ms!important}}
"""

# Общая логика клиента: подключение, синхронизация часов, вычисление фазы, табло.
CLIENT_JS = r"""
const BASE = document.documentElement.dataset.base || '/';
// tryAllTransports: если WebSocket не поднялся (прокси, сеть), соединение пойдёт обычными запросами, а не оборвётся.
const socket = io({path: BASE + 'socket.io', transports: ['websocket', 'polling'], tryAllTransports: true,
                   reconnectionDelay: 400, reconnectionDelayMax: 2500, timeout: 8000});
let S = null, clockOffset = 0, bootId = null, LIVE = {m: null, s: 0, h: 0, i: -1, at: -1e9};
const offsets = [];
const offlineBar = document.getElementById('offline');
if (offlineBar) {
  const b = document.createElement('button'); b.className = 'offbtn'; b.textContent = 'Обновить страницу';
  b.onclick = () => location.reload(); offlineBar.appendChild(b);
}
let downSince = 0;
const setOffline = on => { if (offlineBar) offlineBar.classList.toggle('on', on); };
socket.on('connect', () => { downSince = 0; setOffline(false); });
socket.on('disconnect', () => { if (!downSince) downSince = performance.now(); setOffline(true); });
socket.on('connect_error', () => { if (!downSince) downSince = performance.now(); setOffline(true); });
socket.on('state', s => {
  if (s.boot !== bootId) { bootId = s.boot; S = null; offsets.length = 0; }   // сервер перезапускался: номера снимков пошли заново
  else if (S && s.rev < S.rev) return;                                        // более старый снимок обогнал новый — не откатываемся
  // Поправка часов: берём лучшую из последних замеров (у неё самая короткая задержка сети), так таймер не дёргается.
  offsets.push(s.server_now - Date.now() / 1000); if (offsets.length > 12) offsets.shift();
  clockOffset = Math.max(...offsets);
  S = s; window.onState && window.onState(s);
});
// Живая высота тона от экрана с микрофоном (нужна экранам без своего микрофона).
socket.on('live', m => {
  LIVE = {m: m.m, s: m.s, h: m.h, i: m.i, at: performance.now()};
  window.onLive && window.onLive(LIVE);
});
const wake = () => { if (document.hidden) return; if (socket.connected) socket.emit('sync'); else { try { socket.connect(); } catch (e) {} } };
document.addEventListener('visibilitychange', wake);
addEventListener('online', wake);
addEventListener('focus', wake);
addEventListener('pageshow', wake);
// Пинг сервера раз в 4 минуты: хостинг не засыпает, пока открыта хотя бы одна страница.
setInterval(() => { fetch(BASE + 'healthz', {cache: 'no-store'}).catch(() => {}); }, 240000);
// Связи нет дольше 20 секунд, а сервер по обычному запросу отвечает: сокет «залип». Свежая страница вернёт связь,
// состояние конкурса хранится на сервере и никуда не денется.
setInterval(async () => {
  if (socket.connected || document.hidden) return;
  if (!downSince) downSince = performance.now();
  if (performance.now() - downSince < 20000) return;
  downSince = performance.now();
  try { const r = await fetch(BASE + 'healthz', {cache: 'no-store'}); if (r.ok && !socket.connected) location.reload(); } catch (e) {}
}, 5000);
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
// Время забега: 0 — начало замера, до этого отрицательное (идёт отсчёт). null — забег не запущен.
function rawTime(s){ return !s || s.started_at == null ? null : serverNow() - s.started_at - s.prep; }
// Время забега с остановками: пока стена не открыта, время перед ней стоит. Стена i касается шарика в lead + i * spacing.
function wallAt(g, i){ return g.lead + i * g.spacing; }
function courseTime(s){ const r = rawTime(s); if (r == null) return null; const p = s.participants[s.current]; return !p || !s.game ? r : Math.min(r - (s.blocked || 0), wallAt(s.game, p.score)); }
function fmtTime(sec){ sec = Math.max(0, Math.ceil(sec)); return Math.floor(sec / 60) + ':' + String(sec % 60).padStart(2, '0'); }
const passedOf = p => p.score;
function noteWord(n){ const a = n % 10, b = n % 100; if (a === 1 && b !== 11) return 'нота'; if (a >= 2 && a <= 4 && (b < 12 || b > 14)) return 'ноты'; return 'нот'; }
/* ---------- Табло ----------
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
function esc(v){ return String(v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function ranking(s){ return s.participants.map((p, i) => ({...p, i})).filter(p => p.done).sort((a, b) => b.score - a.score || passedOf(b) - passedOf(a) || a.i - b.i); }
"""

# ======================================================================
# Высота тона: алгоритм YIN + разбор «поёт / не поёт» + устойчивость.
# Чистые функции без доступа к браузеру: этот же код проверяется в Node на синтетическом голосе.
#
# Что делает попадания в ноты надёжнее:
#  1. Ноты сравниваются по названию, без октавы. Главная ошибка определения высоты (скачок на октаву)
#     перестаёт иметь значение, а каждый голос поёт в своей октаве.
#  2. Звук перед анализом чистится: срезаем гул ниже 80 Гц (стук по микрофону, ветер) и шипение выше 2,2 кГц.
#  3. Порог «есть голос» плавающий: он поднимается над шумом зала, поэтому разговоры и музыка вдалеке не дают нот.
#  4. Нота засчитывается, только если высота устойчива (разброс последних замеров не больше полутона):
#     речь и крики скользят, а пение держится.
#  5. Короткие провалы звука (вдох, согласная) до 100 мс не обрывают ноту.
#  6. Медиана последних замеров убирает одиночные выбросы.
# ======================================================================
PITCH_JS = r"""
const PITCH_FMIN = 75, PITCH_FMAX = 1050;
const PITCH_THR = 0.18;          // порог YIN: чем меньше, тем строже (0.18 ≈ «чистота» 82%)
const STABLE_RANGE = 1.0;        // разброс последних замеров (полутонов), при котором нота считается устойчивой
const HOLD_TICKS = 4;            // сколько замеров подряд провал звука не обрывает ноту
const ABS_GATE = -56;            // абсолютный порог громкости, dBFS
function circ12(d){ return ((d + 6) % 12 + 12) % 12 - 6; }
function mod12(v){ return ((v % 12) + 12) % 12; }
// Равномерная шкала нот: между соседними нотами один шаг, как на экране (ми–фа и си–до — полутон, остальные — тон).
const KNOTS = [0, 2, 4, 5, 7, 9, 11, 12];
function posOf(v){
  const k = Math.floor(v / 12), r = v - 12 * k;
  let i = 0; while (i < KNOTS.length - 2 && r >= KNOTS[i + 1]) i++;
  return 7 * k + i + (r - KNOTS[i]) / (KNOTS[i + 1] - KNOTS[i]);
}
function pdist(m, target){ return ((posOf(m) - posOf(target)) % 7 + 10.5) % 7 - 3.5; }     // разница в шагах шкалы, по кругу, без привязки к октаве

/* YIN: возвращает {hz, clarity} или hz = 0, если период не найден. buf — Float32Array, sr — частота дискретизации. */
function yinDetect(buf, sr, out){
  out = out || {};
  const W = buf.length >> 1;
  const tauMin = Math.max(2, Math.floor(sr / PITCH_FMAX));
  const tauMax = Math.min(W - 2, Math.ceil(sr / PITCH_FMIN));
  const need = tauMax + 2;
  if (!yinDetect._d || yinDetect._d.length < need) { yinDetect._d = new Float32Array(need); yinDetect._c = new Float32Array(need); }
  const d = yinDetect._d, cm = yinDetect._c;
  for (let tau = 1; tau <= tauMax + 1; tau++) {
    let s = 0;
    for (let j = 0; j < W; j++) { const x = buf[j] - buf[j + tau]; s += x * x; }
    d[tau] = s;
  }
  cm[0] = 1;
  let run = 0;
  for (let tau = 1; tau <= tauMax + 1; tau++) { run += d[tau]; cm[tau] = run > 0 ? d[tau] * tau / run : 1; }
  let best = -1, gmin = -1;
  for (let tau = tauMin; tau <= tauMax; tau++) {
    if (gmin < 0 || cm[tau] < cm[gmin]) gmin = tau;
    if (cm[tau] < PITCH_THR) {
      while (tau + 1 <= tauMax && cm[tau + 1] < cm[tau]) tau++;     // спускаемся до дна ямы
      best = tau; break;
    }
  }
  if (best < 0) best = gmin;
  if (best < 0) { out.hz = 0; out.clarity = 0; return out; }
  const a = cm[best - 1], b = cm[best], c = cm[best + 1], den = a - 2 * b + c;
  let shift = den !== 0 ? .5 * (a - c) / den : 0;
  if (!(shift > -1 && shift < 1)) shift = 0;
  out.hz = sr / (best + shift);
  out.clarity = Math.max(0, 1 - b);
  return out;
}
function hzToMidi(hz){ return 69 + 12 * Math.log2(hz / 440); }
const NOTE_NAMES = ['Do','Do♯','Re','Re♯','Mi','Fa','Fa♯','Sol','Sol♯','La','La♯','Si'];

/* Разбор по кадрам. Один объект на один микрофон. */
function createVoicing(){
  const st = {floor: -70, hist: [], missed: 99, last: null, tmp: {}};
  function process(buf, sr, gateOffset, calNoise){
    let sum = 0;
    for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
    const ms = sum / buf.length, dbfs = ms > 1e-12 ? 10 * Math.log10(ms) : -120;
    // плавающий шум: быстро опускается, медленно поднимается (длинная нота не поднимет его до уровня голоса)
    if (dbfs < st.floor) st.floor += .3 * (dbfs - st.floor); else st.floor += .002 * (dbfs - st.floor);
    st.floor = Math.max(-90, Math.min(-35, st.floor));
    const noise = Math.max(st.floor, calNoise == null ? -120 : calNoise);
    const gate = Math.max(ABS_GATE + (gateOffset || 0), noise + 8);
    const f = {dbfs, gate, voiced: false, held: false, hz: 0, clarity: 0, midi: 0, pc: 0, stable: false, floor: st.floor};
    let ok = false;
    if (dbfs >= gate && dbfs < -2) {
      const r = yinDetect(buf, sr, st.tmp);
      f.hz = r.hz; f.clarity = r.clarity;
      ok = r.hz >= PITCH_FMIN && r.hz <= PITCH_FMAX && r.clarity >= 1 - PITCH_THR;
    }
    if (ok) {
      f.midi = hzToMidi(f.hz);
      st.hist.push(mod12(f.midi)); if (st.hist.length > 5) st.hist.shift();
      st.missed = 0;
      f.voiced = true;
    } else if (st.last && st.missed < HOLD_TICKS) {       // короткий провал: держим прошлую ноту
      st.missed++;
      return Object.assign(f, {voiced: true, held: true, midi: st.last.midi, pc: st.last.pc, stable: st.last.stable, hz: st.last.hz, clarity: st.last.clarity});
    } else { st.hist.length = 0; st.missed = 99; st.last = null; return f; }
    // медиана и разброс последних замеров — по кругу (до и ля-диез рядом)
    const ref = st.hist[st.hist.length - 1];
    const u = st.hist.map(v => ref + circ12(v - ref)).sort((x, y) => x - y);
    const med = u[u.length >> 1];
    f.pc = mod12(med);
    f.stable = u.length >= 3 && (u[u.length - 1] - u[0]) <= STABLE_RANGE;
    st.last = {midi: f.midi, pc: f.pc, stable: f.stable, hz: f.hz, clarity: f.clarity};
    return f;
  }
  function reset(){ st.hist.length = 0; st.missed = 99; st.last = null; st.floor = -70; }
  return {process, reset, get floor(){ return st.floor; }};
}
if (typeof module !== 'undefined') module.exports = {yinDetect, createVoicing, hzToMidi, circ12, mod12, posOf, pdist, PITCH_THR};
"""

# ======================================================================
# Микрофон: общий код для экрана и Setup.
# Настройки хранятся в localStorage браузера: экран и Setup должны быть
# открыты в одном браузере на одном компьютере.
# ======================================================================
AUDIO_JS = r"""
const NT_KEY = 'nt.settings.v1';
const NT_DEFAULT = {deviceId: '', label: '', gate: 0, noise: null, agc: false, ns: false, ec: false};
function ntLoad(){
  let s = {};
  try { s = JSON.parse(localStorage.getItem(NT_KEY) || '{}') || {}; } catch (e) {}
  if (!s.deviceId) {   // вход ещё не выбирали здесь: берём тот, что выбран в Voice meter на этом компьютере
    try { const v = JSON.parse(localStorage.getItem('vm.settings.v1') || '{}'); if (v.deviceId) s = {...s, deviceId: v.deviceId, label: v.label || ''}; } catch (e) {}
  }
  return {...NT_DEFAULT, ...s};
}
function ntSave(patch){ const s = {...ntLoad(), ...patch}; try { localStorage.setItem(NT_KEY, JSON.stringify(s)); } catch (e) {} return s; }

const Mic = (() => {
  const TICK = 25;                   // разбор звука каждые 25 мс
  let ctx = null, stream = null, an = null, buf = null, timer = null, tickWorker = null, retry = null, gen = 0, wanted = false, cfg = ntLoad(), lastMeasure = 0;
  const voicing = createVoicing();
  const api = {running: false, error: '', note: '', label: '', deviceId: '', settings: null, sampleRate: 0,
               frame: null, suspended: false, onFrame: null, onState: null};
  const fire = () => { api.suspended = !!ctx && ctx.state !== 'running'; api.onState && api.onState(api); };

  function explain(e){
    const n = e && e.name;
    if (n === 'NotAllowedError' || n === 'PermissionDeniedError') return 'Доступ к микрофону запрещён. Разрешите его в настройках сайта (значок замка в адресной строке) и обновите страницу.';
    if (n === 'NotFoundError' || n === 'DevicesNotFoundError') return 'Микрофон не найден. Проверьте подключение звуковой карты.';
    if (n === 'NotReadableError' || n === 'TrackStartError') return 'Микрофон занят другой программой или недоступен.';
    if (n === 'SecurityError') return 'Микрофон работает только по HTTPS или на localhost.';
    return 'Не удалось открыть микрофон: ' + ((e && e.message) || n || 'неизвестная ошибка');
  }
  /* Браузер сильно замедляет обычные таймеры свёрнутого окна, и разбор голоса «слепнет».
     Таймеры внутри отдельного потока (Worker) не замедляются, поэтому тикаем оттуда. Если Worker недоступен — обычный таймер. */
  function startTimer(){
    stopTimer();
    try {
      const url = URL.createObjectURL(new Blob(['setInterval(function(){postMessage(0)},' + TICK + ')'], {type: 'text/javascript'}));
      let first = true;
      tickWorker = new Worker(url);
      tickWorker.onmessage = () => { if (first) { first = false; URL.revokeObjectURL(url); } measure(); };
      tickWorker.onerror = () => { stopTimer(); timer = setInterval(measure, TICK); };
    } catch (e) { tickWorker = null; timer = setInterval(measure, TICK); }
  }
  function stopTimer(){
    clearInterval(timer); timer = null;
    if (tickWorker) { tickWorker.onmessage = tickWorker.onerror = null; tickWorker.terminate(); tickWorker = null; }
  }
  function teardown(){
    stopTimer();
    if (stream) { stream.getTracks().forEach(t => { t.onended = null; t.stop(); }); stream = null; }
    if (ctx) { ctx.onstatechange = null; ctx.close().catch(() => {}); ctx = null; }
    an = null; api.running = false; api.frame = null; api.sampleRate = 0; voicing.reset();
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
    const t = performance.now();
    if (t - lastMeasure < 15) return;           // отстающие тики не копим: лучше пропустить замер, чем отстать от голоса
    lastMeasure = t;
    an.getFloatTimeDomainData(buf);
    const f = voicing.process(buf, ctx.sampleRate, cfg.gate, cfg.noise);
    api.frame = f;
    api.onFrame && api.onFrame(f);
  }

  api.start = async () => {
    const my = ++gen; wanted = true; clearTimeout(retry);
    cfg = ntLoad(); api.note = ''; api.error = '';
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
    // Чистим звук перед анализом: без гула ниже 80 Гц и без шипения выше 2,2 кГц
    const hp = ctx.createBiquadFilter(); hp.type = 'highpass'; hp.frequency.value = 80; hp.Q.value = .7;
    const lp = ctx.createBiquadFilter(); lp.type = 'lowpass'; lp.frequency.value = 2200; lp.Q.value = .7;
    an = ctx.createAnalyser(); an.fftSize = 2048; an.smoothingTimeConstant = 0;
    ctx.createMediaStreamSource(s).connect(hp); hp.connect(lp); lp.connect(an);
    buf = new Float32Array(an.fftSize); voicing.reset();
    startTimer();     // таймер, а не кадры: разбор не зависит от того, видна ли вкладка
    api.running = true; fire(); return true;
  };
  api.stop = () => { wanted = false; gen++; clearTimeout(retry); teardown(); api.error = ''; fire(); };
  api.resume = async () => {
    if (ctx && ctx.state !== 'running') { try { await ctx.resume(); } catch (e) {} }
    fire(); return !!ctx && ctx.state === 'running';
  };
  api.reload = () => { cfg = ntLoad(); };
  api.setGate = v => { cfg.gate = v; };
  api.setNoise = v => { cfg.noise = v; };
  return api;
})();
"""

# ======================================================================
# Фон с глубиной. Слои смещаются вместе с шариком (вверх и вниз), но слабее: чем дальше слой,
# тем меньше он двигается. Плюс медленный ход вправо-влево вместе с барьерами.
# Варианты выбираются адресом (?bg=notes|stars|waves|bokeh|city) или клавишей B на экране.
# ======================================================================
BG_JS = r"""
const Bg = (() => {
  const html = document.documentElement;
  const cv = document.getElementById('bg');
  if (!cv) return {pulse(){}, level(){}, recolor(){}, setPitch(){}, setFlow(){}, next(){}, variant: ''};
  const ctx = cv.getContext('2d');
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const VARIANTS = ['notes', 'stars', 'waves', 'bokeh', 'eq', 'city'];
  const params = new URLSearchParams(location.search);
  let variant = VARIANTS.includes(params.get('bg')) ? params.get('bg') : (VARIANTS.includes(html.dataset.bg) ? html.dataset.bg : 'stars');
  let W = 0, H = 0, dpr = 1, k = 1, flash = 0, flashTarget = 0, energy = 0, energyTarget = 0;
  let pitchTarget = .5, pitch = .5, flow = 0, flowShown = 0;
  const t0 = performance.now();
  let rgb = [255, 79, 168];
  function readColor(){
    const c = getComputedStyle(html).getPropertyValue('--signal').trim();
    const m = c.match(/^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i);
    if (m) rgb = m.slice(1).map(h => parseInt(h, 16));
  }
  const clamp01 = v => Math.max(0, Math.min(1, v));
  const rgba = (a, c) => { c = c || rgb; return `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${clamp01(a)})`; };
  const mix = (c, d, f) => [c[0] + (d[0] - c[0]) * f, c[1] + (d[1] - c[1]) * f, c[2] + (d[2] - c[2]) * f];
  const WHITE = [255, 255, 255], LILAC = [176, 120, 255], PEACH = [255, 170, 140], DEEP = [90, 10, 60];
  function size(){
    dpr = Math.min(2, devicePixelRatio || 1); W = innerWidth; H = innerHeight;
    k = Math.max(.8, Math.min(1.8, Math.min(W, H) / 800));
    cv.width = W * dpr; cv.height = H * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  addEventListener('resize', size); size(); readColor();

  // Предсказуемый «случайный» набор: раскладка слоёв не меняется при перерисовке
  function rng(seed){ let s = seed >>> 0; return () => (s = (s * 1664525 + 1013904223) >>> 0) / 4294967296; }
  // Смещение слоя глубиной d (0 — далеко, 1 — близко): вверх вместе с шариком, но медленнее
  const oy = d => -(pitch - .5) * H * (.10 + .34 * d);
  const ox = d => -flowShown * (.06 + .5 * d);
  const wrap = (v, size) => ((v % size) + size) % size;

  // ---------- ноты на нотном стане ----------
  function note(x, y, s, a, kind, tilt){
    ctx.save(); ctx.translate(x, y); ctx.scale(s, s);
    ctx.strokeStyle = ctx.fillStyle = rgba(a); ctx.lineWidth = Math.max(.05, 1.3 / s); ctx.lineCap = ctx.lineJoin = 'round';
    const head = (hx, hy) => { ctx.beginPath(); ctx.ellipse(hx, hy, .34, .24, -.4, 0, 6.3); ctx.fill(); };
    if (kind === 0) {                       // восьмая
      head(0, 0); ctx.beginPath(); ctx.moveTo(.3, -.06); ctx.lineTo(.3, -1.5); ctx.bezierCurveTo(.3, -1.1, .95, -1.05, .8, -.45); ctx.stroke();
    } else if (kind === 1) {                // две восьмые под одной планкой
      head(-.5, .3); head(.6, 0);
      ctx.beginPath(); ctx.moveTo(-.2, .24); ctx.lineTo(-.2, -1.25); ctx.moveTo(.9, -.06); ctx.lineTo(.9, -1.5); ctx.stroke();
      ctx.lineWidth = Math.max(.1, 3 / s); ctx.beginPath(); ctx.moveTo(-.2, -1.25); ctx.lineTo(.9, -1.5); ctx.stroke();
    } else {                                // четвертная
      head(0, 0); ctx.beginPath(); ctx.moveTo(.3, -.06); ctx.lineTo(.3, -1.4); ctx.stroke();
    }
    ctx.restore();
  }
  const noteLayers = [0.12, .4, .8].map((d, li) => {
    const r = rng(11 + li * 97), items = [];
    const n = [9, 7, 5][li];
    for (let i = 0; i < n; i++) items.push({x: r(), y: r(), kind: Math.floor(r() * 3), ph: r() * 6, sc: .75 + r() * .5});
    const staves = []; const sn = [3, 2, 2][li];
    for (let i = 0; i < sn; i++) staves.push({y: (i + .5) / sn + (r() - .5) * .12});
    return {d, items, staves};
  });
  function drawNotes(t){
    noteLayers.forEach(L => {
      const d = L.d, a = .05 + d * .13, big = (22 + 70 * d) * k, gapY = (6 + 9 * d) * k;
      const offY = oy(d), offX = ox(d), W2 = W + 260 * k;
      L.staves.forEach(sv => {
        const y0 = sv.y * H + offY - gapY * 2;
        for (let i = 0; i < 5; i++) {
          ctx.strokeStyle = rgba(a * .75 + energy * .05); ctx.lineWidth = Math.max(.8, 1.1 * d * k);
          ctx.beginPath(); ctx.moveTo(0, y0 + i * gapY); ctx.lineTo(W, y0 + i * gapY); ctx.stroke();
        }
      });
      L.items.forEach(it => {
        const x = wrap(it.x * W2 + offX, W2) - 130 * k, y = ((it.y * 1.2 - .1) * H) + offY + Math.sin(t * .6 + it.ph) * 6 * k * (.4 + d);
        const g = ctx.createRadialGradient(x, y, 0, x, y, big * 1.5);
        g.addColorStop(0, rgba(a * .5 + flash * .06)); g.addColorStop(1, rgba(0));
        ctx.globalCompositeOperation = 'lighter'; ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y, big * 1.5, 0, 6.3); ctx.fill();
        ctx.globalCompositeOperation = 'source-over';
        note(x, y, big * it.sc, a * 1.5 + flash * .1 + energy * .1, it.kind);
      });
    });
  }

  // ---------- звёздное небо ----------
  const starLayers = [0.08, .3, .6, .95].map((d, li) => {
    const r = rng(501 + li * 31), items = [], n = [90, 60, 36, 14][li];
    for (let i = 0; i < n; i++) items.push({x: r(), y: r(), s: .5 + r() * 1.3, ph: r() * 6, tw: .5 + r() * 1.8, hue: r()});
    return {d, items};
  });
  const nebula = Array.from({length: 5}, (_, i) => { const r = rng(900 + i * 17); return {x: r(), y: r(), r: .25 + r() * .3, d: .05 + r() * .2, c: i % 2 ? LILAC : null}; });
  const shoot = {t: 0, x: 0, y: 0, on: false};
  function drawStars(t){
    nebula.forEach(n => {
      const x = wrap(n.x * (W + 400) + ox(n.d), W + 400) - 200, y = n.y * H + oy(n.d), R = n.r * Math.max(W, H);
      const g = ctx.createRadialGradient(x, y, 0, x, y, R), c = n.c || rgb;
      g.addColorStop(0, rgba(.16 + energy * .05, c)); g.addColorStop(1, rgba(0, c));
      ctx.fillStyle = g; ctx.fillRect(x - R, y - R, R * 2, R * 2);
    });
    starLayers.forEach(L => {
      const d = L.d, W2 = W + 40;
      L.items.forEach(s => {
        const x = wrap(s.x * W2 + ox(d), W2) - 20, y = wrap(s.y * H * 1.3 + oy(d), H * 1.3) - H * .15;
        const tw = .55 + .45 * Math.sin(t * s.tw + s.ph);
        const c = s.hue < .25 ? mix(WHITE, rgb, .5) : s.hue < .5 ? mix(WHITE, LILAC, .4) : WHITE;
        const a = (.25 + .6 * d) * tw + flash * .15, r = (s.s * (.6 + 1.6 * d) + energy * .6) * k;
        ctx.fillStyle = rgba(a, c); ctx.beginPath(); ctx.arc(x, y, r, 0, 6.3); ctx.fill();
        if (d > .5) {            // у ближних звёзд есть лучики
          ctx.strokeStyle = rgba(a * .6, c); ctx.lineWidth = 1;
          ctx.beginPath(); ctx.moveTo(x - r * 4, y); ctx.lineTo(x + r * 4, y); ctx.moveTo(x, y - r * 4); ctx.lineTo(x, y + r * 4); ctx.stroke();
        }
      });
    });
    if (!shoot.on && Math.random() < .004) Object.assign(shoot, {on: true, t: 0, x: Math.random() * W * .8 + W * .2, y: Math.random() * H * .4});
    if (shoot.on) {
      shoot.t += .016; const p = shoot.t / .9;
      if (p >= 1) shoot.on = false; else {
        const x = shoot.x - p * W * .35, y = shoot.y + p * H * .25 + oy(.6), g = ctx.createLinearGradient(x, y, x + 150 * k, y - 100 * k);
        g.addColorStop(0, rgba(.7 * (1 - p), WHITE)); g.addColorStop(1, rgba(0));
        ctx.strokeStyle = g; ctx.lineWidth = 2 * k; ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x + 150 * k, y - 100 * k); ctx.stroke();
      }
    }
  }

  // ---------- гряды из звуковых волн ----------
  const ridges = [0, 1, 2, 3, 4].map(i => ({d: .05 + i * .22, base: .46 + i * .11, amp: .05 + i * .012, f1: 1.1 + i * .35, f2: 2.7 + i * .6, ph: i * 1.7}));
  function drawWaves(t){
    // закатное солнце за дальней грядой
    const sx = W * .68 + ox(.02), sy = H * .4 + oy(.03), sr = Math.min(W, H) * .24;
    const sg = ctx.createRadialGradient(sx, sy, 0, sx, sy, sr * 2.2);
    sg.addColorStop(0, rgba(.5 + flash * .15, mix(rgb, PEACH, .5))); sg.addColorStop(.35, rgba(.2)); sg.addColorStop(1, rgba(0));
    ctx.fillStyle = sg; ctx.fillRect(sx - sr * 2.4, sy - sr * 2.4, sr * 4.8, sr * 4.8);
    ctx.fillStyle = rgba(.35, mix(rgb, PEACH, .6)); ctx.beginPath(); ctx.arc(sx, sy, sr * .55, 0, 6.3); ctx.fill();
    ridges.forEach((R, i) => {
      const d = R.d, y0 = R.base * H + oy(d), off = ox(d) * .01, A = (R.amp + energy * .02) * H;
      const g = ctx.createLinearGradient(0, y0 - A, 0, y0 + H * .35);
      const c = mix(rgb, DEEP, .15 + i * .17);
      g.addColorStop(0, rgba(.14 + d * .36 + flash * .05, c)); g.addColorStop(1, rgba(.55 + d * .4, mix(c, [10, 2, 8], .6)));
      ctx.fillStyle = g; ctx.beginPath(); ctx.moveTo(0, H + 40);
      for (let x = 0; x <= W + 12; x += 12) {
        const u = (x + ox(d) * 1.0) / W * 6.28;
        const y = y0 + Math.sin(u * R.f1 + R.ph + t * .15) * A + Math.sin(u * R.f2 + R.ph * 2 - t * .25) * A * .35;
        ctx.lineTo(x, y);
      }
      ctx.lineTo(W + 12, H + 40); ctx.closePath(); ctx.fill();
      ctx.strokeStyle = rgba(.18 + d * .3, mix(rgb, WHITE, .4)); ctx.lineWidth = (1 + d * 1.6) * k; ctx.stroke();
    });
  }

  // ---------- боке: огоньки и сердечки ----------
  function heart(x, y, r, a){
    ctx.beginPath(); ctx.moveTo(x, y + r * .9);
    ctx.bezierCurveTo(x - r * 1.5, y - r * .1, x - r * .8, y - r * 1.2, x, y - r * .45);
    ctx.bezierCurveTo(x + r * .8, y - r * 1.2, x + r * 1.5, y - r * .1, x, y + r * .9);
    ctx.fill();
  }
  const bokehLayers = [0.1, .38, .75].map((d, li) => {
    const r = rng(1301 + li * 71), items = [], n = [26, 16, 9][li];
    for (let i = 0; i < n; i++) items.push({x: r(), y: r(), r: .6 + r() * .8, v: .01 + r() * .02, ph: r() * 6, hue: r(), heart: li > 0 && r() < .3});
    return {d, items};
  });
  function drawBokeh(t){
    ctx.globalCompositeOperation = 'lighter';
    bokehLayers.forEach(L => {
      const d = L.d, R0 = (10 + 52 * d) * k, W2 = W + 200;
      L.items.forEach(it => {
        const x = wrap(it.x * W2 + ox(d) + Math.sin(t * .3 + it.ph) * 10, W2) - 100;
        const y = wrap(it.y * H * 1.2 + oy(d) - t * it.v * H * (.4 + d), H * 1.2) - H * .1;
        const r = R0 * it.r * (1 + flash * .25 + energy * .15);
        const c = it.hue < .6 ? rgb : it.hue < .85 ? mix(rgb, PEACH, .7) : mix(rgb, LILAC, .7);
        if (it.heart) { ctx.fillStyle = rgba(.14 + d * .16 + flash * .1, c); heart(x, y, r * .8, 1); return; }
        const g = ctx.createRadialGradient(x, y, 0, x, y, r);
        g.addColorStop(0, rgba(.1 + d * .12 + flash * .1, c)); g.addColorStop(.75, rgba(.1 + d * .1, c)); g.addColorStop(1, rgba(0, c));
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y, r, 0, 6.3); ctx.fill();
      });
    });
    ctx.globalCompositeOperation = 'source-over';
  }

  // ---------- эквалайзер: столбики звука, высота которых зависит от голоса ----------
  function drawEq(t){
    const layers = [{d: .08, n: 70, w: .55, a: .08, h: .16}, {d: .4, n: 38, w: .62, a: .13, h: .24}, {d: .8, n: 20, w: .7, a: .2, h: .34}];
    layers.forEach((L, li) => {
      const bw = (W + 60) / L.n, base = H * 1.02 + oy(L.d) * .9, off = ox(L.d) * .5;
      for (let i = -1; i <= L.n + 1; i++) {
        const x = wrap(i * bw + off, W + bw * 2) - bw;
        const wob = .5 + .5 * Math.sin(t * (.7 + li * .25) + i * .83 + li * 2.1) * Math.sin(t * .37 + i * .21);
        const h = H * L.h * (.25 + .75 * Math.abs(wob)) * (1 + energy * 1.3 + flash * .2);
        const bx = x + bw * (1 - L.w) / 2, bwid = bw * L.w;
        const g = ctx.createLinearGradient(0, base - h, 0, base);
        g.addColorStop(0, rgba(L.a * 1.6 + flash * .08, mix(rgb, WHITE, .25))); g.addColorStop(1, rgba(L.a * .35));
        ctx.fillStyle = g; ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(bx, base - h, bwid, h + 20, Math.min(bwid / 2, 10 * k)); else ctx.rect(bx, base - h, bwid, h + 20);
        ctx.fill();
      }
    });
    // тонкие линии света по горизонту
    const hz = H * .72 + oy(.3) * .5, g = ctx.createLinearGradient(0, hz - 60, 0, hz + 60);
    g.addColorStop(0, rgba(0)); g.addColorStop(.5, rgba(.05 + energy * .08)); g.addColorStop(1, rgba(0));
    ctx.fillStyle = g; ctx.fillRect(0, hz - 60, W, 120);
  }

  // ---------- неоновый город (тихая версия: только силуэты и редкие огни) ----------
  const cityLayers = [0.1, .38, .72].map((d, li) => {
    const r = rng(2203 + li * 53), blds = []; let x = 0;
    while (x < 2.4) { const w = (.05 + r() * .06) * (1 + (li ? 0 : -.2)), h = (.07 + r() * .15) * (1 + li * .45); blds.push({x, w, h, win: Math.floor(r() * 5)}); x += w + r() * .01; }
    return {d, blds, len: x, li};
  });
  function drawCity(t){
    const hz = H * .68 + oy(.05);
    const sx = W * .5 + ox(.02) * .3, sr = Math.min(W, H) * .22;
    const sg = ctx.createRadialGradient(sx, hz, sr * .2, sx, hz, sr * 2);
    sg.addColorStop(0, rgba(.3 + flash * .12, mix(rgb, PEACH, .4))); sg.addColorStop(1, rgba(0));
    ctx.fillStyle = sg; ctx.fillRect(sx - sr * 2, hz - sr * 2, sr * 4, sr * 2.4);
    ctx.save(); ctx.beginPath(); ctx.rect(0, 0, W, hz); ctx.clip();
    const sun = ctx.createLinearGradient(0, hz - sr, 0, hz);
    sun.addColorStop(0, rgba(.4, mix(rgb, PEACH, .75))); sun.addColorStop(1, rgba(.38, rgb));
    ctx.fillStyle = sun; ctx.beginPath(); ctx.arc(sx, hz, sr, Math.PI, 0); ctx.fill();
    ctx.fillStyle = 'rgba(20,7,16,.9)';                     // полосы на солнце
    for (let i = 0; i < 5; i++) { const yy = hz - sr * (.12 + i * .17), hh = 2 + i * 1.6; ctx.fillRect(sx - sr, yy, sr * 2, hh * k); }
    ctx.restore();
    cityLayers.forEach(L => {
      const d = L.d, total = L.len * W, offs = wrap(ox(d), total);
      const col = mix([26, 8, 22], rgb, .06 + L.li * .04);
      for (let rep = -1; rep <= 1; rep++) L.blds.forEach(b => {
        const bx = b.x * W - offs + rep * total, bw = b.w * W;
        if (bx > W + 10 || bx + bw < -10) return;
        const bh = b.h * H * (.8 + d * .6), by = hz + oy(d) * .8 - bh + L.li * H * .035;
        ctx.fillStyle = rgba(.55 + d * .35, col); ctx.fillRect(bx, by, bw, hz + H - by);
        ctx.strokeStyle = rgba(.16 + d * .24, mix(rgb, WHITE, .2)); ctx.lineWidth = 1.2 * k; ctx.beginPath(); ctx.moveTo(bx, by + bh); ctx.lineTo(bx, by); ctx.lineTo(bx + bw, by); ctx.lineTo(bx + bw, by + bh); ctx.stroke();
        const wc = Math.max(2, Math.floor(bw / (16 * k)));
        for (let yy = by + 12 * k; yy < by + bh - 10 * k; yy += 20 * k) for (let i = 0; i < wc; i++) {
          if (((i * 7 + Math.floor(yy / (20 * k)) * 13 + b.win) % 7) < 1) { ctx.fillStyle = rgba(.10 + d * .2 + flash * .08, i % 3 ? mix(rgb, WHITE, .3) : PEACH); ctx.fillRect(bx + (i + .3) * bw / wc, yy, bw / wc * .4, 6 * k); }
        }
      });
    });
    // пол: сетка в перспективе
    const gy = hz + oy(.2) * .8 + 4;
    const fg = ctx.createLinearGradient(0, gy, 0, H); fg.addColorStop(0, rgba(.0)); fg.addColorStop(1, rgba(.1));
    ctx.fillStyle = fg; ctx.fillRect(0, gy, W, H - gy);
    ctx.strokeStyle = rgba(.14 + energy * .15);
    ctx.lineWidth = 1;
    for (let i = -12; i <= 12; i++) { ctx.beginPath(); ctx.moveTo(W / 2 + i * 26 * k, gy); ctx.lineTo(W / 2 + i * W * .17, H); ctx.stroke(); }
    for (let j = 0; j < 9; j++) { const kk = (j / 9 + flowShown * .0004) % 1, y = gy + Math.pow(kk, 2.2) * (H - gy); ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke(); }
  }

  const draw = {notes: drawNotes, stars: drawStars, waves: drawWaves, bokeh: drawBokeh, eq: drawEq, city: drawCity};
  let lastNow = performance.now();
  function loop(now){
    const dt = Math.min(.1, (now - lastNow) / 1000); lastNow = now;
    const t = reduce ? 0 : (now - t0) / 1000;
    ctx.clearRect(0, 0, W, H);
    // Фон догоняет шарик с небольшой задержкой: это и даёт ощущение глубины и плавности
    const a = 1 - Math.exp(-dt / .35);
    pitch += (pitchTarget - pitch) * a;
    flowShown += (flow - flowShown) * (1 - Math.exp(-dt / .25));
    try { draw[variant](t); } catch (e) { console.error(e); }
    ctx.fillStyle = 'rgba(10,3,8,.34)'; ctx.fillRect(0, 0, W, H);      // фон чуть темнее, чтобы стены и ноты читались
    flash += (flashTarget - flash) * .07; flashTarget *= .975;
    energy += (energyTarget - energy) * .12;
    requestAnimationFrame(loop);
  }
  requestAnimationFrame(loop);
  return {
    pulse(){ flashTarget = 1; },
    level(v){ energyTarget = clamp01(v); },
    recolor: readColor,
    setPitch(n){ pitchTarget = clamp01(n); },       // 0 — внизу шкалы, 1 — вверху
    setFlow(px){ flow = px; },                       // сколько пикселей проехали барьеры
    next(){ variant = VARIANTS[(VARIANTS.indexOf(variant) + 1) % VARIANTS.length]; return variant; },
    get variant(){ return variant; },
    set variant(v){ if (VARIANTS.includes(v)) variant = v; },
    VARIANTS,
  };
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
<html lang="ru" data-theme="women" data-base="{{ base }}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#140710"><title>Точно в ноту · пульт</title>
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
.btn{display:block;width:100%;border:0;border-radius:var(--r-m);padding:18px;font-size:19px;font-weight:800}
.btn.primary{background:var(--signal);color:var(--signal-ink)}
.btn.quiet{background:transparent;border:1px solid var(--line);color:var(--chalk);font-weight:600}
.btn.danger{background:transparent;border:1px solid var(--danger-bg);color:var(--danger);font-weight:600;font-size:15px;padding:14px}
.btn:disabled{opacity:.4;cursor:default}
.btn.armed{background:var(--warm);border-color:var(--warm);color:var(--signal-ink)}
.btn.quiet.armed{color:var(--signal-ink)}
.resetzone{margin-top:30px;padding-top:20px;border-top:1px dashed var(--line);text-align:center}
.reset-open{background:none;border:0;color:var(--mist);opacity:.75;font-size:14px;font-weight:600;padding:10px 14px;text-decoration:underline;text-underline-offset:3px}
.reset-ask{text-align:left}
.reset-ask p{margin:0 0 14px;color:var(--chalk);line-height:1.4}
.reset-ask p span{display:block;color:var(--mist);font-size:14px;margin-top:4px}
.reset-ask .two{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.reset-ask .btn{padding:15px 8px;font-size:16px}
.btn:active:not(:disabled){transform:scale(.98)}
.who{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.who .name{font-family:var(--display);font-size:26px;font-weight:800}
.who .of{color:var(--mist);font-weight:600;white-space:nowrap}
.mic{display:flex;align-items:center;gap:.6em;margin:12px 0 0;font-size:14px;font-weight:600;color:var(--mist)}
.mic i{flex:none;width:.7em;height:.7em;border-radius:50%;background:var(--danger)}
.mic.ok i{background:var(--signal);box-shadow:0 0 10px var(--signal)}
.mic.bad{color:var(--danger)}
.status{display:flex;justify-content:space-between;align-items:center;margin:14px 0 12px;padding:14px 16px;border-radius:var(--r-m);background:var(--ink-2)}
.status .label{color:var(--mist);font-weight:600}
.status .time{font-size:30px;}
.status.hot .time{color:var(--signal)}
.status.end .time{color:var(--chalk)}
.dots{display:flex;gap:7px;justify-content:center;margin:0 0 14px}
.dots i{flex:1;max-width:34px;height:12px;border-radius:6px;background:var(--ink-2);border:1px solid var(--line)}
.dots i.ok{background:var(--signal);border-color:var(--signal)}
.dots i.no{background:var(--danger-bg);border-color:var(--danger)}
.score{text-align:center;padding:2px 0 14px}
.score .num{font-size:96px;line-height:1;color:var(--signal)}
.score .unit{color:var(--mist);font-weight:600;margin-top:4px}
.stack{display:flex;flex-direction:column;gap:10px}
.rows{display:flex;flex-direction:column}
.row{display:flex;justify-content:space-between;padding:12px 2px;border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:0}
.row b{font-family:var(--digits);font-weight:var(--digits-w);color:var(--signal)}
.row b small{font-family:var(--ui);color:var(--mist);font-weight:600;margin-left:6px}
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
  <div class="top"><span class="wordmark"><i></i>Точно в ноту</span>
    <span class="links"><a class="link" href="{{ base }}screen" target="_blank" rel="noopener">Экран для гостей</a><a class="link" href="{{ base }}setup" target="_blank" rel="noopener">Setup</a></span></div>

  <section class="card" id="setup">
    <h2>Сколько участников</h2>
    <div class="stepper"><button id="minusCount" aria-label="Меньше">−</button><div class="num" id="count">4</div><button id="plusCount" aria-label="Больше">+</button></div>
    <button class="btn primary" id="begin">Начать конкурс</button>
    <p class="hint" style="margin:12px 0 0">У каждого участника {{ round }} секунд: стены с нотами, очко за каждую пройденную</p>
  </section>

  <section class="card" id="game" hidden>
    <div class="who"><span class="name" id="name"></span><span class="of" id="of"></span></div>
    <div class="mic" id="mic"><i></i><span id="micText"></span></div>
    <div class="status" id="status"><span class="label" id="statusLabel"></span><span class="time num" id="time"></span></div>
    <div class="score" id="scoreBox" hidden><div class="num" id="score">0</div><div class="unit" id="unit">очков</div></div>
    <div id="actions"></div>
  </section>

  <section class="card" id="final" hidden>
    <div class="who"><span class="name">Конкурс завершён</span></div>
    <p class="hint" style="text-align:left;margin:8px 0 0">Итоги уже на экране для гостей.</p>
  </section>

  <section class="card" id="results" hidden><h2 id="resultsTitle">Уже спели</h2><div class="rows" id="rows"></div></section>

  <section class="card" id="names" hidden>
    <h2>Имена участников</h2>
    <div class="fields" id="nameFields"></div>
    <p class="hint" style="text-align:left;margin:10px 0 0">Пустое поле — снова «Участник N». Имя сохраняется сразу.</p>
  </section>

  <div class="spacer"></div>
  <button class="namelink" id="namesToggle" hidden>Имена участников</button>
  <div class="resetzone" id="resetZone" hidden>
    <button class="reset-open" id="resetOpen"></button>
    <div class="reset-ask" id="resetAsk" hidden>
      <p><b id="resetQ"></b><span id="resetSub"></span></p>
      <div class="two"><button class="btn quiet" id="resetNo">Отмена</button><button class="btn danger" id="resetYes" disabled></button></div>
    </div>
  </div>
</main>
<script>{{ client_js|safe }}</script>
<script>
const $ = id => document.getElementById(id);
let count = 4, lastKey = '';
function setText(el, v){ v = String(v); if (el._v !== v) { el._v = v; el.textContent = v; } }   // трогаем DOM только при изменении
function setHtml(el, h){ if (el._h !== h) { el._h = h; el.innerHTML = h; } }

/* ---------- связь ----------
   Все команды уходят через send(): если связи нет, команда не копится в очереди
   (иначе «Запустить время» выстрелит позже, когда никто не ждёт), а сверху видна красная полоса.
   После каждой команды просим свежее состояние; если ответа нет — тихо переподключаемся. */
let pendingAt = 0, lastState = performance.now();
socket.on('state', () => { pendingAt = 0; lastState = performance.now(); });
function reconnect(){
  pendingAt = 0; lastState = performance.now();
  offlineBar && offlineBar.classList.add('on');
  try { socket.disconnect(); socket.connect(); } catch (e) {}
}
function send(name, data){
  if (!socket.connected) { offlineBar && offlineBar.classList.add('on'); return false; }
  socket.emit(name, data || {});
  socket.emit('sync');
  if (!pendingAt) pendingAt = performance.now();
  return true;
}
setInterval(() => {
  const now = performance.now();
  if (pendingAt && now - pendingAt > 3500) return reconnect();     // команда ушла, а ответа нет
  if (document.hidden) return;
  if (socket.connected) socket.emit('sync');                          // живой пульс: страница никогда не «застывает» молча
  if (now - lastState > 12000) reconnect();
}, 2000);

/* ---------- подтверждения без системных окон ----------
   Системные confirm() телефоны и встроенные браузеры могут молча блокировать — тогда кнопка «не реагирует».
   Вместо них кнопка меняет подпись и ждёт второго нажатия. */
function disarm(btn){ if (!btn || !btn._armed) return; btn._armed = false; clearTimeout(btn._armT); btn.textContent = btn._orig; btn.classList.remove('armed'); }
function arm(btn, askText, run){
  if (btn._armed) { disarm(btn); run(); return; }
  btn._armed = true; btn._orig = btn.textContent; btn.textContent = askText; btn.classList.add('armed');
  btn._armT = setTimeout(() => disarm(btn), 4000);
}

$('minusCount').onclick = () => { count = Math.max(1, count - 1); setText($('count'), count); };
$('plusCount').onclick = () => { count = Math.min(30, count + 1); setText($('count'), count); };
$('begin').onclick = () => send('setup', {count});

let namesOpen = false, namesKey = '';
$('namesToggle').onclick = () => { namesOpen = !namesOpen; namesKey = ''; $('names').hidden = !namesOpen; $('namesToggle').textContent = namesOpen ? 'Скрыть имена' : 'Имена участников'; if (namesOpen) $('names').scrollIntoView({behavior: 'smooth', block: 'start'}); };
const isDefaultName = n => /^Участник \d+$/.test(n);
function renderNames(){
  if (!namesOpen || !S) return;
  const key = S.participants.length + ':' + S.current;
  const box = $('nameFields');
  if (key !== namesKey) {   // поля пересобираются только при смене состава, чтобы не мешать вводу
    namesKey = key;
    box.innerHTML = S.participants.map((p, i) => `<label><span class="num">${i + 1}</span><input data-i="${i}" maxlength="32" placeholder="Участник ${i + 1}" value="${esc(isDefaultName(p.name) ? '' : p.name)}"></label>`).join('');
  }
  box.querySelectorAll('input').forEach(inp => {
    if (document.activeElement === inp) return;
    const p = S.participants[+inp.dataset.i]; if (!p) return;
    const v = isDefaultName(p.name) ? '' : p.name;
    if (inp.value !== v) inp.value = v;
  });
}
$('nameFields').addEventListener('change', e => { const inp = e.target.closest('input'); if (inp) send('rename', {index: +inp.dataset.i, name: inp.value}); });
$('nameFields').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); e.target.blur(); } });

/* ---------- сброс игры: внизу, в два шага ---------- */
let resetT = 0, resetUnlockT = 0;
function closeReset(){ clearTimeout(resetT); clearTimeout(resetUnlockT); $('resetAsk').hidden = true; $('resetOpen').hidden = false; $('resetYes').disabled = true; }
$('resetOpen').onclick = () => {
  $('resetOpen').hidden = true; $('resetAsk').hidden = false; $('resetYes').disabled = true;
  resetUnlockT = setTimeout(() => { $('resetYes').disabled = false; }, 700);   // защита от случайного двойного нажатия
  resetT = setTimeout(closeReset, 10000);
  $('resetAsk').scrollIntoView({behavior: 'smooth', block: 'nearest'});
};
$('resetNo').onclick = closeReset;
$('resetYes').onclick = () => { if ($('resetYes').disabled) return; closeReset(); send('reset'); };

function renderActions(ph, isLast){
  // Перерисовываем кнопки только при смене фазы, чтобы нажатие не «съедалось»
  const key = ph + (isLast ? 'L' : '');
  if (key === lastKey) return;
  lastKey = key;
  const a = $('actions');
  if (ph === 'ready') {
    a.innerHTML = `<div class="stack"><button class="btn primary" data-act="start_timer">Запустить время</button>
      <p class="hint">${S.prep} секунд отсчёта, потом ${S.round} секунд игры</p></div>`;
  } else if (ph === 'countdown') {
    a.innerHTML = `<p class="hint">Участник готовится…</p>`;
  } else if (ph === 'play') {
    a.innerHTML = `<p class="hint">Идёт забег. Очко — за каждую стену, в которую попала нота.</p>`;
  } else if (ph === 'timeup') {
    a.innerHTML = `<div class="stack"><button class="btn primary" data-act="next">${isLast ? 'Показать итоги' : 'Следующий участник'}</button>
      <button class="btn quiet" data-act="replay">Переиграть раунд</button></div>`;
  } else a.innerHTML = '';
}
$('actions').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b || b.disabled) return;
  const act = b.dataset.act;
  if (act === 'replay') return arm(b, 'Обнулить результат? Нажмите ещё раз', () => send('replay'));
  // Без микрофона забег пройдёт впустую: просим подтвердить второй раз
  if (act === 'start_timer' && S && !S.mic.on) return arm(b, 'Микрофон не подключён. Запустить всё равно?', () => send(act));
  if (act) send(act);
});

function frame(){
  if (!S) return;
  const ph = phaseOf(S), p = S.participants[S.current];
  const show = (id, on) => { const el = $(id); if (el.hidden === on) el.hidden = !on; };
  show('setup', ph === 'idle');
  show('game', !!(p && !S.finished));
  show('final', ph === 'finished');
  show('resetZone', ph !== 'idle');
  show('namesToggle', ph !== 'idle');
  if (ph === 'idle') { namesOpen = false; $('names').hidden = true; setText($('namesToggle'), 'Имена участников'); closeReset(); }
  const fin = ph === 'finished';
  setText($('resetOpen'), fin ? 'Начать новый конкурс' : 'Сбросить игру');
  setText($('resetQ'), fin ? 'Начать новый конкурс?' : 'Сбросить игру?');
  setText($('resetSub'), fin ? 'Итоги исчезнут, участников придётся задать заново.' : 'Все участники и результаты удалятся.');
  setText($('resetYes'), fin ? 'Да, начать заново' : 'Да, сбросить');
  renderNames();
  if (p && !S.finished) {
    const isLast = S.current === S.participants.length - 1;
    setText($('name'), p.name);
    setText($('of'), (S.current + 1) + ' из ' + S.participants.length);
    const m = $('mic'), micCls = 'mic ' + (S.mic.on ? 'ok' : 'bad');
    if (m.className !== micCls) m.className = micCls;
    setText($('micText'), S.mic.on ? 'Микрофон слышно' + (S.mic.label ? ': ' + S.mic.label : '') : 'Микрофон не подключён. Откройте экран для гостей и разрешите доступ.');
    show('scoreBox', ph === 'play' || ph === 'timeup');
    if (ph === 'play' || ph === 'timeup') {
      setRoll($('score'), p.score);
      setText($('unit'), 'стен пройдено');
    }
    const st = $('status'), r = remaining(S);
    const sc = 'status' + (ph === 'play' ? ' hot' : ph === 'timeup' ? ' end' : '');
    if (st.className !== sc) st.className = sc;
    setText($('statusLabel'), {ready:'Ждём старта', countdown:'Отсчёт', play:'Идёт забег', timeup:'Время вышло'}[ph]);
    setRoll($('time'), ph === 'countdown' ? String(Math.ceil(r)) : ph === 'timeup' ? '0:00' : fmtTime(r), {up: false});
    renderActions(ph, isLast);
  } else renderActions(ph, false);
  const done = ranking(S);
  show('results', done.length > 0);
  setText($('resultsTitle'), fin ? 'Итоги' : 'Уже спели');
  setHtml($('rows'), done.map((q, i) => `<div class="row${i === 0 ? ' first' : ''}"><span>${esc(q.name)}</span><b>${q.score}</b></div>`).join(''));
}
// Одна ошибка в отрисовке не должна навсегда останавливать страницу
function tick(){ try { frame(); } catch (e) { console.error(e); } requestAnimationFrame(tick); }
requestAnimationFrame(tick);
</script></body></html>"""

# ======================================================================
# Экран для гостей
# ======================================================================
SCREEN_HTML = r"""<!doctype html>
<html lang="ru" data-theme="women" data-base="{{ base }}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Точно в ноту</title>
""" + FONTS + r"""
<style>{{ theme_css|safe }}
html,body{height:100%;overflow:hidden}
body{background:radial-gradient(60% 70% at 50% 55%,var(--signal-soft),transparent 70%),radial-gradient(90% 80% at 0 0,var(--ink-2),var(--ink) 65%)}
.screen{height:100vh;display:grid;grid-template-rows:auto 1fr;padding:3.2vh 4.5vw 6.5vh}
.head{display:flex;justify-content:space-between;align-items:center}
.head .wordmark{font-size:clamp(20px,2vw,34px)}
.head .round{color:var(--mist);font-weight:600;font-size:clamp(16px,1.4vw,24px)}
.stage{position:relative;display:flex;min-height:0}
.view{flex:1;min-width:0;min-height:0}
/* --- игра: поле на весь экран, сверху плашка с именем и счётом --- */
body[data-view=game] .head{display:none}
.play{position:fixed;inset:0;z-index:2}
.hud{position:absolute;top:2.4vh;left:50%;transform:translateX(-50%);z-index:5;display:flex;align-items:center;gap:1.8vw;max-width:94vw;padding:.9vh 1.8vw;border-radius:999px;
  background:rgba(20,7,16,.74);border:1px solid var(--line);backdrop-filter:blur(8px);box-shadow:0 8px 40px rgba(0,0,0,.35)}
.who{font-family:var(--display);font-weight:800;font-size:clamp(18px,1.9vw,36px);line-height:1.15;min-width:0;max-width:26vw;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hud .sep{flex:none;width:1px;align-self:stretch;margin:.5vh 0;background:var(--line)}
.rnd{flex:none;color:var(--mist);font-weight:600;font-size:clamp(13px,1.1vw,20px);white-space:nowrap}
.brand{position:absolute;top:2.6vh;left:2.2vw;z-index:6;font-size:clamp(16px,1.7vw,30px)}
.tm{flex:none;font-family:var(--display);font-weight:800;font-size:clamp(16px,1.6vw,30px);color:var(--chalk);font-variant-numeric:tabular-nums;min-width:2.6em;text-align:center}
.tm.low{color:var(--danger)}
.scorebox{flex:none;display:flex;align-items:baseline;gap:.35em}
.scorebox .num{font-size:clamp(26px,2.9vw,56px);line-height:1;color:var(--signal);filter:drop-shadow(0 0 18px var(--signal-soft));--roll-ms:700ms}
.scorebox small{color:var(--mist);font-weight:600;font-size:clamp(12px,1.05vw,20px)}
@media (max-width:900px){.rnd,.brand{display:none}.who{max-width:40vw}}
.course{position:absolute;inset:0}
.course canvas{position:absolute;inset:0;width:100%;height:100%;display:block}
.ov{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;pointer-events:none}
.ov .sub{color:var(--chalk);font-weight:600;font-size:clamp(18px,1.9vw,34px);text-shadow:0 2px 18px var(--ink);background:rgba(20,7,16,.62);padding:.4em 1em;border-radius:999px}
.ov .sub.dim{color:var(--mist);font-size:clamp(15px,1.4vw,26px);margin-top:1vh}
.ov .sub:empty{display:none}
.countnum{font-size:min(20vw,44vh);line-height:1;color:var(--chalk);filter:drop-shadow(0 0 40px var(--signal-soft));--roll-ms:520ms}
.resbig{font-size:min(10vw,19vh);line-height:1;color:var(--signal);filter:drop-shadow(0 0 40px var(--signal-soft));--roll-ms:1300ms}
.ov.res{background:rgba(20,7,16,.82);justify-content:flex-start;padding-top:12vh}
.ov .msg{font-family:var(--display);font-weight:900;font-size:clamp(28px,3.4vw,64px);line-height:1;margin-bottom:1.2vh}
.ov.res .sub{background:none;padding:0;margin-top:1vh;color:var(--mist)}
/* таблица показывается только между участниками: итог раунда + текущий рейтинг */
.rank{width:min(640px,88vw);margin-top:3vh;min-height:0}
.rank-h{color:var(--mist);font-weight:600;font-size:clamp(15px,1.3vw,24px);margin:0 0 1.2vh .2em;text-align:left}
.side-list{position:relative;--rh:clamp(38px,6vh,60px)}
.srow{position:absolute;left:0;right:0;top:0;height:var(--rh);display:grid;grid-template-columns:1.6em minmax(0,1fr) auto auto;align-items:center;gap:.6em;padding:0 .9em;border-radius:var(--r-m);text-align:left;
  background:rgba(38,16,31,.9);border:1px solid var(--line);font-size:clamp(14px,calc(var(--rh) * .4),26px);
  transition:transform .7s cubic-bezier(.2,.8,.2,1),opacity .4s,border-color .3s,background .3s}
.srow .pl{color:var(--mist)}
.srow .pn{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:flex;align-items:center;gap:.5em}
.srow .pg{color:var(--mist);font-size:.62em;font-weight:600;white-space:nowrap}
.srow .sc{color:var(--chalk);--roll-ms:600ms}
.srow.lead .sc{color:var(--signal)}
.srow.now{border-color:var(--signal);background:rgba(255,79,168,.14)}
.srow.gone{opacity:0}
/* кнопка «включить звук и микрофон» и состояние микрофона */
.sound{position:fixed;right:20px;bottom:20px;z-index:50;border:1px solid var(--line);background:rgba(38,16,31,.92);color:var(--chalk);border-radius:999px;padding:12px 20px;font-weight:600;font-size:16px;cursor:pointer;transition:opacity .4s}
.sound:hover{border-color:var(--signal)}
.sound.off{opacity:0;pointer-events:none}
.micchip{position:fixed;left:20px;bottom:16px;z-index:50;max-width:min(60vw,560px);display:flex;align-items:center;gap:.6em;border:0;background:none;color:var(--mist);font:600 clamp(12px,1vw,16px)/1.2 var(--ui);padding:6px 4px;opacity:.55;transition:opacity .3s;text-decoration:none}
.micchip:hover{opacity:1}
.micchip i{flex:none;width:.7em;height:.7em;border-radius:50%;background:var(--mist)}
.micchip span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.micchip.ok i{background:var(--signal);box-shadow:0 0 10px var(--signal)}
.micchip.bad{opacity:1;color:var(--danger)}
.micchip.bad i{background:var(--danger)}
.toast{position:fixed;left:50%;bottom:6vh;transform:translateX(-50%);z-index:60;padding:10px 22px;border-radius:999px;background:rgba(38,16,31,.92);border:1px solid var(--line);font-weight:600;font-size:18px;opacity:0;transition:opacity .3s;pointer-events:none}
.toast.on{opacity:1}
/* --- полноэкранные состояния --- */
.full{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;min-height:0}
.full .big{font-family:var(--display);font-weight:900;font-size:clamp(56px,8vw,160px);line-height:.95;letter-spacing:-.02em}
.full .sub{color:var(--mist);font-weight:600;font-size:clamp(20px,2vw,36px);margin-top:3vh}
/* --- итоги: таблица, подстраивается под число участников --- */
.final{display:flex;flex-direction:column;min-height:0}
.final h1{font-family:var(--display);font-weight:900;font-size:clamp(40px,4.6vw,88px);margin:1.5vh 0 2.5vh;line-height:1}
.board{flex:1;min-height:0;display:grid;grid-auto-flow:column;grid-template-rows:repeat(var(--rows),auto);grid-template-columns:repeat(var(--cols),minmax(0,1fr));column-gap:3vw;align-content:start}
.board{--rh:calc((100vh - 3.2vh*2 - 3vw - 18vh) / var(--rows))}
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
  <div class="head"><span class="wordmark"><i></i>Точно в ноту</span><span class="round" id="round"></span></div>

  <div class="stage" id="stage">
    <div class="view full" id="idle"><div class="big">Точно в ноту</div><div class="sub">Скоро начнём</div></div>

    <div class="view play" id="game" hidden>
      <div class="course" id="courseBox">
        <canvas id="course" aria-label="Трасса: стены с просветами на высоте нужных нот"></canvas>
        <div class="ov" id="ovReady" hidden><div class="sub">Спойте любую ноту: шарик покажет высоту вашего голоса</div><div class="sub dim">Октава не важна. Пока вы молчите, шарик лежит на полу</div></div>
        <div class="ov" id="ovCd" hidden><div class="sub" id="cdName"></div><div class="countnum num" id="cdNum"></div></div>
        <div class="ov res" id="ovRes" hidden><div class="msg">Готово!</div><div class="resbig num" id="resNum"></div><div class="sub" id="resSub"></div>
          <div class="rank"><div class="rank-h">Результаты</div><div class="side-list" id="sideList"></div></div></div>
      </div>
      <div class="brand wordmark"><i></i>Точно в ноту</div>
      <div class="hud">
        <span class="who" id="who"></span><span class="rnd" id="roundHud"></span><i class="sep"></i>
        <span class="tm" id="tm">1:00</span><i class="sep"></i>
        <span class="scorebox"><span class="num" id="score">0</span><small>стен</small></span>
      </div>
    </div>

    <div class="view final" id="final" hidden><h1>Итоги</h1><div class="board" id="board"></div></div>
  </div>
</div>
<button class="sound" id="soundBtn">Включить звук и микрофон</button>
<a class="micchip" id="micChip" href="{{ base }}setup" target="_blank" rel="noopener" title="Открыть Setup"><i></i><span id="micText">Микрофон: подключаю…</span></a>
<div class="toast" id="toast"></div>
<script>{{ client_js|safe }}</script>
<script>{{ audio_js|safe }}</script>
<script>{{ bg_js|safe }}</script>
<script>
const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.search);
const micEnabled = params.get('mic') !== '0';     // ?mic=0 — второй экран только показывает, микрофон не слушает
let lastView = '', lastFinalKey = '', resFor = -1, lastPh = null, lastCd = null, lastLocal = -1e9;

/* ---------- Шкала высоты: окно в 12 полутонов от «до», без привязки к октаве ---------- */
const NOTE_RU = {0: 'Do', 2: 'Re', 4: 'Mi', 5: 'Fa', 7: 'Sol', 9: 'La'};     // подписи на экране — слоги латиницей
const GRID = [['Do', 0], ['Re', 2], ['Mi', 4], ['Fa', 5], ['Sol', 7], ['La', 9], ['Si', 11]];   // линии шкалы: семь нот через равные промежутки
const LOP = -.5, HIP = 6.5, LOOK = 4.2;         // видимое окно в «ступенях»; LOOK — за сколько секунд стена доезжает от правого края до шарика
const foldPos = v => { const p = posOf(mod12(v)); return p >= HIP ? p - 7 : p; };

/* ---------- Шарик: сглаженная высота голоса ---------- */
const P = {disp: 0, vis: 0, stable: false, has: false};
function currentPitch(now){
  if (micEnabled && Mic.running && now - lastLocal < 400) { const f = Mic.frame; return f && f.voiced ? {pc: f.pc, stable: f.stable} : null; }   // свой микрофон: без задержки сети
  if (now - LIVE.at < 700 && LIVE.m != null) return {pc: LIVE.m, stable: !!LIVE.s};                                                    // высота от другого экрана
  return null;
}
function updatePitch(now, dt){
  const p = currentPitch(now);
  if (p) {
    const d = circ12(p.pc - P.disp);
    if (!P.has || P.vis < .2 || Math.abs(d) > 4) P.disp = p.pc; else P.disp = mod12(P.disp + d * (1 - Math.exp(-dt / .045)));
    P.has = true; P.stable = p.stable; P.vis += (1 - P.vis) * (1 - Math.exp(-dt / .06));
  } else { P.stable = false; P.vis += (0 - P.vis) * (1 - Math.exp(-dt / .25)); }
}

/* ---------- Микрофон ---------- */
let lastSent = 0, wasRunning = false, holdLocal = 0, holdGate = -1, holdAt = 0, holdKey = '';
Mic.onFrame = f => {
  if (Mic.suspended) return;       // браузер приостановил звук: данные не отправляем, ведущий увидит «нет сигнала»
  const now = performance.now();
  const dt = Math.min(.12, (now - lastLocal) / 1000); lastLocal = now;
  // Полоска «держу ноту» считается здесь же, по тем же правилам, что и на сервере, чтобы не ждать сети
  const att = attemptNow(), gi = att ? att.i : -1, key = S ? S.started_at + ':' + S.current + ':' + (att ? att.i + '/' + att.k : '') : '';
  if (gi !== holdGate || key !== holdKey) { holdGate = gi; holdKey = key; holdLocal = 0; }
  if (gi >= 0 && f.voiced && f.stable && Math.abs(pdist(f.pc, S.seq[gi])) <= S.game.tol) holdLocal += dt;
  Bg.level(Math.max(0, Math.min(1, (f.dbfs + 60) / 40)));
  if (now - lastSent >= 48) {      // 20 раз в секунду
    lastSent = now;
    if (socket.connected) socket.emit('pitch', {m: f.voiced ? Math.round(f.pc * 100) / 100 : null, s: f.voiced && f.stable ? 1 : 0});
  }
};
function claim(){ if (micEnabled && Mic.running && socket.connected) socket.emit('mic_claim', {label: Mic.label}); }
socket.on('connect', () => { socket.emit('watch'); claim(); });   // watch — подписка на живую высоту тона
if (socket.connected) socket.emit('watch');
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
addEventListener('storage', e => { if (e.key === NT_KEY && micEnabled) { Mic.reload(); Mic.start(); } });   // сменили вход в Setup — подхватываем сразу
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
    count(d = 0){ tone(660, .16, {gain: .35, at: d}); },                                   // отсчёт 5…1, до начала замера
    // Перезвон после забега: мажорное арпеджио. Во время забега экран молчит: всё, что прозвучит, попало бы в микрофон.
    chime(d = 0){ [523, 659, 784, 1047].forEach((f, i) => tone(f, 1.8 - i * .2, {gain: .24 - i * .03, attack: .01, at: d + i * .11})); },
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

/* Прокрутка цифры: новая цифра встаёт на место примерно на 40% длительности. Звук ставим ровно на этот момент. */
const LAND = .4;
function landDelay(el){ const ms = parseFloat(getComputedStyle(el).getPropertyValue('--roll-ms')) || 560; return ms / 1000 * LAND; }
function sounds(ph){
  if (ph === 'countdown') {
    const n = Math.ceil(remaining(S));
    if (n !== lastCd) { lastCd = n; if (lastPh) Snd.count(landDelay($('cdNum'))); }
  } else lastCd = null;
  if (ph === 'timeup' && lastPh === 'play') { Snd.chime(.3); socket.emit('sync'); }   // sync — на случай, если итог ещё в пути
  lastPh = ph;
}

/* ---------- Фон: B — сменить вариант ---------- */
let toastT = 0;
function toast(text){ const t = $('toast'); t.textContent = text; t.classList.add('on'); clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove('on'), 1600); }
const BG_NAMES = {notes: 'Фон: ноты и нотный стан', stars: 'Фон: звёздное небо', waves: 'Фон: гряды из звуковых волн', bokeh: 'Фон: огоньки и сердечки', eq: 'Фон: эквалайзер', city: 'Фон: неоновый город'};
addEventListener('keydown', e => { if (!e.repeat && 'bBиИ'.includes(e.key) && e.key.length === 1) toast(BG_NAMES[Bg.next()]); });
addEventListener('keydown', e => { if (!e.repeat && 'nNтТ'.includes(e.key) && e.key.length === 1) toast(BALL_NAMES[Ball.next()]); });

/* ---------- Трасса на canvas ---------- */
const cv = $('course'), cx = cv.getContext('2d');
const G = {W: 0, H: 0, dpr: 1, padL: 0, padR: 0, yF: 0, markerX: 0, speed: 1, yT: 0, yB: 0, r: 16};
function resizeCourse(){
  const b = $('courseBox').getBoundingClientRect();
  if (b.width < 10 || b.height < 10) return;
  G.dpr = Math.min(2, devicePixelRatio || 1); G.W = b.width; G.H = b.height;
  cv.width = Math.round(G.W * G.dpr); cv.height = Math.round(G.H * G.dpr); cx.setTransform(G.dpr, 0, 0, G.dpr, 0, 0);
  G.padL = Math.max(36, G.W * .03); G.padR = Math.max(120, G.W * .09); G.markerX = G.padL + (G.W - G.padR - G.padL) * .2; G.speed = (G.W - G.padR - G.markerX) / LOOK;
  G.yT = G.H * .17; G.yB = G.H * .80; G.yF = G.H * .915; G.r = Math.max(13, G.H * .03);
}
if (window.ResizeObserver) new ResizeObserver(resizeCourse).observe($('courseBox'));
addEventListener('resize', resizeCourse);
const yOf = p => G.yT + (HIP - p) / (HIP - LOP) * (G.yB - G.yT);       // p — позиция на шкале
const yV = v => yOf(posOf(v));                                         // v — полутоны
function rr(c, x, y, w, h, r){ c.beginPath(); if (c.roundRect) c.roundRect(x, y, w, h, r); else c.rect(x, y, w, h); }
let PINK = [255, 79, 168];
function readPink(){ const m = getComputedStyle(document.documentElement).getPropertyValue('--signal').trim().match(/^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i); if (m) PINK = m.slice(1).map(h => parseInt(h, 16)); }
readPink();
const col = (a, c) => { c = c || PINK; return `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${Math.max(0, Math.min(1, a))})`; };
const LIGHT = [255, 220, 238], RED = [255, 142, 156];

const fx = [], floats = [], gateFx = new Map();
function burst(x, y, n){
  for (let i = 0; i < n; i++) { const a = Math.random() * 6.28, v = 80 + Math.random() * 260; fx.push({x, y, vx: Math.cos(a) * v, vy: Math.sin(a) * v - 60, life: 0, max: .6 + Math.random() * .6, s: 2 + Math.random() * 4}); }
}
const WALL_FADE = .9;
const PRED = {key: ''}, BUMP = {at: -1e9, key: ''};
function attemptNow(){ // окно попытки пройти стену: {i: номер стены, k: номер попытки} или null
  if (!S || phaseOf(S) !== 'play') return null;
  const g = S.game, p = S.participants[S.current], r = rawTime(S);
  if (!p || r == null) return null;
  const u = r - (S.blocked || 0) - wallAt(g, p.score);
  if (u < -g.pre) return null;
  const k = Math.floor((u + g.pre) / g.retry), ph = u - k * g.retry;
  return ph <= g.post ? {i: p.score, k} : null;
}
function gateNow(){ const a = attemptNow(); return a ? a.i : -1; }
function worldState(p, g){  // откат мира назад после неудачной попытки
  const r = rawTime(S);
  if (r == null || phaseOf(S) !== 'play') return {off: 0, fail: false, key: ''};
  const u = r - (S.blocked || 0) - wallAt(g, p.score);
  if (u < 0) return {off: 0, fail: false, key: ''};
  const k = Math.floor(u / g.retry), ph = u - k * g.retry, key = S.started_at + ':' + S.current + ':' + p.score + ':' + k;
  const hold = Math.max(holdLocal, LIVE.i === p.score && performance.now() - LIVE.at < 700 ? LIVE.h : 0);
  if (hold >= g.need) PRED.key = key;                                    // сами видим, что проходим: ждём подтверждения сервера
  if (ph < g.post || (PRED.key === key && ph < .5)) return {off: 0, fail: false, key};
  const t = ph - g.post;      // отброс назад, затем возврат
  const t2 = t - g.knock, v = G.speed;
  const off = t < g.knock ? G.D * (1 - Math.pow(1 - t / g.knock, 3))                          // отброс: быстро, потом всё медленнее
    : Math.max(0, t2 < g.ramp ? G.D - v * t2 * t2 / (2 * g.ramp)                              // возврат: с нуля, разгон до обычной скорости
      : G.D - v * g.ramp / 2 - v * (t2 - g.ramp));
  return {off, fail: true, key};
}
function drawGate(now, i, p, ct, g, sc, off){
  const t = wallAt(g, i), target = S.seq[i], r = G.r;
  const sw = Math.max(46, G.W * .045);                                   // толщина стены
  const xL = G.markerX + r + (t - ct) * G.speed + off;
  if (xL + sw < G.padL - 20 || xL > G.W + 20) return;
  const st = i < sc ? 'pass' : (i === sc && ct >= t - 1.5) ? 'active' : 'pending';
  const key = S.started_at + ':' + S.current + ':' + i;
  let fxs = gateFx.get(key);
  if (!fxs) { fxs = {st, at: now}; gateFx.set(key, fxs); if (st === 'pass') fxs.at = -1e9; }   // уже пройденные при открытии страницы — без вспышек
  if (fxs.st !== st) {
    fxs.st = st; fxs.at = now;
    if (st === 'pass') { burst(G.markerX, B.y, 26); floats.push({x: G.markerX + 30, y: B.y - 28, text: '+1', t0: now}); Bg.pulse(); }
  }
  const pad = r * .9;                                                    // щель с запасом на размер шарика
  const yC = yV(target), half = g.tol * (G.yB - G.yT) / (HIP - LOP) + pad, yT = yC - half, yB = yC + half;   // щели одной высоты у всех нот
  const age = (now - fxs.at) / 1000, bt = i === sc ? (now - BUMP.at) / 1000 : 9;
  let a = 1, shift = 0, base = PINK, glow = 0;
  if (st === 'pass') { a = Math.max(0, 1 - age / WALL_FADE); shift = (1 - a) * G.H * .06; glow = a; }
  else if (st === 'active') glow = .7;
  if (bt < .45) { glow = Math.max(glow, 1 - bt / .45); base = RED; }
  if (a <= .01) return;
  const xW = xL + (bt < .4 ? Math.sin(bt * 70) * 8 * (1 - bt / .4) : 0);
  const gr = cx.createLinearGradient(0, 0, 0, G.H);
  gr.addColorStop(0, col(.45 * a, base)); gr.addColorStop(Math.max(0, yT / G.H - .02), col(.9 * a, base));
  gr.addColorStop(Math.min(1, yB / G.H + .02), col(.9 * a, base)); gr.addColorStop(1, col(.55 * a, base));
  cx.save();
  if (glow) { cx.shadowColor = col(.9 * glow, base); cx.shadowBlur = 24 * glow; }
  cx.fillStyle = gr; cx.strokeStyle = col((st === 'pending' ? .65 : .95) * a, mixc(base, LIGHT, .35)); cx.lineWidth = st === 'pending' ? 2 : 3;
  rr(cx, xW, -40 - shift, sw, yT + 40, 10); cx.fill(); cx.stroke();
  rr(cx, xW, yB + shift, sw, G.H - yB + 40, 10); cx.fill(); cx.stroke();
  cx.restore();
  // название ноты на стене, над щелью
  const fs = Math.max(16, Math.min(G.H * .05, 40));
  cx.font = `800 ${fs}px Unbounded, Onest, sans-serif`; cx.textAlign = 'center'; cx.textBaseline = 'middle';
  cx.fillStyle = col(.98 * a, [255, 255, 255]);
  cx.fillText(NOTE_RU[target] || '', xW + sw / 2, yT - fs * .85 < G.H * .1 ? yB + fs * .85 : yT - fs * .85);
}
function mixc(c, d, f){ return [c[0] + (d[0] - c[0]) * f, c[1] + (d[1] - c[1]) * f, c[2] + (d[2] - c[2]) * f]; }
/* ---------- Варианты шарика (клавиша N) ---------- */
const BALLS = ['pearl', 'comet', 'heart', 'star', 'note', 'ring'];
const BALL_NAMES = {pearl: 'Шарик: жемчужина', comet: 'Шарик: комета', heart: 'Шарик: сердечко', star: 'Шарик: звезда', note: 'Шарик: нота', ring: 'Шарик: кольцо'};
const Ball = {variant: BALLS.includes(params.get('ball')) ? params.get('ball') : 'pearl', next(){ this.variant = BALLS[(BALLS.indexOf(this.variant) + 1) % BALLS.length]; return this.variant; }};
function glow(x, y, r, a){ const gl = cx.createRadialGradient(x, y, 0, x, y, r); gl.addColorStop(0, col(a)); gl.addColorStop(1, col(0)); cx.fillStyle = gl; cx.beginPath(); cx.arc(x, y, r, 0, 6.3); cx.fill(); }
function heartPath(x, y, r){ cx.beginPath(); cx.moveTo(x, y + r * .95); cx.bezierCurveTo(x - r * 1.5, y - r * .1, x - r * .95, y - r * 1.15, x, y - r * .45); cx.bezierCurveTo(x + r * .95, y - r * 1.15, x + r * 1.5, y - r * .1, x, y + r * .95); cx.closePath(); }
function starPath(x, y, r, rot){ cx.beginPath(); for (let k = 0; k < 10; k++) { const rad = k % 2 ? r * .45 : r * 1.25, a = rot + k * Math.PI / 5 - Math.PI / 2; cx[k ? 'lineTo' : 'moveTo'](x + Math.cos(a) * rad, y + Math.sin(a) * rad); } cx.closePath(); }
function drawBall(x, y, r, vis, near, now){
  const v = Ball.variant, lvl = near ? 1 : 0;
  glow(x, y, r * (v === 'ring' ? 2.6 : 3.4), (near ? .75 : .5) * vis);
  const body = (x0, y0, rad) => { const bd = cx.createRadialGradient(x0 - rad * .35, y0 - rad * .4, rad * .1, x0, y0, rad); bd.addColorStop(0, col(vis, [255, 240, 248])); bd.addColorStop(.45, col(vis, PINK)); bd.addColorStop(1, col(vis, mixc(PINK, [120, 10, 70], .5))); return bd; };
  if (v === 'pearl') {
    cx.fillStyle = body(x, y, r); cx.beginPath(); cx.arc(x, y, r, 0, 6.3); cx.fill();
    if (P.stable) { cx.strokeStyle = col(.8 * vis, LIGHT); cx.lineWidth = 2; cx.beginPath(); cx.arc(x, y, r + 5, 0, 6.3); cx.stroke(); }
  } else if (v === 'comet') {
    for (let k = 1; k < trail.length; k++) {                                   // яркий хвост из следа
      const a = trail[k - 1], b = trail[k]; if (Math.abs(b.y - a.y) > G.H * .45 || b.t - a.t > 120) continue;
      const age = (now - b.t) / 1600, w = r * 1.5 * (1 - age);
      cx.strokeStyle = col(.7 * (1 - age), mixc(PINK, LIGHT, .5 * (1 - age))); cx.lineWidth = w;
      cx.beginPath(); cx.moveTo(x - (now - a.t) / 1000 * G.speed, a.y); cx.lineTo(x - (now - b.t) / 1000 * G.speed, b.y); cx.stroke();
    }
    glow(x, y, r * 1.8, vis); cx.fillStyle = col(vis, [255, 245, 250]); cx.beginPath(); cx.arc(x, y, r * .72, 0, 6.3); cx.fill();
    cx.fillStyle = col(vis, PINK); cx.beginPath(); cx.arc(x, y, r * .45, 0, 6.3); cx.fill();
    for (let k = 0; k < 5; k++) { const ph = (now / 420 + k * .2) % 1; cx.fillStyle = col((1 - ph) * .8 * vis, LIGHT); cx.beginPath(); cx.arc(x - ph * r * 3 - r * .6, y + Math.sin(k * 2.4 + now / 200) * r * .8 * ph, r * .13 * (1 - ph), 0, 6.3); cx.fill(); }
  } else if (v === 'heart') {
    const bt = 1 + Math.sin(now / 160) * (P.stable ? .1 : .04);
    heartPath(x, y, r * 1.05 * bt); cx.fillStyle = body(x, y, r * 1.2); cx.fill();
    cx.strokeStyle = col(.9 * vis, LIGHT); cx.lineWidth = 2; cx.stroke();
    cx.fillStyle = col(.7 * vis, [255, 255, 255]); cx.beginPath(); cx.ellipse(x - r * .5, y - r * .35, r * .22, r * .12, -.7, 0, 6.3); cx.fill();
  } else if (v === 'star') {
    const rot = now / 900 * (P.stable ? 2 : 1);
    starPath(x, y, r * .95, rot); cx.fillStyle = body(x, y, r * 1.25); cx.fill(); cx.strokeStyle = col(.95 * vis, LIGHT); cx.lineWidth = 2; cx.lineJoin = 'round'; cx.stroke();
    for (let k = 0; k < 4; k++) { const a = now / 500 + k * 1.57, d = r * (1.7 + .3 * Math.sin(now / 200 + k)); cx.fillStyle = col(.8 * vis, LIGHT); cx.beginPath(); cx.arc(x + Math.cos(a) * d, y + Math.sin(a) * d, r * .1, 0, 6.3); cx.fill(); }
  } else if (v === 'note') {
    const hr = r * .62;
    cx.fillStyle = col(vis, PINK); cx.strokeStyle = col(vis, LIGHT); cx.lineWidth = 2;
    cx.beginPath(); cx.ellipse(x, y + r * .35, hr * 1.2, hr * .85, -.45, 0, 6.3); cx.fill(); cx.stroke();
    cx.lineWidth = Math.max(3, r * .16); cx.lineCap = 'round'; cx.beginPath(); cx.moveTo(x + hr * 1.05, y + r * .15); cx.lineTo(x + hr * 1.05, y - r * 1.5); cx.stroke();
    cx.beginPath(); cx.moveTo(x + hr * 1.05, y - r * 1.5); cx.bezierCurveTo(x + r * 1.9, y - r * 1.1, x + r * 1.7, y - r * .3, x + r * 1.55, y); cx.stroke();
  } else {
    const pr = r * (1 + .12 * Math.sin(now / 140) * (P.stable ? 1 : .3));
    cx.strokeStyle = col(vis, PINK); cx.lineWidth = r * .55; cx.beginPath(); cx.arc(x, y, pr, 0, 6.3); cx.stroke();
    cx.strokeStyle = col(.9 * vis, LIGHT); cx.lineWidth = 2; cx.beginPath(); cx.arc(x, y, pr + r * .3, 0, 6.3); cx.stroke(); cx.beginPath(); cx.arc(x, y, pr - r * .3, 0, 6.3); cx.stroke();
    cx.fillStyle = col(.9 * vis, [255, 255, 255]); cx.beginPath(); cx.arc(x, y, r * .16, 0, 6.3); cx.fill();
  }
}
let lastDraw = performance.now();
const B = {y: 0, vy: 0, init: false};
let lastFailKey = '', lastScore = -1, ctPrev = 0, ctCorr = 0;
const trail = [], labE = [0, 0, 0, 0, 0, 0, 0];
function drawCourse(now){
  if (!G.W) return;
  const dt = Math.min(.1, (now - lastDraw) / 1000); lastDraw = now;
  cx.clearRect(0, 0, G.W, G.H);
  const ph = S ? phaseOf(S) : 'idle', p = S && S.participants[S.current];
  const ct = S ? courseTime(S) : null, g = S && S.game;
  const playing = S && p && !S.finished && (ph === 'countdown' || ph === 'play');
  // шкала нот: семь линий через равные промежутки, подписи справа; поющаяся нота увеличивается и белеет
  cx.textBaseline = 'middle';
  const lblBase = Math.max(16, Math.min(G.H * .04, 30)), singing = P.vis > .3;
  GRID.forEach(([nm, semi], k) => {
    const y = yOf(k), want = singing && Math.abs(pdist(P.disp, semi)) <= .5 ? 1 : 0;
    labE[k] += (want - labE[k]) * (1 - Math.exp(-dt / .07));
    const e = labE[k];
    cx.strokeStyle = col(.2 + .5 * e, LIGHT); cx.lineWidth = 1.5 + 1.5 * e;
    cx.beginPath(); cx.moveTo(0, y); cx.lineTo(G.W - G.padR + 14, y); cx.stroke();
    cx.font = `${e > .5 ? 800 : 700} ${lblBase * (1 + .7 * e)}px Unbounded, Onest, sans-serif`;
    cx.textAlign = 'center'; cx.fillStyle = col(.85 + .15 * e, mixc(mixc(PINK, LIGHT, .5), [255, 255, 255], e));
    if (e > .05) { cx.shadowColor = col(.9 * e, [255, 255, 255]); cx.shadowBlur = 22 * e; }
    cx.fillText(nm, G.W - G.padR / 2 + 6, y); cx.shadowBlur = 0;
  });
  // шарик плывёт за голосом, как в невесомости: мягкая пружина с небольшим затуханием; без голоса медленно опускается вниз
  const floorY = G.yF - G.r, voiced = P.vis > .35;
  if (!B.init) { B.y = floorY; B.init = true; }
  const w0 = voiced ? 9 : 3.2, ytgt = voiced ? yOf(foldPos(P.disp)) : floorY;
  B.vy += (w0 * w0 * (ytgt - B.y) - 2 * .85 * w0 * B.vy) * dt; B.y += B.vy * dt;
  if (B.y > floorY) { B.y = floorY; B.vy = Math.min(0, B.vy); }
  // стены
  let activeGate = -1;
  G.worldOff = 0;
  if (playing && p && g && S.seq.length > p.score + 4) {
    const sc = p.score;
    G.D = g.spacing * G.speed;
    const ws = worldState(p, g); G.worldOff = ws.off;
    if (sc !== lastScore) { ctCorr = lastScore >= 0 && sc === lastScore + 1 && ct != null ? Math.max(-.3, Math.min(0, ctPrev - ct)) : 0; lastScore = sc; }
    ctCorr *= Math.exp(-dt / .15);
    const ctD = ct + ctCorr; ctPrev = ct;
    if (ws.fail && ws.key !== lastFailKey) { lastFailKey = ws.key; BUMP.at = now; burst(G.markerX + G.r, B.y, 22); Bg.pulse(); }
    cx.save(); cx.beginPath(); cx.rect(0, 0, G.W - G.padR + 14, G.H); cx.clip();
    for (let i = Math.max(0, sc - 2); i <= sc + 3; i++) drawGate(now, i, p, ctD, g, sc, ws.off);
    cx.restore();
    activeGate = ct >= wallAt(g, sc) - 1.5 ? sc : -1;
  } else { lastScore = -1; }
  // след голоса
  const yNow = B.y + (voiced ? 0 : Math.sin(performance.now() / 800) * 3);
  trail.push({t: now, y: yNow});
  while (trail.length && now - trail[0].t > 1600) trail.shift();
  cx.lineCap = 'round'; cx.lineJoin = 'round';
  for (let k = 1; k < trail.length; k++) {
    const a = trail[k - 1], b = trail[k];
    if (b.t - a.t > 120) continue;
    const age = (now - b.t) / 1600;
    cx.strokeStyle = col(.55 * (1 - age)); cx.lineWidth = G.r * (1.1 - age) * .9;
    cx.beginPath(); cx.moveTo(G.markerX - (now - a.t) / 1000 * G.speed, a.y); cx.lineTo(G.markerX - (now - b.t) / 1000 * G.speed, b.y); cx.stroke();
  }
  // шарик
  const vis = 1;
  if (playing || ph === 'ready' || ph === 'timeup') {
    let near = false;
    if (activeGate >= 0) near = voiced && Math.abs(pdist(P.disp, S.seq[activeGate])) <= g.tol && P.stable;
    const r = G.r * (1 + (near ? .12 : 0)), x = G.markerX;
    drawBall(x, yNow, r, vis, near, now);
    // название ноты рядом и стрелка «выше / ниже» перед барьером
    if (voiced) { const nm = NOTE_NAMES[Math.round(mod12(P.disp)) % 12];
      cx.font = `700 ${Math.max(14, G.H * .036)}px Onest, sans-serif`; cx.textAlign = 'left'; cx.fillStyle = col(.9 * vis, LIGHT);
      cx.fillText(nm, x + r * 2.1, yNow); }
    const tgtI = activeGate;
    if (tgtI >= 0 && voiced && !near) {
      const d = pdist(S.seq[tgtI], P.disp);
      if (Math.abs(d) > g.tol) {
        const up = d > 0, ay = yNow + (up ? -1 : 1) * (r + 26);
        cx.fillStyle = col(.9, LIGHT); cx.beginPath();
        cx.moveTo(x, ay + (up ? -10 : 10)); cx.lineTo(x - 11, ay + (up ? 6 : -6)); cx.lineTo(x + 11, ay + (up ? 6 : -6)); cx.closePath(); cx.fill();
      }
    }
  }
  // искры и «+очки»
  for (let k = fx.length - 1; k >= 0; k--) {
    const f = fx[k]; f.life += dt; if (f.life > f.max) { fx.splice(k, 1); continue; }
    f.x += f.vx * dt; f.y += f.vy * dt; f.vy += 260 * dt;
    cx.fillStyle = col(1 - f.life / f.max, LIGHT); cx.beginPath(); cx.arc(f.x, f.y, f.s * (1 - f.life / f.max * .5), 0, 6.3); cx.fill();
  }
  for (let k = floats.length - 1; k >= 0; k--) {
    const f = floats[k], age = (now - f.t0) / 1000; if (age > 1.4) { floats.splice(k, 1); continue; }
    cx.font = `900 ${Math.max(22, G.H * .07)}px Nunito, Onest, sans-serif`; cx.textAlign = 'left'; cx.fillStyle = col(1 - age / 1.4, [255, 255, 255]);
    cx.fillText(f.text, f.x, f.y - age * G.H * .12);
  }
}

/* ---------- Результаты справа ---------- */
const sideRows = new Map();
function renderSide(){
  const list = $('sideList');
  const rowH = list.querySelector('.srow') ? list.querySelector('.srow').offsetHeight : Math.max(38, Math.min(innerHeight * .066, 64));
  const step = rowH + 8, maxRows = Math.max(3, Math.floor((innerHeight * .4) / step));
  const ranked = S.participants.map((p, i) => ({...p, i})).filter(p => p.done || p.i === S.current)
    .sort((a, b) => b.score - a.score || passedOf(b) - passedOf(a) || (b.done - a.done) || a.i - b.i);
  let place = 0, prev = null;
  ranked.forEach((p, k) => { const key = p.score + ':' + passedOf(p); if (key !== prev) { place = k + 1; prev = key; } p.place = place; });
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
      el.innerHTML = `<span class="pl num"></span><span class="pn"></span><span class="pg"></span><span class="sc num"></span>`;
      el.style.transform = `translateY(${k * step}px)`;
      list.appendChild(el); sideRows.set(p.i, el); void el.offsetWidth;
    }
    el.classList.remove('gone');
    el.classList.toggle('now', p.i === S.current && !p.done);
    el.classList.toggle('lead', p.place === 1 && p.score > 0);
    el.style.transform = `translateY(${k * step}px)`; el.style.zIndex = p.i === S.current ? 2 : 1;
    el.querySelector('.pl').textContent = p.place;
    el.querySelector('.pn').textContent = p.name;
    el.querySelector('.pg').textContent = '';
    setRoll(el.querySelector('.sc'), p.score);
  });
  sideRows.forEach((el, i) => { if (!visible.has(i)) el.classList.add('gone'); });
  list.style.height = (shown.length * step) + 'px';
}
function fitWho(){}      // имя в плашке одной строкой, длинное обрезается многоточием
function clearSide(){ sideRows.forEach(el => el.remove()); sideRows.clear(); }

function setView(v){
  if (v === lastView) return; lastView = v;
  ['idle', 'game', 'final'].forEach(id => $(id).hidden = id !== v);
  $('stage').dataset.view = v; document.body.dataset.view = v;
  if (v === 'game') requestAnimationFrame(resizeCourse);
}

function renderHud(p, ph){
  const r = ph === 'play' ? remaining(S) : ph === 'countdown' || ph === 'ready' ? S.round : 0, tm = $('tm');
  const txt = fmtTime(r); if (tm.textContent !== txt) tm.textContent = txt;
  tm.classList.toggle('low', ph === 'play' && r <= 10);
}

function renderFinal(){
  const list = S.participants.map((p, i) => ({...p, i})).sort((a, b) => b.score - a.score || passedOf(b) - passedOf(a) || a.i - b.i);
  const key = JSON.stringify(list.map(p => [p.i, p.score, p.name, passedOf(p)]));
  if (key === lastFinalKey) return; lastFinalKey = key;
  const n = list.length, cols = n <= 8 ? 1 : n <= 18 ? 2 : 3, rows = Math.ceil(n / cols);
  const b = $('board'); b.style.setProperty('--cols', cols); b.style.setProperty('--rows', rows);
  const top = n ? list[0].score : 0;
  let place = 0, prev = null;
  b.innerHTML = list.map((p, k) => {
    const pk = p.score + ':' + passedOf(p);
    if (pk !== prev) { place = k + 1; prev = pk; }
    const win = place === 1 && top > 0;
    return `<div class="trow${win ? ' win' : ''}" style="animation-delay:${Math.min(k, 20) * .06}s">
      <span class="pl num">${place}</span><span class="pn">${esc(p.name)}</span><span class="sc num" data-v="${p.score}"></span><span class="u">стен</span></div>`;
  }).join('');
  b.querySelectorAll('.sc').forEach((el, k) => { setRoll(el, '0', {up: true}); setTimeout(() => setRoll(el, el.dataset.v, {up: true}), 250 + Math.min(k, 20) * 60); });
}

let flowAcc = 0, lastFrame = performance.now(), lastWho = '';
function frame(now){
  const dt = Math.min(.1, (now - lastFrame) / 1000); lastFrame = now;
  updatePitch(now, dt);
  const ph = S ? phaseOf(S) : 'idle', p = S && S.participants[S.current];
  const ct = S ? courseTime(S) : null;
  Bg.setPitch((foldPos(P.disp) - LOP) / (HIP - LOP));
  flowAcc += dt * (G.speed || 200) * (ph === 'play' || ph === 'countdown' ? 1 : .15);
  Bg.setFlow(flowAcc - (G.worldOff || 0) * .6);
  if (S) {
    $('round').textContent = p && !S.finished ? `${S.current + 1} из ${S.participants.length}` : '';
    $('roundHud').textContent = p && !S.finished ? `${S.current + 1} из ${S.participants.length}` : '';
    sounds(ph);
    if (ph === 'idle') { setView('idle'); resFor = -1; lastFinalKey = ''; clearSide(); }
    else if (ph === 'finished') { setView('final'); renderFinal(); resFor = -1; clearSide(); }
    else {
      setView('game'); lastFinalKey = '';
      if (lastWho !== p.name) { lastWho = p.name; $('who').textContent = p.name; fitWho(); }
      setRoll($('score'), String(p.score));
      const show = (id, on) => { const el = $(id); if (el.hidden === !on) return; el.hidden = !on; };
      show('ovReady', ph === 'ready');
      show('ovCd', ph === 'countdown');
      show('ovRes', ph === 'timeup');
      if (ph === 'countdown') {
        $('cdName').textContent = p.name + ', приготовьтесь';
        const n = String(Math.ceil(remaining(S)));
        if ($('cdNum')._text !== n) { setRoll($('cdNum'), n, {up: false}); Bg.pulse(); }
      }
      if (ph === 'timeup') {
        if (resFor !== S.current) {
          resFor = S.current;
          setRoll($('resNum'), '0', {up: true});
          setTimeout(() => { const q = S && S.participants[S.current]; if (q) setRoll($('resNum'), String(q.score), {up: true}); }, 350);
        } else setRoll($('resNum'), String(p.score), {up: true});
        $('resSub').textContent = 'Стен пройдено: ' + p.score;
      } else resFor = -1;
      renderHud(p, ph);
      if (ph === 'timeup') renderSide(); else clearSide();
    }
  }
  drawCourse(now);
}
function loop(now){ try { frame(now); } catch (e) { console.error(e); } requestAnimationFrame(loop); }
requestAnimationFrame(loop);
</script></body></html>"""

# ======================================================================
# Setup: выбор аудиовхода, тюнер, проверка попадания в ноту
# ======================================================================
SETUP_HTML = r"""<!doctype html>
<html lang="ru" data-theme="women" data-base="{{ base }}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#140710"><title>Точно в ноту · Setup</title>
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
.tuner{display:grid;grid-template-columns:auto 1fr;gap:6px 22px;align-items:center}
.tuner .name{font-family:var(--display);font-weight:900;font-size:76px;line-height:1;min-width:2.2em;color:var(--chalk)}
.tuner .name.dim{color:var(--line)}
.tuner .side{display:grid;gap:6px;font-size:14px;color:var(--mist)}
.tuner .side b{color:var(--chalk);font-variant-numeric:tabular-nums}
.badge{display:inline-block;padding:3px 10px;border-radius:999px;border:1px solid var(--line);font-weight:600;font-size:13px;color:var(--mist)}
.badge.ok{border-color:var(--signal);color:var(--signal)}
.centsbar{position:relative;height:30px;margin:20px 0 6px;border:1px solid var(--line);border-radius:var(--r-m);background:var(--ink-2)}
.centsbar:before{content:"";position:absolute;left:50%;top:4px;bottom:4px;width:2px;margin-left:-1px;background:var(--signal)}
.centsbar .tol{position:absolute;top:4px;bottom:4px;left:16.7%;right:16.7%;border-radius:8px;background:var(--signal-soft)}
.centsbar .needle{position:absolute;top:2px;bottom:2px;width:6px;margin-left:-3px;left:50%;border-radius:3px;background:var(--chalk);box-shadow:0 0 12px rgba(248,239,244,.7);transition:left .06s linear;opacity:.25}
.centsbar .needle.on{opacity:1}
.centslbl{display:flex;justify-content:space-between;font-size:12px;color:var(--mist);font-family:var(--digits);font-weight:800}
.lvl{position:relative;height:16px;margin-top:18px;border-radius:8px;background:var(--ink-2);border:1px solid var(--line);overflow:hidden}
.lvl i{position:absolute;left:0;top:0;bottom:0;width:0;background:var(--signal);opacity:.85}
.lvl b{position:absolute;top:0;bottom:0;width:3px;background:var(--chalk)}
.lvllbl{display:flex;justify-content:space-between;margin-top:6px;font-size:12px;color:var(--mist)}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
@media (max-width:520px){.row2{grid-template-columns:1fr}.tuner .name{font-size:56px}}
.drill{display:flex;align-items:center;gap:18px}
.drill .tg{font-family:var(--display);font-weight:900;font-size:56px;min-width:2.4em;line-height:1}
.drill .pr{flex:1}
.pbar{height:14px;border-radius:7px;background:var(--ink-2);border:1px solid var(--line);overflow:hidden}
.pbar i{display:block;height:100%;width:0;background:var(--signal)}
.drill .cnt{margin-top:8px;font-size:14px;color:var(--mist)}
.drill .cnt b{color:var(--signal);font-family:var(--digits);font-weight:900;font-size:20px}
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
ul.tips{margin:0;padding-left:20px;color:var(--mist);font-size:14px;line-height:1.6}
ul.tips b{color:var(--chalk)}
</style></head>
<body>
<main class="app">
  <div class="top"><span class="wordmark"><i></i>Точно в ноту · Setup</span>
    <span class="links"><a class="link" href="{{ base }}screen" target="_blank" rel="noopener">Экран для гостей</a><a class="link" href="{{ base }}" target="_blank" rel="noopener">Пульт ведущего</a></span></div>

  <p class="note">Микрофон слушает браузер <b>экрана для гостей</b>. Эта страница настраивает вход того компьютера и браузера, где она открыта, поэтому откройте её <b>там же, где открыт экран для гостей</b>. Настройки запоминаются, а открытый экран подхватывает смену входа сразу.</p>

  <section class="card">
    <h2>1. Аудиовход</h2>
    <select id="dev" aria-label="Аудиовход"></select>
    <button class="btn primary" id="allow">Разрешить доступ к микрофону</button>
    <button class="btn quiet" id="refresh">Обновить список устройств</button>
    <dl class="info" id="info"></dl>
    <div class="msg" id="msg" hidden></div>
  </section>

  <section class="card">
    <h2>2. Тюнер: слышит ли система голос</h2>
    <div class="tuner">
      <div class="name dim" id="nName">—</div>
      <div class="side"><div><span class="badge" id="nStable">нет голоса</span></div><div>Частота: <b id="nHz">—</b></div><div>Чистота звука: <b id="nClar">—</b></div></div>
    </div>
    <div class="centsbar"><div class="tol"></div><div class="needle" id="needle"></div></div>
    <div class="centslbl"><span>−50</span><span>центов</span><span>+50</span></div>
    <div class="lvl"><i id="lvlFill"></i><b id="lvlGate"></b></div>
    <div class="lvllbl"><span>тихо</span><span id="lvlTxt">порог голоса</span><span>громко</span></div>
    <div class="msg" id="tipMsg">Спойте ноту «а-а-а». Если название ноты появляется и держится, а значок показывает «устойчиво», всё работает.</div>
  </section>

  <section class="card">
    <h2>3. Попади в ноту</h2>
    <div class="drill"><div class="tg" id="tgName">—</div>
      <div class="pr"><div class="pbar"><i id="tgBar"></i></div><div class="cnt">Держите ноту 0,7 секунды. Попаданий: <b id="tgCnt">0</b></div></div></div>
    <p class="note" style="margin-top:12px">Любая октава подойдёт. Допуск: полшага шкалы в обе стороны, как щель в стене: у нот с тоном между ними это около тона, у ми–фа около полутона.</p>
  </section>

  <section class="card">
    <h2>4. Шум в зале и чувствительность <span class="saved" id="saved" style="margin-left:10px">Сохранено</span></h2>
    <button class="btn primary" id="calib" style="margin-top:0">Замерить шум в зале (3 секунды тишины)</button>
    <div class="msg" id="calMsg">Попросите всех притихнуть и нажмите кнопку. Порог «есть голос» поднимется над шумом, поэтому разговоры не дадут ложных нот.</div>
    <div class="slider">
      <div class="top2"><span>Порог голоса</span><span id="gateVal">0 dB</span></div>
      <input type="range" id="gate" min="-12" max="12" step="1" value="0" aria-label="Порог голоса">
      <p class="note">Голос тихий и обрывается: сдвиньте влево. Ловятся посторонние звуки: сдвиньте вправо.</p>
    </div>
  </section>

  <section class="card">
    <h2>5. Обработка звука браузером</h2>
    <label class="opt"><input type="checkbox" id="agc"><div><b>Автоусиление (AGC)</b><span>Подстраивает громкость сам. Для конкурса лучше выключить.</span></div></label>
    <label class="opt"><input type="checkbox" id="ns"><div><b>Шумоподавление</b><span>Может «съедать» тихие ноты. Лучше выключить.</span></div></label>
    <label class="opt"><input type="checkbox" id="ec"><div><b>Эхоподавление</b><span>Нужно только если звук из колонок попадает обратно в микрофон.</span></div></label>
  </section>

  <section class="card">
    <h2>Как добиться точных попаданий</h2>
    <ul class="tips">
      <li><b>Микрофон близко.</b> Лучше всего 10–20 см ото рта, направленный (вокальный или петличка). Голос должен быть заметно громче зала.</li>
      <li><b>Колонки тише.</b> Музыка и аплодисменты рядом с микрофоном мешают. Экран во время забега сам молчит.</li>
      <li><b>Не дуть в микрофон.</b> Буквы «п», «б» и дыхание дают стук. Петь лучше «а-а» или «о-о», чем слова.</li>
      <li><b>Замерьте шум.</b> Перед конкурсом нажмите кнопку из пункта 4. Если в зале стало шумнее, порог поднимается сам.</li>
      <li><b>Отключите AGC и шумоподавление.</b> Они искажают звук и сбивают определение высоты.</li>
    </ul>
  </section>
</main>
<script>{{ audio_js|safe }}</script>
<script>
const $ = id => document.getElementById(id);
const dev = $('dev');
let cfg = ntLoad();
const SCALE = [0, 2, 4, 5, 7, 9], NAMES_SC = {0: 'Do', 2: 'Re', 4: 'Mi', 5: 'Fa', 7: 'Sol', 9: 'La'}, TOL = 0.5, NEED = 0.7;
function flashSaved(){ const s = $('saved'); s.classList.add('on'); clearTimeout(flashSaved.t); flashSaved.t = setTimeout(() => s.classList.remove('on'), 1500); }
function showMsg(text, cls){ const m = $('msg'); m.hidden = !text; m.textContent = text || ''; m.className = 'msg ' + (cls || ''); }
function esc(v){ return String(v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

async function refreshDevices(){
  let list = [];
  try { list = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === 'audioinput'); } catch (e) {}
  dev.innerHTML = '';
  if (!list.length) { dev.innerHTML = '<option>Аудиовходы не найдены</option>'; dev.disabled = true; $('allow').hidden = false; return; }
  dev.disabled = false;
  list.forEach((d, i) => { const o = document.createElement('option'); o.value = d.deviceId; o.textContent = d.label || ('Аудиовход ' + (i + 1)); dev.appendChild(o); });
  const c = ntLoad();
  const pick = list.find(d => d.deviceId && d.deviceId === c.deviceId) || list.find(d => d.label && d.label === c.label) || list.find(d => d.deviceId && d.deviceId === Mic.deviceId);
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
Mic.onState = async () => {
  if (Mic.error) showMsg(Mic.error, 'bad');
  else if (Mic.note) showMsg(Mic.note, 'bad');
  else if (Mic.suspended) showMsg('Звук остановлен браузером. Нажмите в любом месте страницы.', 'bad');
  else if (Mic.running) showMsg('Микрофон работает. Спойте ноту: тюнер ниже должен показать её название.', 'ok');
  renderInfo();
  await refreshDevices();
};

/* ---------- тюнер, тренировка, замер шума ---------- */
let lastT = performance.now(), calibrating = false, calSamples = [], drill = {target: 0, hold: 0, count: 0, until: 0};
function newTarget(){ const prev = drill.target; let n; do { n = SCALE[Math.floor(Math.random() * SCALE.length)]; } while (n === prev); drill.target = n; drill.hold = 0; $('tgName').textContent = NAMES_SC[n]; }
newTarget();
const toPct = db => Math.max(0, Math.min(100, (db + 80) / 80 * 100));
Mic.onFrame = f => {
  const now = performance.now(), dt = Math.min(.12, (now - lastT) / 1000); lastT = now;
  if (calibrating) calSamples.push(f.dbfs);
  $('lvlFill').style.width = toPct(f.dbfs) + '%';
  $('lvlGate').style.left = toPct(f.gate) + '%';
  const nm = $('nName'), nd = $('needle'), st = $('nStable');
  if (f.voiced) {
    const mid = f.midi, idx = Math.round(mid), cents = (mid - idx) * 100;
    nm.textContent = NOTE_NAMES[((idx % 12) + 12) % 12]; nm.classList.remove('dim');
    $('nHz').textContent = f.hz.toFixed(1) + ' Гц'; $('nClar').textContent = Math.round(f.clarity * 100) + '%';
    nd.style.left = (50 + Math.max(-60, Math.min(60, cents))) + '%'; nd.classList.add('on');
    st.textContent = f.stable ? 'устойчиво' : 'плавает'; st.className = 'badge' + (f.stable ? ' ok' : '');
  } else {
    nm.textContent = '—'; nm.classList.add('dim'); $('nHz').textContent = '—'; $('nClar').textContent = '—';
    nd.classList.remove('on'); st.textContent = f.dbfs >= f.gate ? 'звук без высоты' : 'нет голоса'; st.className = 'badge';
  }
  $('lvlTxt').textContent = 'порог ' + Math.round(f.gate) + ' dBFS, сейчас ' + (f.dbfs > -119 ? Math.round(f.dbfs) : '—');
  // тренировка
  if (now < drill.until) return;
  if (f.voiced && f.stable && Math.abs(pdist(f.pc, drill.target)) <= TOL) drill.hold += dt; else drill.hold = Math.max(0, drill.hold - dt * .5);
  $('tgBar').style.width = Math.min(100, drill.hold / NEED * 100) + '%';
  if (drill.hold >= NEED) { drill.count++; $('tgCnt').textContent = drill.count; drill.until = now + 700; $('tgBar').style.width = '100%'; setTimeout(newTarget, 700); }
};

dev.onchange = async () => {
  const o = dev.selectedOptions[0];
  cfg = ntSave({deviceId: dev.value, label: o ? o.textContent : ''}); flashSaved();
  await Mic.start();
};
$('allow').onclick = async () => { $('allow').disabled = true; await Mic.start(); $('allow').disabled = false; };
$('refresh').onclick = refreshDevices;
navigator.mediaDevices && navigator.mediaDevices.addEventListener && navigator.mediaDevices.addEventListener('devicechange', refreshDevices);
addEventListener('pointerdown', () => { if (Mic.suspended) Mic.resume(); }, {passive: true});

['agc', 'ns', 'ec'].forEach(k => {
  $(k).checked = !!cfg[k];
  $(k).onchange = async () => { cfg = ntSave({[k]: $(k).checked}); flashSaved(); await Mic.start(); };
});

function showGate(v){ $('gateVal').textContent = (v > 0 ? '+' : v < 0 ? '−' : '') + Math.abs(v) + ' dB'; }
$('gate').value = cfg.gate; showGate(cfg.gate);
$('gate').oninput = () => { const v = +$('gate').value; showGate(v); Mic.setGate(v); };
$('gate').onchange = () => { cfg = ntSave({gate: +$('gate').value}); flashSaved(); };

$('calib').onclick = () => {
  if (!Mic.running || calibrating) return;
  calibrating = true; calSamples = []; $('calib').disabled = true;
  const msg = $('calMsg'); msg.className = 'msg ok';
  let left = 3;
  const step = () => {
    if (left > 0) { msg.textContent = `Тишина… осталось ${left} с`; left--; setTimeout(step, 1000); return; }
    calibrating = false; $('calib').disabled = false;
    if (calSamples.length < 20) { msg.className = 'msg bad'; msg.textContent = 'Не удалось замерить: микрофон не отдаёт звук.'; return; }
    calSamples.sort((a, b) => a - b);
    const noise = Math.round(calSamples[Math.floor(calSamples.length * .9)]);     // 90-й процентиль: редкие щелчки не в счёт
    cfg = ntSave({noise}); Mic.setNoise(noise); flashSaved();
    msg.textContent = `Готово: шум в зале около ${noise} dBFS. Голос должен быть громче ${Math.max(noise + 8, -56 + cfg.gate)} dBFS: шкала выше показывает порог белой чертой.`;
  };
  step();
};

Mic.start().then(() => refreshDevices());
</script></body></html>"""

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), allow_unsafe_werkzeug=True)
