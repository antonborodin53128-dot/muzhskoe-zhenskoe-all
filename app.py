from flask import Flask, render_template_string, request
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.serving import run_simple
from balls_app import app as balls_app
from hamster_app import app as hamster_app
from voice_app import app as voice_app
from note_app import app as note_app
from kolcebros_app import app as kolcebros_app
from dictation_app import app as dictation_app
import io, base64
import qrcode


app=Flask(__name__)

CSS="""
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at 12% 45%,#082519 0,#040b08 38%,#030505 72%);color:#f5f6f2;font-family:Arial,sans-serif}
.wrap{max-width:1180px;margin:auto;padding:34px 28px 70px}.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:54px}
.logo{font-size:28px;font-weight:900}.m{border:2px solid #20ee78;padding:8px 12px}.slash{color:#20ee78}.fword{color:#ff4fa3}
h1{font-size:52px;margin:0 0 42px}.section{margin:42px 0}.title{font-size:22px;font-weight:900;margin-bottom:16px;color:#20ee78}.title.f{color:#ff4fa3}.title.mix{color:#eee}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:17px}.card{height:165px;border:1px solid #1c5c3a;border-radius:20px;background:#06150e;display:flex;align-items:center;justify-content:center;text-align:center;text-decoration:none;color:#fff;font-size:29px;font-weight:900;transition:.15s;box-shadow:inset 0 0 35px rgba(32,238,120,.02)}
.card:hover{transform:translateY(-3px);border-color:#20ee78;box-shadow:0 0 25px rgba(32,238,120,.12)}.card.f{border-color:#5b2340;background:#160912}.card.f:hover{border-color:#ff4fa3;box-shadow:0 0 25px rgba(255,79,163,.12)}
.card.mix{border-color:#51404d;background:linear-gradient(110deg,#07170f,#140912)}.disabled{opacity:.38;cursor:default}.disabled:hover{transform:none;box-shadow:none}
.menu{max-width:760px;margin:70px auto}.back{color:#8ca096;text-decoration:none;font-weight:900}.contest{font-size:58px;margin:35px 0 10px}.hint{color:#81958b;margin-bottom:35px}
.menuGrid{display:grid;gap:14px}.action{display:flex;align-items:center;justify-content:space-between;padding:23px 25px;border-radius:17px;border:1px solid #20543a;background:#07170f;color:#fff;text-decoration:none;font-size:21px;font-weight:900}
.action:hover{border-color:#20ee78}.action span{color:#20ee78}.action.setup{background:#101713;border-color:#3c4b43}.small{font-size:13px;color:#82968c;margin-top:5px;font-weight:normal}
.qrbox{margin-top:24px;padding:22px;border:1px solid #20543a;border-radius:17px;background:#07170f;display:flex;align-items:center;gap:22px}
.qrbox img{width:150px;height:150px;background:#fff;padding:8px;border-radius:12px}.qrtitle{font-size:19px;font-weight:900}.qrhint{font-size:13px;color:#82968c;margin-top:7px;line-height:1.4}
@media(max-width:520px){.qrbox{flex-direction:column;text-align:center}}
@media(max-width:800px){.grid{grid-template-columns:1fr}.card{height:120px}h1{font-size:39px}.contest{font-size:45px}}
"""
HOME="""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Мужское / Женское</title><style>{{css}}</style></head><body><div class="wrap">
<div class="top"><div class="logo"><span class="m">МУЖСКОЕ</span> <span class="slash">/</span> <span class="fword">ЖЕНСКОЕ</span></div></div>
<h1>КОНКУРСЫ</h1>
<div class="section"><div class="title">МУЖСКОЕ</div><div class="grid">
<a class="card" href="/contest/voice">VOICE METER</a><a class="card" href="/contest/balls">ШАРИКИ</a><a class="card" href="/contest/hamster">ХОМЯК</a>
</div></div>
<div class="section"><div class="title f">ЖЕНСКОЕ</div><div class="grid">
<a class="card f" href="/contest/note">ТОЧНО В НОТУ</a><a class="card f" href="/contest/kolcebros">КОЛЬЦЕБРОС</a><a class="card f" href="/contest/diktant">ДИКТАНТ</a>
</div></div>
<div class="section"><div class="title mix">МУЖЧИНА VS ЖЕНЩИНА</div><div class="grid"><div class="card mix disabled">ОБЩИЙ КОНКУРС</div></div></div>
</div></body></html>"""

MENU="""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{{name}}</title><style>{{css}}</style></head><body><div class="wrap"><div class="menu">
<a class="back" href="/">← НАЗАД К КОНКУРСАМ</a><div class="contest">{{name}}</div><div class="hint">Выберите нужный режим.</div><div class="menuGrid">
{% for a in actions %}<a class="action {{a.get('class','')}}" href="{{a.url}}" {% if a.get('new') %}target="_blank"{% endif %}><div>{{a.title}}{% if a.get('desc') %}<div class="small">{{a.desc}}</div>{% endif %}</div><span>→</span></a>{% endfor %}
</div>
<div class="qrbox"><img src="{{qr}}" alt="QR для ведущего"><div><div class="qrtitle">QR ДЛЯ ВЕДУЩЕГО</div><div class="qrhint">Отсканируйте телефоном, чтобы открыть страницу управления конкурсом.</div></div></div>
</div></div></body></html>"""


def qr_data(url):
    img=qrcode.make(url)
    buf=io.BytesIO()
    img.save(buf,format="PNG")
    return "data:image/png;base64,"+base64.b64encode(buf.getvalue()).decode("ascii")

def absolute(path):
    return request.url_root.rstrip("/") + path

@app.get("/")
def home(): return render_template_string(HOME,css=CSS)

@app.get("/contest/balls")
def balls_menu():
    return render_template_string(MENU,css=CSS,name="ШАРИКИ",qr=qr_data(absolute("/men/balls/")),actions=[
        {"title":"ВЕДУЩИЙ","url":"/men/balls/","desc":"Управление участниками, таймером и результатами"},
        {"title":"ГОСТЕВОЙ ЭКРАН","url":"/men/balls/screen","desc":"Экран для проектора","new":True},
    ])

@app.get("/contest/hamster")
def hamster_menu():
    return render_template_string(MENU,css=CSS,name="ХОМЯК",qr=qr_data(absolute("/men/hamster/")),actions=[
        {"title":"ВЕДУЩИЙ","url":"/men/hamster/","desc":"Управление конкурсом"},
        {"title":"ГОСТЕВОЙ ЭКРАН","url":"/men/hamster/screen","desc":"Игровой экран для участника / проектора","new":True},
    ])

@app.get("/contest/voice")
def voice_menu():
    return render_template_string(MENU,css=CSS,name="VOICE METER",qr=qr_data(absolute("/men/voice/")),actions=[
        {"title":"ВЕДУЩИЙ","url":"/men/voice/","desc":"Управление участниками, таймером и результатами"},
        {"title":"ГОСТЕВОЙ ЭКРАН","url":"/men/voice/screen","desc":"Экран для проектора со шкалой громкости. Слушает микрофон","new":True},
        {"title":"SETUP","url":"/men/voice/setup","desc":"Выбор аудиовхода и чувствительности. Открывать на компьютере с микрофоном","class":"setup","new":True},
    ])

@app.get("/contest/note")
def note_menu():
    return render_template_string(MENU,css=CSS,name="ТОЧНО В НОТУ",qr=qr_data(absolute("/women/note/")),actions=[
        {"title":"ВЕДУЩИЙ","url":"/women/note/","desc":"Управление участницами, таймером и результатами"},
        {"title":"ГОСТЕВОЙ ЭКРАН","url":"/women/note/screen","desc":"Экран для проектора с барьерами. Слушает микрофон","new":True},
        {"title":"SETUP","url":"/women/note/setup","desc":"Выбор аудиовхода, тюнер и проверка попадания. Открывать на компьютере с микрофоном","class":"setup","new":True},
    ])

@app.get("/contest/kolcebros")
def kolcebros_menu():
    return render_template_string(MENU,css=CSS,name="КОЛЬЦЕБРОС",qr=qr_data(absolute("/women/kolcebros/")),actions=[
        {"title":"ВЕДУЩИЙ","url":"/women/kolcebros/","desc":"Управление участниками и баллами"},
        {"title":"ГОСТЕВОЙ ЭКРАН","url":"/women/kolcebros/screen","desc":"Экран для проектора с результатами","new":True},
    ])

@app.get("/contest/diktant")
def diktant_menu():
    return render_template_string(MENU,css=CSS,name="ДИКТАНТ",qr=qr_data(absolute("/women/diktant/")),actions=[
        {"title":"ВЕДУЩИЙ","url":"/women/diktant/","desc":"Выбор набора слов, запуск на экран, кнопка «Озвучить слово»"},
        {"title":"ГОСТЕВОЙ ЭКРАН","url":"/women/diktant/screen","desc":"Экран с клавиатурой и колонками для проектора","new":True},
        {"title":"ПРОВЕРКА ОЗВУЧКИ","url":"/women/diktant/audio-check","desc":"Прослушать все 20 слов перед конкурсом","class":"setup","new":True},
    ])

application=DispatcherMiddleware(app,{
    "/men/balls":balls_app,
    "/men/hamster":hamster_app,
    "/men/voice":voice_app,
    "/women/note":note_app,
    "/women/kolcebros":kolcebros_app,
    "/women/diktant":dictation_app,
})
if __name__=="__main__":
    run_simple("0.0.0.0",5000,application,use_reloader=True)
