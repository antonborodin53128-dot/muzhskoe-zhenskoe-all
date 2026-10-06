"""Какой конкурс сейчас показан на едином гостевом экране (/screen).
Ведущий нажимает «Запустить на гостевом экране» в лаунчере, вкладка гостевого экрана сама переключается.
Процесс один (gunicorn workers=1), поэтому хватает памяти процесса."""
import time

SCREENS = {
    "voice": "/men/voice/screen",
    "balls": "/men/balls/screen",
    "hamster": "/men/hamster/screen",
    "note": "/women/note/screen",
    "kolcebros": "/women/kolcebros/screen",
    "diktant": "/women/diktant/screen",
    "final": "/mix/final/screen",
}
_state = {"key": None, "n": 0, "seen": 0.0}


def launch(key):
    if key not in SCREENS:
        return False
    if _state["key"] != key:
        _state["key"] = key; _state["n"] += 1
    return True


def clear():
    """Показать заставку между конкурсами."""
    if _state["key"] is not None:
        _state["key"] = None; _state["n"] += 1
    return True


def heartbeat():
    _state["seen"] = time.time()


def get_active():
    k = _state["key"]
    return {"key": k, "url": SCREENS.get(k), "n": _state["n"], "screen_online": time.time() - _state["seen"] < 6}
