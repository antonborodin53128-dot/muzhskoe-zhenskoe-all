"""Диктант — конкурс «Мужское / Женское».

Участники по очереди пишут на клавиатуре слово, которое произносит ведущий. Каждая
ошибка в слове — это +1 ошибка (неверная, пропущенная или лишняя буква). Побеждает тот,
у кого за игру меньше всего ошибок.

Один экран и два набора по 10 слов. Перед стартом ведущий выбирает набор и запускает
его на экран; после окончания можно сбросить игру и запустить другой набор.

  /                    — пульт ведущего (телефон): выбор набора, кнопка «Озвучить слово»
  /screen              — экран для гостей (проектор); к нему подключена клавиатура
                         и колонки, слово звучит на нём
  /audio-check         — прослушать озвучку всех 20 слов перед конкурсом

Клавиши работают на любой раскладке: русские буквы берутся по положению клавиши
(как в «Хомяке» буквы A–Z). Все ссылки относительные, поэтому этот же файл работает
и отдельно, и внутри общего сборника (/women/diktant/...).
"""
import io
import json
import os
import random
import secrets
import tempfile
import time
from threading import RLock

from flask import Flask, Response, request, send_file
from flask_socketio import SocketIO, emit, join_room, leave_room

from dictation_audio import audio_bytes

# Два набора по 10 слов; ведущий выбирает набор перед стартом.
WORD_SETS = {
    "1": ["интеллигентность", "ассимиляция", "параллелепипед", "аббревиатура", "искусство",
          "привередливый", "комбинезон", "периферия", "бюллетень", "целлофан"],
    "2": ["иррациональность", "идентифицировать", "коррозия", "прецедент", "палисадник",
          "поскользнуться", "привилегия", "пессимистичный", "прерогатива", "брошюра"],
}
WORDS_PER_GAME = 10
# Номер озвучки каждого слова. Экран гостей знает только номер, а не само слово.
AUDIO_IDS = {w: f"{n:02d}" for n, w in enumerate(WORD_SETS["1"] + WORD_SETS["2"], start=1)}
AUDIO_WORDS = {i: w for w, i in AUDIO_IDS.items()}

MAX_PARTICIPANTS = 12
MAX_TYPED = 40
ALLOWED = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюя-")

# Состояние игр переживает перезапуск процесса: ведущий не теряет конкурс из-за сбоя сервера.
STATE_FILE = os.environ.get("DICTATION_STATE_FILE", os.path.join(tempfile.gettempdir(), "dictation_state.json"))
STATE_TTL = 6 * 3600
BOOT = secrets.token_hex(4)   # меняется при каждом запуске: клиенты понимают, что номера снимков пошли заново

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading", ping_interval=15, ping_timeout=25)

# ВАЖНО. socketio.emit() может прямо внутри себя закрыть «протухшее» соединение (например, свёрнутое окно)
# и тут же вызвать on_disconnect в том же потоке. Если emit() вызван под этой блокировкой, а on_disconnect
# тоже берёт её, поток зависает сам на себе, и весь сервер перестаёт отвечать.
# Поэтому: под блокировкой только меняем состояние и собираем снимки, а рассылаем всегда ПОСЛЕ неё.
lock = RLock()


def new_game():
    return {
        "set": None,           # номер выбранного набора слов
        "phase": "idle",       # idle → typing ⇄ reveal → finished
        "participants": [],    # [{"name": str}]
        "words": [],           # слова игры в случайном порядке
        "wi": 0,               # номер слова
        "pi": 0,               # кто пишет сейчас
        "typed": "",           # что набрано сейчас
        "answers": {},         # {"слово": {"участник": {"typed", "errors", "ops"}}}
        "show_table": False,   # показать таблицу лидеров на экране
        "resume": None,        # куда вернуться, если конкурс завершили досрочно
        "bump": 0,
    }


GID = "main"                             # игра одна; ключ нужен только для общих структур ниже
G = {GID: new_game()}
REV = {GID: 0}          # номер снимка: клиент игнорирует снимок, обогнанный более новым
PLAY_SEQ = {GID: 0}
SCREENS = {GID: {}}     # sid экрана -> включён ли на нём звук
SID_INFO = {}                            # sid -> (игра, роль)


# ---------- подсчёт ошибок ----------

def calc(right, typed):
    """Расстояние Левенштейна: каждая неверная, пропущенная или лишняя буква — 1 ошибка.
    Возвращает число ошибок и разбор по буквам для подсветки на экране."""
    a, b = right.lower(), typed.lower()
    n, m = len(a), len(b)
    d = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = i
    for j in range(m + 1):
        d[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
    i, j, ops = n, m, []
    while i or j:
        if i and j and d[i][j] == d[i - 1][j - 1] + (a[i - 1] != b[j - 1]):
            ops.append(["ok" if a[i - 1] == b[j - 1] else "sub", a[i - 1], b[j - 1]])
            i -= 1
            j -= 1
        elif i and d[i][j] == d[i - 1][j] + 1:
            ops.append(["miss", a[i - 1], ""])      # буква пропущена
            i -= 1
        else:
            ops.append(["extra", "", b[j - 1]])     # лишняя буква
            j -= 1
    return d[n][m], list(reversed(ops))


def clean_text(value):
    return "".join(ch for ch in str(value).lower() if ch in ALLOWED)[:MAX_TYPED]


# ---------- состояние ----------

def totals_locked(g):
    totals = [0] * len(g["participants"])
    for row in g["answers"].values():
        for pi, a in row.items():
            if int(pi) < len(totals):
                totals[int(pi)] += a["errors"]
    return totals


def current_word_locked(g):
    if g["phase"] in ("typing", "reveal") and 0 <= g["wi"] < len(g["words"]):
        return g["words"][g["wi"]]
    return ""


def answers_for_word_locked(g, wi):
    row = g["answers"].get(str(wi), {})
    out = []
    for pi, p in enumerate(g["participants"]):
        a = row.get(str(pi))
        if a:
            out.append({"pi": pi, "name": p["name"], "typed": a["typed"], "errors": a["errors"], "ops": a["ops"]})
    return out


def snapshot_locked(gid, role):
    g = G[gid]
    REV[gid] += 1
    ph = g["phase"]
    parts, tot = g["participants"], totals_locked(g)
    word = current_word_locked(g)
    snap = {
        "set": g["set"],
        "role": role,
        "phase": ph,
        "participants": [{"name": p["name"], "errors": tot[i]} for i, p in enumerate(parts)],
        "wi": g["wi"],
        "total": WORDS_PER_GAME,
        "pi": g["pi"],
        "name": parts[g["pi"]]["name"] if ph == "typing" and g["pi"] < len(parts) else "",
        "typed": g["typed"] if ph == "typing" else "",
        "show_table": g["show_table"],
        "audio": f"audio/{AUDIO_IDS[word]}.mp3" if word else "",
        "screens": {"count": len(SCREENS[gid]), "audio": sum(1 for v in SCREENS[gid].values() if v)},
        "bump": g["bump"],
        "rev": REV[gid],
        "boot": BOOT,
        "server_now": time.time(),
    }
    if role == "host":
        if word:
            snap["word"] = word
        if ph in ("typing", "reveal"):
            snap["answers"] = answers_for_word_locked(g, g["wi"])
    elif ph == "reveal":
        snap["word"] = word
        snap["answers"] = answers_for_word_locked(g, g["wi"])
    return snap


def snaps_locked(gid):
    return {"host": snapshot_locked(gid, "host"), "screen": snapshot_locked(gid, "screen")}


def publish(gid, snaps):
    """Разослать снимки. Вызывать только когда блокировка уже отпущена."""
    socketio.emit("state", snaps["host"], to=f"{gid}:host")
    socketio.emit("state", snaps["screen"], to=f"{gid}:screen")


def save_locked():
    data = {"saved_at": time.time(), "games": {}}
    for gid, g in G.items():
        data["games"][gid] = {
            "set": g["set"], "phase": g["phase"], "participants": g["participants"], "words": g["words"],
            "wi": g["wi"], "pi": g["pi"], "show_table": g["show_table"], "bump": g["bump"],
            "resume": g["resume"],
            # ошибки и разбор пересчитываются при загрузке, в файле только то, что набрали
            "answers": {wi: {pi: a["typed"] for pi, a in row.items()} for wi, row in g["answers"].items()},
        }
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
        if time.time() - float(data["saved_at"]) > STATE_TTL:
            return
        for gid, raw in data["games"].items():
            if gid != GID or raw.get("set") not in WORD_SETS:
                continue
            phase = raw["phase"]
            parts = [{"name": " ".join(str(p["name"]).split())[:32] or f"Участник {i + 1}"}
                     for i, p in enumerate(raw["participants"])][:MAX_PARTICIPANTS]
            if phase == "idle" or not parts:
                continue
            words = [str(w) for w in raw["words"]]
            if sorted(words) != sorted(WORD_SETS[raw["set"]]) or phase not in ("typing", "reveal", "finished"):
                continue
            wi, pi = int(raw["wi"]), int(raw["pi"])
            if not (0 <= wi <= WORDS_PER_GAME and 0 <= pi < len(parts)):
                continue
            answers = {}
            for w_idx, row in raw["answers"].items():
                if not 0 <= int(w_idx) < WORDS_PER_GAME:
                    continue
                for p_idx, typed in row.items():
                    if not 0 <= int(p_idx) < len(parts):
                        continue
                    typed = clean_text(typed)
                    errors, ops = calc(words[int(w_idx)], typed)
                    answers.setdefault(str(int(w_idx)), {})[str(int(p_idx))] = {"typed": typed, "errors": errors, "ops": ops}
            resume = raw.get("resume")
            if resume is not None:
                resume = [str(resume[0]), int(resume[1]), int(resume[2])]
            g = G[gid]
            g.update(set=raw["set"], phase=phase, participants=parts, words=words, wi=wi, pi=pi, typed="", answers=answers,
                     show_table=bool(raw.get("show_table", False)), resume=resume,
                     bump=int(raw.get("bump", 0)) + 1)
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
        pass


load_state()


def submit_locked(g, typed=None, allow_empty=False):
    """Записать ответ текущего участника и передать ход дальше."""
    if g["phase"] != "typing":
        return False
    if typed is not None:
        g["typed"] = clean_text(typed)   # засчитываем то, что участник видел на экране
    text = g["typed"]
    if not text and not allow_empty:
        return False
    errors, ops = calc(g["words"][g["wi"]], text)
    g["answers"].setdefault(str(g["wi"]), {})[str(g["pi"])] = {"typed": text, "errors": errors, "ops": ops}
    g["typed"] = ""
    g["pi"] += 1
    if g["pi"] >= len(g["participants"]):
        g["pi"] = 0
        g["phase"] = "reveal"
    g["show_table"] = False
    g["bump"] += 1
    return True


def step_back_locked(g):
    """Шаг назад: вернуть последнего ответившего (его ответ можно переписать) или предыдущее слово."""
    ph = g["phase"]
    parts = len(g["participants"])
    if ph == "finished":
        if g["resume"]:
            g["phase"], g["wi"], g["pi"] = g["resume"][0], g["resume"][1], g["resume"][2]
            g["resume"] = None
        elif g["wi"] >= WORDS_PER_GAME:
            g["wi"], g["phase"], g["pi"] = WORDS_PER_GAME - 1, "reveal", 0
        else:
            return False
        g["typed"] = ""
    elif ph == "reveal":
        g["phase"], g["pi"] = "typing", parts - 1
        a = g["answers"].get(str(g["wi"]), {}).pop(str(g["pi"]), None)
        g["typed"] = a["typed"] if a else ""
    elif ph == "typing" and g["pi"] > 0:
        g["pi"] -= 1
        a = g["answers"].get(str(g["wi"]), {}).pop(str(g["pi"]), None)
        g["typed"] = a["typed"] if a else ""
    elif ph == "typing" and g["wi"] > 0:
        g["wi"] -= 1
        g["phase"], g["pi"], g["typed"] = "reveal", 0, ""
    else:
        return False
    g["show_table"] = False
    g["bump"] += 1
    return True


def payload(data):
    """Клиент может прислать что угодно; нужен только словарь."""
    return data if isinstance(data, dict) else {}


def game_of(data):
    return GID


def int_of(value, default=-1):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def at_current(g, data):
    """Команда пришла про ту же очередь, что и на сервере. Не даёт запоздалому «Enter» уйти следующему участнику."""
    return g["phase"] == "typing" and int_of(data.get("wi")) == g["wi"] and int_of(data.get("pi")) == g["pi"]


# ---------- события ----------

def attach_locked(sid, gid, role):
    """Подключить сокет к игре и роли; вернуть игру, которую он покинул, чтобы обновить счётчики."""
    old = SID_INFO.get(sid)
    if old:
        SCREENS[old[0]].pop(sid, None)
    SID_INFO[sid] = (gid, role)
    if role == "screen":
        SCREENS[gid].setdefault(sid, False)
    return old


@socketio.on("join")
def on_join(data=None):
    data = payload(data)
    gid = game_of(data)
    role = "screen" if data.get("role") == "screen" else "host"
    if not gid:
        return
    sid = request.sid
    with lock:
        old = SID_INFO.get(sid)
        if old:
            leave_room(f"{old[0]}:{old[1]}")
        join_room(f"{gid}:{role}")
        old = attach_locked(sid, gid, role)
        mine = snapshot_locked(gid, role)
        old_snaps = snaps_locked(old[0]) if old and old[0] != gid else None
        new_snaps = snaps_locked(gid) if role == "screen" else None
    emit("state", mine)
    if old_snaps:
        publish(old[0], old_snaps)
    if new_snaps:
        publish(gid, new_snaps)


@socketio.on("sync")
def on_sync(data=None):
    data = payload(data)
    gid = game_of(data)
    role = "screen" if data.get("role") == "screen" else "host"
    if not gid:
        return
    if SID_INFO.get(request.sid) != (gid, role):
        return on_join(data)
    with lock:
        snap = snapshot_locked(gid, role)
    emit("state", snap)


@socketio.on("disconnect")
def on_disconnect(*args):
    snaps, gid = None, None
    with lock:
        info = SID_INFO.pop(request.sid, None)
        if info:
            gid = info[0]
            if SCREENS[gid].pop(request.sid, None) is not None:
                snaps = snaps_locked(gid)
    if snaps:
        publish(gid, snaps)


@socketio.on("screen_audio")
def on_screen_audio(data=None):
    """Экран сообщает, включён ли на нём звук: пульт показывает, услышат ли слово."""
    data = payload(data)
    gid = game_of(data)
    if not gid:
        return
    with lock:
        if request.sid not in SCREENS[gid]:
            return
        SCREENS[gid][request.sid] = bool(data.get("ready"))
        snaps = snaps_locked(gid)
    publish(gid, snaps)


@socketio.on("setup")
def on_setup(data=None):
    data = payload(data)
    gid = game_of(data)
    if not gid:
        return
    count = max(1, min(MAX_PARTICIPANTS, int_of(data.get("count"), 4)))
    set_id = str(data.get("set", ""))
    if set_id not in WORD_SETS:
        return
    with lock:
        g = G[gid]
        if g["phase"] != "idle":
            return
        words = WORD_SETS[set_id][:]
        random.shuffle(words)
        g.update(new_game())
        g.update(set=set_id, phase="typing", words=words,
                 participants=[{"name": f"Участник {i + 1}"} for i in range(count)], bump=1)
        save_locked()
        snaps = snaps_locked(gid)
    publish(gid, snaps)


@socketio.on("typing")
def on_typing(data=None):
    """Участник набирает слово. Присылается весь текст целиком, поэтому потерянная клавиша ничего не ломает."""
    data = payload(data)
    gid = game_of(data)
    if not gid:
        return
    with lock:
        g = G[gid]
        if not at_current(g, data):
            return
        g["typed"] = clean_text(data.get("typed", ""))
        snaps = snaps_locked(gid)
    publish(gid, snaps)


@socketio.on("submit")
def on_submit(data=None):
    data = payload(data)
    gid = game_of(data)
    if not gid:
        return {"ok": False}
    with lock:
        g = G[gid]
        if not at_current(g, data) or not submit_locked(g, data.get("typed", "")):
            return {"ok": False}
        save_locked()
        snaps = snaps_locked(gid)
    publish(gid, snaps)
    return {"ok": True}


@socketio.on("skip")
def on_skip(data=None):
    """Участник не смог написать слово: ответ пустой, каждая буква слова — ошибка."""
    data = payload(data)
    gid = game_of(data)
    if not gid:
        return
    with lock:
        g = G[gid]
        if not at_current(g, data) or not submit_locked(g, "", allow_empty=True):
            return
        save_locked()
        snaps = snaps_locked(gid)
    publish(gid, snaps)


@socketio.on("back")
def on_back(data=None):
    gid = game_of(data)
    if not gid:
        return
    with lock:
        if not step_back_locked(G[gid]):
            return
        save_locked()
        snaps = snaps_locked(gid)
    publish(gid, snaps)


@socketio.on("next")
def on_next(data=None):
    gid = game_of(data)
    if not gid:
        return
    with lock:
        g = G[gid]
        if g["phase"] != "reveal":
            return
        g["wi"] += 1
        g["pi"], g["typed"], g["show_table"] = 0, "", False
        g["phase"] = "finished" if g["wi"] >= WORDS_PER_GAME else "typing"
        g["bump"] += 1
        save_locked()
        snaps = snaps_locked(gid)
    publish(gid, snaps)


@socketio.on("table")
def on_table(data=None):
    data = payload(data)
    gid = game_of(data)
    if not gid:
        return
    with lock:
        g = G[gid]
        if g["phase"] in ("idle", "finished"):
            return
        g["show_table"] = bool(data.get("show"))
        save_locked()
        snaps = snaps_locked(gid)
    publish(gid, snaps)


@socketio.on("finish")
def on_finish(data=None):
    """Завершить конкурс досрочно. Слово, на которое ответили не все, не засчитывается никому."""
    gid = game_of(data)
    if not gid:
        return
    with lock:
        g = G[gid]
        if g["phase"] not in ("typing", "reveal"):
            return
        g["resume"] = [g["phase"], g["wi"], g["pi"]]
        if g["phase"] == "typing":
            g["answers"].pop(str(g["wi"]), None)
        g["typed"], g["show_table"], g["phase"] = "", False, "finished"
        g["bump"] += 1
        save_locked()
        snaps = snaps_locked(gid)
    publish(gid, snaps)


@socketio.on("rename")
def on_rename(data=None):
    data = payload(data)
    gid = game_of(data)
    if not gid:
        return
    i = int_of(data.get("index"))
    name = " ".join(str(data.get("name", "")).split())[:32]
    with lock:
        g = G[gid]
        if not 0 <= i < len(g["participants"]):
            return
        g["participants"][i]["name"] = name or f"Участник {i + 1}"
        save_locked()
        snaps = snaps_locked(gid)
    publish(gid, snaps)


@socketio.on("reset")
def on_reset(data=None):
    gid = game_of(data)
    if not gid:
        return
    with lock:
        bump = G[gid]["bump"] + 1
        G[gid].update(new_game())
        G[gid]["bump"] = bump
        save_locked()
        snaps = snaps_locked(gid)
    publish(gid, snaps)


@socketio.on("play")
def on_play(data=None):
    """Кнопка «Озвучить слово»: экран гостей проигрывает озвучку текущего слова."""
    gid = game_of(data)
    if not gid:
        return {"ok": False, "why": "game"}
    with lock:
        word = current_word_locked(G[gid])
        if not word:
            return {"ok": False, "why": "phase"}
        PLAY_SEQ[gid] += 1
        msg = {"seq": PLAY_SEQ[gid], "audio": f"audio/{AUDIO_IDS[word]}.mp3", "word": word}
        screens, audio = len(SCREENS[gid]), sum(1 for v in SCREENS[gid].values() if v)
    socketio.emit("play", msg, to=f"{gid}:screen")
    return {"ok": True, "screens": screens, "audio": audio}


# ---------- страницы ----------

def base_path():
    # "/" отдельно или "/women/diktant/" внутри сборника
    return (request.script_root or "") + "/"


@app.after_request
def no_cache(resp):
    if request.path in ("/", "/screen", "/audio-check"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def page(template, **extra):
    html = (template.replace("%%THEME_CSS%%", THEME_CSS).replace("%%CLIENT_JS%%", CLIENT_JS)
            .replace("%%BG_JS%%", BG_JS).replace("%%SND_JS%%", SND_JS).replace("%%FONTS%%", FONTS)
            .replace("%%BASE%%", base_path()))
    for key, value in extra.items():
        html = html.replace(f"%%{key}%%", value)
    return Response(html, mimetype="text/html")


@app.get("/")
def control():
    preview = {f"SET{sid}": ", ".join(words[:3]) + "…" for sid, words in WORD_SETS.items()}
    return page(CONTROL_HTML, **preview)


@app.get("/screen")
def screen():
    return page(SCREEN_HTML)


@app.get("/audio-check")
def audio_check():
    rows = [{"set": sid, "n": n, "word": w, "id": AUDIO_IDS[w]}
            for sid, words in WORD_SETS.items() for n, w in enumerate(words, start=1)]
    return page(CHECK_HTML, ROWS=json.dumps(rows, ensure_ascii=False))


@app.get("/healthz")
def healthz():
    # Пинг от открытых страниц: не даёт бесплатному хостингу «заснуть» и позволяет странице понять, жив ли сервер
    return "ok", 200, {"Cache-Control": "no-store", "Content-Type": "text/plain"}


@app.get("/audio/<audio_id>.mp3")
def audio_file(audio_id):
    word = AUDIO_WORDS.get(audio_id)
    data = audio_bytes(word) if word else None
    if not data:
        return "Not found", 404
    return send_file(io.BytesIO(data), mimetype="audio/mpeg", conditional=True, max_age=86400,
                     etag=f"dictation-{audio_id}-{len(data)}")

# ======================================================================
# Оформление. Розовая тема «Женское» — те же цвета и шрифты, что в «Хомяке».
# ======================================================================
FONTS = """<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Onest:wght@400;600;800&family=Unbounded:wght@600;800;900&family=Nunito:wght@800;900&display=swap" rel="stylesheet">
<script src="https://cdn.socket.io/4.8.1/socket.io.min.js"></script>"""

THEME_CSS = r"""
:root,[data-theme=women]{
  --ink:#140710; --ink-2:#1d0b17; --surface:#26101f; --line:#4e1d3b;
  --signal:#ff4fa8; --signal-ink:#22000f; --signal-soft:rgba(255,79,168,.14);
  --chalk:#f8eff4; --mist:#b394a6; --danger:#ffb08e; --danger-bg:#2d1714;
  --bad:#ff6f8f; --warm:#ffd36e;
  --display:"Unbounded",system-ui,sans-serif; --ui:"Onest",system-ui,sans-serif; --digits:"Nunito",var(--display);
  --r-s:12px; --r-m:18px; --r-l:28px;
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--ink);color:var(--chalk);font-family:var(--ui);-webkit-font-smoothing:antialiased}
body{min-height:100vh;min-height:100dvh}
button{font:inherit;color:inherit;cursor:pointer;-webkit-tap-highlight-color:transparent}
button:focus-visible,a:focus-visible{outline:3px solid var(--signal);outline-offset:3px}
[hidden]{display:none!important}
#bg{position:fixed;inset:0;width:100%;height:100%;z-index:0;pointer-events:none}
.wordmark{display:inline-flex;align-items:center;gap:.5em;font-family:var(--display);font-weight:800;letter-spacing:.02em}
.wordmark i{width:.62em;height:.62em;border-radius:50%;background:var(--signal);box-shadow:0 0 18px var(--signal)}
.offline{position:fixed;left:0;right:0;top:0;z-index:100;padding:10px 16px;text-align:center;font-weight:600;background:var(--danger-bg);color:var(--danger);transform:translateY(-100%);transition:transform .25s}
.offline.on{transform:none}
.offbtn{margin-left:12px;border:1px solid currentColor;background:none;color:inherit;border-radius:999px;padding:4px 14px;font:inherit;font-weight:700;cursor:pointer}
/* Разбор слова по буквам */
.diff{font-family:var(--digits);font-weight:900;letter-spacing:.04em;word-break:break-all}
.diff .sub{color:var(--bad);text-decoration:underline wavy;text-underline-offset:.14em}
.diff s{color:var(--bad);opacity:.8}
.diff .miss{color:var(--warm);border-bottom:.08em dashed var(--warm);opacity:.95}
.diff .miss:before{content:"+"}
.badge{display:inline-block;border-radius:999px;padding:.18em .7em;font-weight:800;font-size:.8em;white-space:nowrap;background:var(--bad);color:#2a0010}
.badge.zero{background:var(--signal);color:var(--signal-ink)}
@media (prefers-reduced-motion:reduce){*,*:before,*:after{animation-duration:.01ms!important;transition-duration:.01ms!important}}
"""

# ======================================================================
# Общая логика клиента: связь, ввод с клавиатуры, разбор слова.
# ======================================================================
CLIENT_JS = r"""
const BASE = document.documentElement.dataset.base || '/';
const ROLE = document.documentElement.dataset.role === 'screen' ? 'screen' : 'host';
const MAX_TYPED = 40;
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function setText(el, v){ v = String(v); if (el._v !== v) { el._v = v; el.textContent = v; } }   // трогаем DOM только при изменении
function setHtml(el, h){ if (el._h !== h) { el._h = h; el.innerHTML = h; } }
function pl(n){ const a = n % 10, b = n % 100; return a === 1 && b !== 11 ? 'ошибка' : (a >= 2 && a <= 4 && (b < 10 || b >= 20)) ? 'ошибки' : 'ошибок'; }
function badge(n){ return `<span class="badge${n ? '' : ' zero'}">${n} ${pl(n)}</span>`; }
// Разбор: верная буква — как есть, заменённая — красным, лишняя — зачёркнута, пропущенная — жёлтым «+буква».
function diffHtml(ops){
  return ops.map(o => o[0] === 'ok' ? esc(o[2]) : o[0] === 'sub' ? `<span class="sub">${esc(o[2])}</span>`
    : o[0] === 'extra' ? `<s>${esc(o[2])}</s>` : `<span class="miss">${esc(o[1])}</span>`).join('');
}
// Места: меньше ошибок — выше место, равные результаты делят место.
function ranked(list){
  const sorted = list.map((p, i) => ({...p, i})).sort((a, b) => a.errors - b.errors || a.i - b.i);
  let place = 0, prev = null;
  sorted.forEach((p, k) => { if (p.errors !== prev) { place = k + 1; prev = p.errors; } p.place = place; });
  return sorted;
}

const socket = io({path: BASE + 'socket.io', transports: ['websocket', 'polling'], tryAllTransports: true,
                   reconnectionDelay: 400, reconnectionDelayMax: 2500, timeout: 8000});
let S = null, bootId = null, downSince = 0, lastState = performance.now();
const offlineBar = $('offline');
if (offlineBar) {
  const b = document.createElement('button'); b.className = 'offbtn'; b.textContent = 'Обновить страницу';
  b.onclick = () => location.reload(); offlineBar.appendChild(b);
}
const setOffline = on => { if (offlineBar) offlineBar.classList.toggle('on', on); };
socket.on('connect', () => { downSince = 0; lastState = performance.now(); setOffline(false); socket.emit('join', {role: ROLE}); window.onConnect && window.onConnect(); });
socket.on('disconnect', () => { if (!downSince) downSince = performance.now(); setOffline(true); });
socket.on('connect_error', () => { if (!downSince) downSince = performance.now(); setOffline(true); });

/* ---------- набор слова ----------
   Текст хранится целиком и уходит на сервер целиком: потерянная клавиша ничего не ломает.
   Клавиши берутся по положению на клавиатуре, поэтому раскладка не важна. */
const KEYMAP = {KeyQ:'й',KeyW:'ц',KeyE:'у',KeyR:'к',KeyT:'е',KeyY:'н',KeyU:'г',KeyI:'ш',KeyO:'щ',KeyP:'з',BracketLeft:'х',BracketRight:'ъ',
  KeyA:'ф',KeyS:'ы',KeyD:'в',KeyF:'а',KeyG:'п',KeyH:'р',KeyJ:'о',KeyK:'л',KeyL:'д',Semicolon:'ж',Quote:'э',
  KeyZ:'я',KeyX:'ч',KeyC:'с',KeyV:'м',KeyB:'и',KeyN:'т',KeyM:'ь',Comma:'б',Period:'ю',Backquote:'ё',Minus:'-'};
let typed = '', turnKey = '', lastKeyAt = 0;
const turnOf = s => s && s.phase === 'typing' ? s.wi + ':' + s.pi : '';
function syncTyped(s){
  const k = turnOf(s);
  if (k !== turnKey) { turnKey = k; typed = s.typed || ''; }
  else if (k && performance.now() - lastKeyAt > 800 && (s.typed || '') !== typed) typed = s.typed || '';
}
socket.on('state', s => {
  if (s.boot !== bootId) { bootId = s.boot; S = null; }        // сервер перезапускался: номера снимков пошли заново
  else if (S && s.rev < S.rev) return;                          // более старый снимок обогнал новый — не откатываемся
  lastState = performance.now();
  S = s; syncTyped(s);
  try { window.onState && window.onState(s); } catch (e) { console.error(e); }
});
function emitGame(name, data, ack){
  if (!socket.connected) { setOffline(true); return false; }    // команды не копятся: «Дальше» не должно выстрелить позже
  const msg = data || {};
  if (ack) socket.emit(name, msg, ack); else socket.emit(name, msg);   // пустой аргумент ушёл бы на сервер как null
  return true;
}
function emitTyped(){ if (S) emitGame('typing', {wi: S.wi, pi: S.pi, typed}); }
function doSubmit(){
  if (!S || S.phase !== 'typing') return;
  if (!typed) { window.Snd && Snd.error(); window.onEmpty && window.onEmpty(); return; }
  const sent = emitGame('submit', {wi: S.wi, pi: S.pi, typed}, ack => {
    if (ack && ack.ok) { typed = ''; lastKeyAt = 0; window.Snd && Snd.enter(); } else window.Snd && Snd.error();
  });
  if (!sent) window.Snd && Snd.error();
}
addEventListener('keydown', e => {
  if (e.ctrlKey || e.metaKey || e.altKey || e.isComposing) return;
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA')) return;
  window.onAnyKey && window.onAnyKey();
  if (!S || S.phase !== 'typing') return;
  if (e.key === 'Backspace') { e.preventDefault(); if (typed) { typed = typed.slice(0, -1); keyDone(); } return; }
  if (e.key === 'Enter') { e.preventDefault(); if (!e.repeat) doSubmit(); return; }
  let ch = KEYMAP[e.code];
  if (!ch && e.key && e.key.length === 1 && /[а-яё-]/i.test(e.key)) ch = e.key.toLowerCase();
  if (!ch) return;
  e.preventDefault();
  if (typed.length < MAX_TYPED) { typed += ch; keyDone(); }
});
function keyDone(){
  lastKeyAt = performance.now();
  window.Snd && Snd.tick(); window.Bg && Bg.pulse();
  window.renderTyped && window.renderTyped();
  emitTyped();
}

/* ---------- живучесть связи ---------- */
const wake = () => { if (document.hidden) return; if (socket.connected) socket.emit('sync', {role: ROLE}); else { try { socket.connect(); } catch (e) {} } };
document.addEventListener('visibilitychange', wake);
addEventListener('online', wake);
addEventListener('focus', wake);
addEventListener('pageshow', wake);
function reconnect(){ lastState = performance.now(); setOffline(true); try { socket.disconnect(); socket.connect(); } catch (e) {} }
setInterval(() => {                       // живой пульс: страница никогда не «застывает» молча
  if (document.hidden) return;
  if (socket.connected) { socket.emit('sync', {role: ROLE}); if (performance.now() - lastState > 12000) reconnect(); }
}, 2000);
setInterval(() => { fetch(BASE + 'healthz', {cache: 'no-store'}).catch(() => {}); }, 240000);   // хостинг не засыпает
// Связи нет дольше 20 секунд, а сервер отвечает: сокет «залип». Свежая страница вернёт связь, игра хранится на сервере.
setInterval(async () => {
  if (socket.connected || document.hidden) return;
  if (!downSince) downSince = performance.now();
  if (performance.now() - downSince < 20000) return;
  downSince = performance.now();
  try { const r = await fetch(BASE + 'healthz', {cache: 'no-store'}); if (r.ok && !socket.connected) location.reload(); } catch (e) {}
}, 5000);
"""

# ======================================================================
# Фон: маленькие карандаши медленно плывут по экрану; от каждой клавиши вспыхивают.
# ======================================================================
BG_JS = r"""
const Bg = (() => {
  const cv = document.getElementById('bg'); if (!cv) return {pulse(){}};
  const cx = cv.getContext('2d');
  const calm = matchMedia('(prefers-reduced-motion: reduce)').matches;
  let W = 0, H = 0, dpr = 1, items = [], glow = 0, rgb = '255,79,168', last = performance.now();
  const rnd = (a, b) => a + Math.random() * (b - a);
  function make(anywhere){
    const len = rnd(20, 40);
    return {x: anywhere ? rnd(0, W) : rnd(-40, W), y: anywhere ? rnd(0, H) : H + 40, len, a: rnd(0, 6.283),
            vx: rnd(-5, 9), vy: -rnd(6, 16), spin: rnd(-.25, .25), al: rnd(.16, .34), ph: rnd(0, 6.283)};
  }
  function resize(){
    dpr = Math.min(2, devicePixelRatio || 1); W = innerWidth; H = innerHeight;
    cv.width = W * dpr; cv.height = H * dpr; cx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const n = Math.max(14, Math.min(46, Math.round(W * H / 26000)));
    items = Array.from({length: n}, () => make(true));
    try { const c = getComputedStyle(document.documentElement).getPropertyValue('--signal').trim();
          if (/^#[0-9a-f]{6}$/i.test(c)) rgb = [1, 3, 5].map(i => parseInt(c.slice(i, i + 2), 16)).join(','); } catch (e) {}
  }
  function pencil(p, al){
    const L = p.len, w = L * .24, e = w * .8, t = w * 1.25;
    cx.save(); cx.translate(p.x, p.y); cx.rotate(p.a);
    cx.lineWidth = 1.3; cx.lineJoin = 'round'; cx.strokeStyle = `rgba(${rgb},${al})`; cx.fillStyle = `rgba(${rgb},${al * .28})`;
    cx.beginPath(); cx.rect(-L / 2, -w / 2, e * .55, w); cx.fill(); cx.stroke();                 // ластик
    cx.beginPath(); cx.moveTo(-L / 2 + e * .55, -w / 2); cx.lineTo(-L / 2 + e * .55, w / 2);
    cx.moveTo(-L / 2 + e, -w / 2); cx.lineTo(-L / 2 + e, w / 2); cx.stroke();                     // металлическая вставка
    cx.beginPath(); cx.rect(-L / 2 + e, -w / 2, L - e - t, w); cx.stroke();                         // корпус
    cx.beginPath(); cx.moveTo(-L / 2 + e + 2, -w / 6); cx.lineTo(L / 2 - t - 2, -w / 6); cx.stroke();
    cx.beginPath(); cx.moveTo(L / 2 - t, -w / 2); cx.lineTo(L / 2, 0); cx.lineTo(L / 2 - t, w / 2); cx.closePath(); cx.stroke();  // заточка
    cx.beginPath(); cx.moveTo(L / 2 - t * .38, -w * .17); cx.lineTo(L / 2, 0); cx.lineTo(L / 2 - t * .38, w * .17); cx.closePath();
    cx.fillStyle = `rgba(${rgb},${Math.min(.9, al * 2)})`; cx.fill();                               // грифель
    cx.restore();
  }
  function frame(now){
    const dt = Math.min(.05, (now - last) / 1000); last = now;
    cx.clearRect(0, 0, W, H);
    glow = Math.max(0, glow - dt * 1.4);
    for (const p of items) {
      if (!calm) {
        const k = 1 + glow * 3;
        p.x += p.vx * dt * k; p.y += p.vy * dt * k; p.a += p.spin * dt * k;
        if (p.y < -50 || p.x > W + 50 || p.x < -50) Object.assign(p, make(false), {x: rnd(0, W)});
      }
      pencil(p, Math.min(.85, p.al * (1 + .25 * Math.sin(now / 1800 + p.ph)) + glow * .35));
    }
    requestAnimationFrame(frame);
  }
  addEventListener('resize', resize); resize(); requestAnimationFrame(frame);
  return {pulse(){ glow = Math.min(1, glow + .45); }};
})();
"""

# ======================================================================
# Звуки (синтезируются, файлов нет). Тот же AudioContext играет озвучку слов.
# ======================================================================
SND_JS = r"""
const Snd = (() => {
  let ac = null, vol = .5;
  const ctx = () => {
    if (!ac) {
      const C = window.AudioContext || window.webkitAudioContext; if (!C) return null;
      ac = new C(); ac.onstatechange = () => window.onAudioState && window.onAudioState(ac.state);
    }
    return ac;
  };
  function tone(f, t0, dur, type, gain){
    const c = ctx(); if (!c || c.state !== 'running') return;
    const o = c.createOscillator(), g = c.createGain(), t = c.currentTime + t0;
    o.type = type || 'sine'; o.frequency.value = f;
    g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime((gain || .2) * vol, t + .012);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    o.connect(g); g.connect(c.destination); o.start(t); o.stop(t + dur + .02);
  }
  return {
    ctx,
    tick(){ tone(900 + Math.random() * 160, 0, .06, 'triangle', .16); },
    enter(){ tone(660, 0, .12, 'sine', .22); tone(990, .09, .18, 'sine', .2); },
    error(){ tone(180, 0, .18, 'sawtooth', .12); },
    sparkle(){ [784, 988, 1175, 1568, 1976].forEach((f, i) => tone(f, i * .07, .3, 'sine', .16)); },
    finish(){ [523, 659, 784, 1047].forEach((f, i) => tone(f, i * .14, .5, 'triangle', .22)); tone(1568, .6, .8, 'sine', .18); },
  };
})();
"""

# ======================================================================
# Пульт ведущего
# ======================================================================
CONTROL_HTML = r"""<!doctype html>
<html lang="ru" data-theme="women" data-role="host" data-base="%%BASE%%">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Диктант — ведущий</title>
%%FONTS%%
<style>
%%THEME_CSS%%
body{background:radial-gradient(120% 60% at 0 0,var(--ink-2),var(--ink) 60%)}
.wrap{position:relative;z-index:1;max-width:560px;margin:0 auto;padding:16px 16px 40px}
header{display:flex;justify-content:space-between;align-items:center;margin:4px 0 14px}
.wordmark{font-size:20px}
.setlabel{padding:8px 16px;border-radius:999px;border:1px solid var(--signal);color:var(--signal);font-weight:800;font-size:14px}
.sets{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0 4px}
.sets button{text-align:left;padding:14px;border-radius:var(--r-m);border:1px solid var(--line);background:var(--ink-2);color:var(--chalk)}
.sets button b{display:block;font-family:var(--display);font-size:16px}
.sets button small{display:block;margin-top:6px;color:var(--mist);font-size:12px;line-height:1.35}
.sets button.on{border-color:var(--signal);background:var(--signal-soft);box-shadow:0 0 0 1px var(--signal)}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-l);padding:18px;margin-bottom:14px}
.muted{color:var(--mist);font-size:14px;line-height:1.4}
.btn{display:block;width:100%;border:0;border-radius:var(--r-m);padding:18px 14px;font-weight:800;font-size:17px;background:var(--ink-2);border:1px solid var(--line);touch-action:manipulation}
.btn.primary{background:var(--signal);color:var(--signal-ink);border-color:var(--signal)}
.btn.quiet{background:transparent;color:var(--chalk);font-weight:600}
.btn.armed{background:var(--warm);border-color:var(--warm);color:var(--signal-ink)}
.btn.big{padding:26px 14px;font-size:20px}
.btn:disabled{opacity:.45}
.two{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.stepper{display:grid;grid-template-columns:72px 1fr 72px;gap:10px;align-items:center;margin:12px 0 16px}
.stepper button{height:72px;border-radius:var(--r-m);border:1px solid var(--line);background:var(--ink-2);font-size:30px;font-weight:600}
.stepper output{text-align:center;font-family:var(--digits);font-weight:900;font-size:64px;color:var(--signal)}
.prog{display:flex;justify-content:space-between;color:var(--mist);font-weight:600;font-size:14px}
.word{font-family:var(--display);font-weight:800;font-size:clamp(28px,9vw,44px);letter-spacing:.04em;color:var(--signal);margin:10px 0 4px;word-break:break-all}
.who{font-size:20px;font-weight:800;margin-top:8px}
.live{min-height:2.2em;margin:8px 0 14px;padding:10px 14px;border-radius:var(--r-m);background:var(--ink-2);font-family:var(--digits);font-weight:900;font-size:26px;letter-spacing:.06em;word-break:break-all}
.live i{display:inline-block;width:2px;height:1em;background:var(--signal);margin-left:2px;vertical-align:-.12em;animation:blink 1s steps(1) infinite}
@keyframes blink{50%{opacity:0}}
.ans{padding:10px 0;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:10px;align-items:center}
.ans:last-child{border:0}
.ans .n{color:var(--mist);font-size:13px}
.ans .diff{font-size:22px}
.row{display:flex;justify-content:space-between;padding:11px 2px;border-bottom:1px solid var(--line)}
.row:last-child{border:0}
.row b{font-family:var(--digits);font-weight:900;color:var(--signal)}
.row.now{color:var(--signal)}
.status{padding:10px 14px;border-radius:var(--r-m);font-size:14px;font-weight:600;background:var(--ink-2);margin-bottom:14px}
.status.ok{color:var(--signal)}
.status.warn{color:var(--warm)}
.fields{display:grid;gap:8px;margin-top:10px}
.fields input{width:100%;min-width:0;background:var(--ink-2);border:1px solid var(--line);border-radius:var(--r-s);color:var(--chalk);font:inherit;font-weight:600;padding:12px 14px}
.fields input:focus{outline:2px solid var(--signal);outline-offset:1px}
.resetzone{margin-top:30px;padding-top:20px;border-top:1px dashed var(--line);text-align:center}
.reset-ask{text-align:left}
.reset-ask p{margin:0 0 14px;line-height:1.4}
.reset-ask p span{display:block;color:var(--mist);font-size:14px;margin-top:4px}
.reset-ask .btn{padding:15px 8px;font-size:16px}
.fb{min-height:1.4em;margin-top:8px;color:var(--mist);font-size:14px;text-align:center}
.fb.bad{color:var(--warm)}
</style></head>
<body>
<div class="offline" id="offline">Нет связи с сервером — переподключаюсь…</div>
<canvas id="bg"></canvas>
<div class="wrap">
  <header>
    <span class="wordmark"><i></i>ДИКТАНТ</span>
    <span class="setlabel" id="setLabel"></span>
  </header>
  <div class="status" id="scr">Подключаюсь…</div>

  <section class="card" id="idle" hidden>
    <b>Какой набор слов запускаем?</b>
    <div class="sets" id="sets">
      <button type="button" data-set="1" class="on"><b>НАБОР 1</b><small>%%SET1%%</small></button>
      <button type="button" data-set="2"><b>НАБОР 2</b><small>%%SET2%%</small></button>
    </div>
    <b style="display:block;margin-top:16px">Сколько участников?</b>
    <div class="stepper"><button id="minus" aria-label="Меньше">−</button><output id="count">4</output><button id="plus" aria-label="Больше">+</button></div>
    <button class="btn primary big" id="start">ЗАПУСТИТЬ НАБОР 1 НА ЭКРАН</button>
    <p class="muted" style="margin:12px 0 0">Слова идут в случайном порядке. Участники по очереди пишут каждое слово на клавиатуре гостевого экрана. Каждая ошибка — +1 к счёту, побеждает тот, у кого ошибок меньше.</p>
  </section>

  <section class="card" id="play" hidden>
    <div class="prog"><span id="prog"></span><span id="phaseLabel"></span></div>
    <div class="word" id="word"></div>
    <div id="typingBox" hidden>
      <div class="who" id="who"></div>
      <div class="live" id="live"></div>
    </div>
    <button class="btn primary big" id="sound">🔊 ОЗВУЧИТЬ СЛОВО</button>
    <div class="fb" id="fb"></div>
    <div id="revealBox" hidden style="margin-top:12px">
      <div id="answers"></div>
      <button class="btn primary big" id="next" style="margin-top:14px">СЛЕДУЮЩЕЕ СЛОВО</button>
    </div>
    <div class="two" id="typingBtns" style="margin-top:12px">
      <button class="btn quiet" id="skip">Не смог написать</button>
      <button class="btn quiet" id="back">Назад</button>
    </div>
  </section>

  <section class="card" id="board" hidden>
    <div class="prog" style="margin-bottom:6px"><b id="boardTitle">Ошибки</b><span>меньше — лучше</span></div>
    <div id="rows"></div>
  </section>

  <section class="card" id="finished" hidden>
    <b>Игра окончена</b>
    <button class="btn quiet" id="backFin" style="margin-top:12px">Вернуться на шаг назад</button>
  </section>

  <section class="card" id="tools" hidden>
    <button class="btn quiet" id="table">Показать таблицу на экране</button>
    <button class="btn quiet" id="namesToggle" style="margin-top:10px">Имена участников</button>
    <div class="fields" id="names" hidden></div>
    <button class="btn quiet" id="finish" style="margin-top:10px">Завершить досрочно</button>
  </section>

  <div class="resetzone" id="resetZone" hidden>
    <button class="btn quiet" id="resetOpen">Сбросить игру</button>
    <div class="reset-ask" id="resetAsk" hidden>
      <p id="resetQ">Сбросить игру?<span>Все участники и результаты этой игры удалятся.</span></p>
      <div class="two"><button class="btn quiet" id="resetNo">Отмена</button><button class="btn primary" id="resetYes">Да, сбросить</button></div>
    </div>
  </div>
</div>
<script>
%%CLIENT_JS%%
%%BG_JS%%
let count = 4, namesOpen = false, chosen = '1';

/* Подтверждения без системных окон: кнопка меняет подпись и ждёт второго нажатия. */
function disarm(btn){ if (!btn || !btn._armed) return; btn._armed = false; clearTimeout(btn._armT); btn.textContent = btn._orig; btn.classList.remove('armed'); }
function arm(btn, askText, run){
  if (btn._armed) { disarm(btn); run(); return; }
  btn._armed = true; btn._orig = btn.textContent; btn.textContent = askText; btn.classList.add('armed');
  btn._armT = setTimeout(() => disarm(btn), 4000);
}
$('minus').onclick = () => { count = Math.max(1, count - 1); setText($('count'), count); };
$('plus').onclick = () => { count = Math.min(12, count + 1); setText($('count'), count); };
function pickSet(id){
  chosen = id;
  document.querySelectorAll('#sets button').forEach(b => b.classList.toggle('on', b.dataset.set === id));
  setText($('start'), 'ЗАПУСТИТЬ НАБОР ' + id + ' НА ЭКРАН');
}
document.querySelectorAll('#sets button').forEach(b => { b.onclick = () => pickSet(b.dataset.set); });
$('start').onclick = () => emitGame('setup', {count, set: chosen});
$('next').onclick = () => emitGame('next');
$('skip').onclick = () => arm($('skip'), 'Точно пропустить?', () => emitGame('skip', {wi: S.wi, pi: S.pi}));
$('back').onclick = () => arm($('back'), 'Откатить последний ответ?', () => emitGame('back'));
$('backFin').onclick = () => arm($('backFin'), 'Вернуться в игру?', () => emitGame('back'));
$('finish').onclick = () => arm($('finish'), 'Точно завершить?', () => emitGame('finish'));
$('table').onclick = () => emitGame('table', {show: !(S && S.show_table)});
$('namesToggle').onclick = () => { namesOpen = !namesOpen; $('names').hidden = !namesOpen; setText($('namesToggle'), namesOpen ? 'Скрыть имена' : 'Имена участников'); };
function closeReset(){ $('resetAsk').hidden = true; $('resetOpen').hidden = false; }
$('resetOpen').onclick = () => { $('resetAsk').hidden = false; $('resetOpen').hidden = true; };
$('resetNo').onclick = closeReset;
$('resetYes').onclick = () => { closeReset(); emitGame('reset'); };

let fbT = 0;
function feedback(text, bad){
  const el = $('fb'); el.textContent = text; el.classList.toggle('bad', !!bad);
  clearTimeout(fbT); fbT = setTimeout(() => { el.textContent = ''; }, 5000);
}
$('sound').onclick = () => {
  const ok = emitGame('play', {}, ack => {
    if (!ack || !ack.ok) return feedback('Сейчас нечего озвучивать', true);
    if (!ack.screens) feedback('Экран гостей не открыт — звук некому играть', true);
    else if (!ack.audio) feedback('Отправлено, но на экране не включён звук: нажмите там любую клавишу', true);
    else feedback('Слово озвучено на экране');
  });
  if (!ok) feedback('Нет связи', true);
};

function render(){
  const s = S; if (!s) return;
  const ph = s.phase, fin = ph === 'finished', idle = ph === 'idle';
  $('idle').hidden = !idle;
  $('play').hidden = !(ph === 'typing' || ph === 'reveal');
  $('finished').hidden = !fin;
  $('board').hidden = idle;
  $('tools').hidden = idle;
  $('resetZone').hidden = idle;
  $('table').hidden = fin;
  $('finish').hidden = fin;
  if (idle) { namesOpen = false; $('names').hidden = true; closeReset(); }
  setText($('resetOpen'), fin ? 'Начать новый конкурс' : 'Сбросить игру');
  setText($('resetQ'), fin ? 'Начать новый конкурс?' : 'Сбросить игру?');
  setText($('resetYes'), fin ? 'Да, начать заново' : 'Да, сбросить');
  setText($('table'), s.show_table ? 'Скрыть таблицу на экране' : 'Показать таблицу на экране');

  const sc = s.screens || {count: 0, audio: 0};
  const scr = $('scr');
  scr.className = 'status ' + (sc.count && sc.audio ? 'ok' : 'warn');
  setText(scr, !sc.count ? 'Экран гостей не открыт' : !sc.audio ? 'Экран открыт, звук не включён — нажмите на нём любую клавишу' : 'Экран гостей готов, звук включён');

  setText($('setLabel'), s.set ? 'НАБОР ' + s.set : '');
  $('setLabel').hidden = !s.set;
  if (ph === 'typing' || ph === 'reveal') {
    setText($('prog'), 'Слово ' + (s.wi + 1) + ' из ' + s.total);
    setText($('phaseLabel'), ph === 'typing' ? 'идёт ввод' : 'разбор');
    setText($('word'), s.word || '');
    $('typingBox').hidden = ph !== 'typing';
    $('skip').hidden = ph !== 'typing';
    $('typingBtns').style.gridTemplateColumns = ph === 'typing' ? '1fr 1fr' : '1fr';
    $('revealBox').hidden = ph !== 'reveal';
    if (ph === 'typing') {
      setText($('who'), s.name + ' пишет');
      renderTyped();
    } else {
      setHtml($('answers'), (s.answers || []).map(a =>
        `<div class="ans"><div><div class="n">${esc(a.name)}</div><div class="diff">${a.typed ? diffHtml(a.ops) : '<span class="muted">без ответа</span>'}</div></div>${badge(a.errors)}</div>`).join(''));
      setText($('next'), s.wi + 1 >= s.total ? 'ПОДВЕСТИ ИТОГИ' : 'СЛЕДУЮЩЕЕ СЛОВО');
    }
  }
  const list = ranked(s.participants.map(p => ({name: p.name, errors: p.errors})));
  const rows = fin ? list : s.participants.map((p, i) => ({...p, i, place: 0}));
  setText($('boardTitle'), fin ? 'Итоги' : 'Ошибки');
  setHtml($('rows'), rows.map(p => {
    const now = ph === 'typing' && p.i === s.pi;
    return `<div class="row${now ? ' now' : ''}"><span>${fin ? p.place + '. ' : ''}${esc(p.name)}${now ? ' ✎' : ''}</span><b>${p.errors} ${pl(p.errors)}</b></div>`;
  }).join(''));
  // поля имён пересобираем только при смене числа участников и не трогаем то, что сейчас редактируют
  const box = $('names');
  if (box._n !== s.participants.length) {
    box._n = s.participants.length;
    box.innerHTML = s.participants.map((p, i) => `<input data-i="${i}" maxlength="32" value="${esc(p.name)}" aria-label="Имя участника ${i + 1}">`).join('');
    box.querySelectorAll('input').forEach(inp => { inp.onchange = () => emitGame('rename', {index: +inp.dataset.i, name: inp.value}); });
  } else box.querySelectorAll('input').forEach((inp, i) => { if (document.activeElement !== inp && inp.value !== s.participants[i].name) inp.value = s.participants[i].name; });
}
function renderTyped(){
  if (!S || S.phase !== 'typing') return;
  setHtml($('live'), esc(typed) + '<i></i>');
}
window.onState = s => { try { render(); } catch (e) { console.error(e); } };
</script>
</body></html>
"""

# ======================================================================
# Экран для гостей
# ======================================================================
SCREEN_HTML = r"""<!doctype html>
<html lang="ru" data-theme="women" data-role="screen" data-base="%%BASE%%">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Диктант</title>
%%FONTS%%
<style>
%%THEME_CSS%%
body{overflow:hidden;background:radial-gradient(60% 70% at 50% 55%,var(--signal-soft),transparent 70%),radial-gradient(90% 80% at 0 0,var(--ink-2),var(--ink) 65%)}
.screen{position:relative;z-index:1;height:100vh;height:100dvh;display:flex;flex-direction:column;padding:3vh 4vw}
.top{display:flex;justify-content:space-between;align-items:center;font-size:clamp(14px,1.6vw,26px);color:var(--mist);font-weight:600}
.top .wordmark{color:var(--chalk);font-size:clamp(18px,2vw,32px)}
.stage{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;min-height:0}
.big{font-family:var(--display);font-weight:900;font-size:min(10vw,16vh);line-height:1.05;color:var(--signal);filter:drop-shadow(0 0 40px var(--signal-soft))}
.sub{font-size:clamp(16px,2.2vw,34px);color:var(--mist);margin-top:2vh;font-weight:600}
.cnt{font-size:clamp(16px,2vw,32px);font-weight:800;letter-spacing:.12em;color:var(--mist)}
.who{font-family:var(--display);font-weight:800;font-size:min(5vw,9vh);margin:2vh 0 3vh}
.who span{color:var(--signal)}
.tile{min-width:min(70vw,900px);max-width:92vw;min-height:min(16vw,22vh);padding:2vh 3vw;border:2px solid var(--line);border-radius:var(--r-l);background:rgba(38,16,31,.82);display:flex;align-items:center;justify-content:center;
  font-family:var(--digits);font-weight:900;font-size:min(8vw,13vh);letter-spacing:.08em;word-break:break-all;box-shadow:0 0 60px var(--signal-soft)}
.tile i{display:inline-block;width:.07em;height:.9em;background:var(--signal);margin-left:.05em;animation:blink 1s steps(1) infinite}
.tile.shake{animation:shake .35s}
@keyframes blink{50%{opacity:0}}
@keyframes shake{25%{transform:translateX(-14px)}75%{transform:translateX(14px)}}
.hint{margin-top:3vh;color:var(--mist);font-size:clamp(14px,1.6vw,26px)}
.hint b{color:var(--chalk)}
.right{font-family:var(--display);font-weight:900;font-size:min(7vw,12vh);color:var(--signal);letter-spacing:.05em;margin-bottom:2vh;word-break:break-all}
.ansrows{width:min(92vw,1100px);display:grid;gap:1.1vh;overflow:hidden}
.arow{display:flex;justify-content:space-between;align-items:center;gap:2vw;padding:1.2vh 2vw;border:1px solid var(--line);border-radius:var(--r-m);background:rgba(38,16,31,.82);
  font-size:clamp(14px,min(2.4vw,4.4vh),40px);animation:pop .35s both}
.arow .nm{color:var(--mist);font-weight:600;text-align:left;flex:none;max-width:34%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.arow .diff{flex:1;text-align:left;font-size:1.25em}
@keyframes pop{from{opacity:0;transform:translateY(10px)}}
.strip{display:flex;flex-wrap:wrap;gap:1vh 1vw;justify-content:center;margin-top:2vh}
.chip{padding:.5em 1em;border:1px solid var(--line);border-radius:999px;background:rgba(38,16,31,.82);font-weight:600;font-size:clamp(13px,1.35vw,22px)}
.chip b{color:var(--signal);margin-left:.5em}
.chip.now{border-color:var(--signal);box-shadow:0 0 18px var(--signal-soft)}
.lb{width:min(92vw,900px);display:grid;gap:1vh}
.lrow{display:flex;align-items:center;gap:2vw;padding:1.3vh 2.4vw;border:1px solid var(--line);border-radius:var(--r-m);background:rgba(38,16,31,.85);font-size:clamp(16px,min(3vw,5.2vh),48px);font-weight:700;animation:pop .4s both}
.lrow .pl{font-family:var(--digits);font-weight:900;color:var(--mist);width:1.6em;text-align:left}
.lrow .nm{flex:1;text-align:left;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lrow .sc{font-family:var(--digits);font-weight:900;color:var(--signal);white-space:nowrap}
.lrow.win{border-color:var(--signal);background:rgba(255,79,168,.16);box-shadow:0 0 40px var(--signal-soft)}
.lrow.win .pl{color:var(--signal)}
.rule{margin-top:2.4vh;color:var(--mist);font-weight:800;letter-spacing:.1em;font-size:clamp(12px,1.4vw,22px)}
.overlay{position:fixed;inset:0;z-index:20;background:rgba(20,7,16,.94);display:flex;flex-direction:column;align-items:center;justify-content:center;padding:4vh 4vw}
.sound{position:fixed;right:20px;bottom:20px;z-index:50;border:1px solid var(--line);background:rgba(38,16,31,.92);color:var(--chalk);border-radius:999px;padding:12px 20px;font-weight:600;font-size:16px}
.sound:hover{border-color:var(--signal)}
</style></head>
<body>
<div class="offline" id="offline">Нет связи с сервером — переподключаюсь…</div>
<canvas id="bg"></canvas>
<div class="screen">
  <div class="top"><span class="wordmark"><i></i>ДИКТАНТ</span><span id="gameLabel"></span></div>
  <div class="stage" id="stage"></div>
</div>
<div class="overlay" id="tableOv" hidden><div class="cnt" style="margin-bottom:3vh">ТАБЛИЦА ОШИБОК</div><div class="lb" id="tableRows"></div><div class="rule">МЕНЬШЕ ОШИБОК — ВЫШЕ МЕСТО</div></div>
<button class="sound" id="soundBtn">🔈 Включить звук (любая клавиша)</button>
<script>
%%CLIENT_JS%%
%%BG_JS%%
%%SND_JS%%

/* ---------- звук: озвучка слов ---------- */
const bufs = {};
let lastSeq = 0;
function audioReady(){ const c = Snd.ctx(); return !!c && c.state === 'running'; }
function reportAudio(){ if (socket.connected) socket.emit('screen_audio', {ready: audioReady()}); setBtn(); }
function setBtn(){ $('soundBtn').hidden = audioReady(); }
async function unlock(){ const c = Snd.ctx(); if (c && c.state !== 'running') { try { await c.resume(); } catch (e) {} } reportAudio(); }
window.onAnyKey = unlock;
addEventListener('pointerdown', unlock);
$('soundBtn').onclick = unlock;
window.onAudioState = reportAudio;
window.onConnect = reportAudio;
async function load(url){
  if (bufs[url]) return bufs[url];
  const c = Snd.ctx(); const r = await fetch(BASE + url); const ab = await r.arrayBuffer();
  return (bufs[url] = await c.decodeAudioData(ab));
}
function speak(word){
  try { speechSynthesis.cancel(); const u = new SpeechSynthesisUtterance(word); u.lang = 'ru-RU'; u.rate = .8; speechSynthesis.speak(u); } catch (e) {}
}
async function playWord(url, word){
  try {
    const c = Snd.ctx(); if (c.state !== 'running') await c.resume();
    if (c.state !== 'running') throw new Error('locked');
    const src = c.createBufferSource(); src.buffer = await load(url); src.connect(c.destination); src.start();
  } catch (e) { speak(word); }            // файл не загрузился или звук заблокирован: пробуем голос браузера
}
socket.on('play', m => { if (!m || m.seq <= lastSeq) return; lastSeq = m.seq; playWord(m.audio, m.word); });

/* ---------- отрисовка ---------- */
const stage = $('stage');
let prevPhase = null, prevWi = -1;
function render(){
  const s = S; if (!s) return;
  const ph = s.phase;
  setText($('gameLabel'), ph === 'typing' || ph === 'reveal' ? 'СЛОВО ' + (s.wi + 1) + ' ИЗ ' + s.total : '');
  $('tableOv').hidden = !(s.show_table && ph !== 'idle' && ph !== 'finished');
  if (!$('tableOv').hidden) setHtml($('tableRows'), board(s, true));
  let html = '';
  if (ph === 'idle') {
    html = `<div class="big">ДИКТАНТ</div><div class="sub">Ждём ведущего</div>`;
  } else if (ph === 'typing') {
    html = `<div class="cnt">СЛУШАЙТЕ СЛОВО</div>
      <div class="who"><span>${esc(s.name)}</span>, пишите!</div>
      <div class="tile" id="tile"></div>
      <div class="hint"><b>Enter</b> — готово · <b>Backspace</b> — стереть</div>
      <div class="strip">${s.participants.map((p, i) => `<span class="chip${i === s.pi ? ' now' : ''}">${esc(p.name)}<b>${p.errors}</b></span>`).join('')}</div>`;
  } else if (ph === 'reveal') {
    html = `<div class="cnt">ПРАВИЛЬНО</div><div class="right">${esc(s.word || '')}</div>
      <div class="ansrows">${(s.answers || []).map((a, i) => `<div class="arow" style="animation-delay:${i * .08}s"><span class="nm">${esc(a.name)}</span><span class="diff">${a.typed ? diffHtml(a.ops) : '—'}</span>${badge(a.errors)}</div>`).join('')}</div>`;
  } else {
    html = `<div class="cnt">ИТОГИ</div><div class="who" style="margin:1vh 0 2.5vh">Победитель — <span>${esc(ranked(s.participants)[0] ? winners(s) : '')}</span></div>
      <div class="lb">${board(s, false)}</div><div class="rule">МЕНЬШЕ ОШИБОК — ВЫШЕ МЕСТО</div>`;
  }
  const key = ph + ':' + s.wi + ':' + s.pi + ':' + (ph === 'typing' ? '' : s.participants.map(p => p.errors).join(',')) + ':' + s.bump;
  if (stage._k !== key) { stage._k = key; stage.innerHTML = html; }
  renderTyped();
  if (ph === 'typing' && s.audio) load(s.audio).catch(() => {});         // заранее готовим озвучку
  // звуки только на живые переходы, а не при перезагрузке страницы
  if (prevPhase !== null && (ph !== prevPhase || s.wi !== prevWi)) {
    if (ph === 'finished') Snd.finish();
    else if (ph === 'reveal' && (s.answers || []).some(a => a.errors === 0)) Snd.sparkle();
  }
  prevPhase = ph; prevWi = s.wi;
}
function winners(s){
  const r = ranked(s.participants), top = r.filter(p => p.errors === r[0].errors);
  return top.map(p => p.name).join(' и ');
}
function board(s, partial){
  return ranked(s.participants).map((p, k) =>
    `<div class="lrow${p.place === 1 && (!partial || true) ? ' win' : ''}" style="animation-delay:${k * .07}s"><span class="pl">${p.place}</span><span class="nm">${esc(p.name)}</span><span class="sc">${p.errors} ${pl(p.errors)}</span></div>`).join('');
}
function renderTyped(){
  const tile = $('tile'); if (!tile || !S || S.phase !== 'typing') return;
  setHtml(tile, esc(typed) + '<i></i>');
}
window.onEmpty = () => { const t = $('tile'); if (t) { t.classList.remove('shake'); void t.offsetWidth; t.classList.add('shake'); } };
window.onState = s => { try { render(); } catch (e) { console.error(e); } };
setBtn();
</script>
</body></html>
"""

# ======================================================================
# Проверка озвучки перед конкурсом
# ======================================================================
CHECK_HTML = r"""<!doctype html>
<html lang="ru" data-theme="women" data-base="%%BASE%%">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Диктант — проверка озвучки</title>
%%FONTS%%
<style>
%%THEME_CSS%%
.wrap{max-width:640px;margin:0 auto;padding:20px 16px 40px}
h1{font-family:var(--display);font-size:22px}
h2{font-size:16px;color:var(--mist);margin:24px 0 8px}
p{color:var(--mist);line-height:1.4}
.r{display:flex;align-items:center;gap:12px;padding:10px 14px;border:1px solid var(--line);border-radius:var(--r-m);background:var(--surface);margin-bottom:8px;font-weight:700;font-size:18px}
.r button{width:48px;height:48px;border-radius:50%;border:0;background:var(--signal);color:var(--signal-ink);font-size:18px;flex:none}
.r span{flex:1}
.r small{color:var(--mist);font-weight:600}
</style></head>
<body><div class="wrap">
<h1>Проверка озвучки</h1>
<p>Нажмите ▶ у каждого слова и убедитесь, что звучит именно оно. Эту страницу гости не видят.</p>
<div id="list"></div>
</div>
<script>
const ROWS = %%ROWS%%;
const BASE = document.documentElement.dataset.base;
let html = '', g = '';
for (const r of ROWS) {
  if (r.set !== g) { g = r.set; html += `<h2>Набор ${g}</h2>`; }
  html += `<div class="r"><button data-id="${r.id}" aria-label="Озвучить ${r.word}">▶</button><span>${r.word}</span><small>${r.n}</small></div>`;
}
document.getElementById('list').innerHTML = html;
const au = new Audio();
document.getElementById('list').onclick = e => {
  const b = e.target.closest('button'); if (!b) return;
  au.src = BASE + 'audio/' + b.dataset.id + '.mp3'; au.play().catch(() => {});
};
</script></body></html>
"""

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), allow_unsafe_werkzeug=True)
