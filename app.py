from flask import Flask, render_template_string, request
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.serving import run_simple
from balls_app import app as balls_app
from hamster_app import app as hamster_app
from voice_app import app as voice_app
from note_app import app as note_app
from kolcebros_app import app as kolcebros_app
from dictation_app import app as dictation_app
from final_app import app as final_app
import io, base64
import active
import qrcode


app=Flask(__name__)

CSS="""
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at 12% 45%,#082519 0,#040b08 38%,#030505 72%);color:#f5f6f2;font-family:Arial,sans-serif}
.wrap{max-width:1180px;margin:auto;padding:34px 28px 70px}.top{display:flex;flex-wrap:wrap;gap:16px;justify-content:space-between;align-items:center;margin-bottom:54px}
.logo{font-size:28px;font-weight:900}.m{border:2px solid #20ee78;padding:8px 12px}.slash{color:#20ee78}.fword{color:#ff4fa3}
h1{font-size:52px;margin:0 0 42px}.section{margin:42px 0}.title{font-size:22px;font-weight:900;margin-bottom:16px;color:#20ee78}.title.f{color:#ff4fa3}.title.mix{color:#eee}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:17px}.card{height:165px;border:1px solid #1c5c3a;border-radius:20px;background:#06150e;display:flex;align-items:center;justify-content:center;text-align:center;text-decoration:none;color:#fff;font-size:29px;font-weight:900;transition:.15s;box-shadow:inset 0 0 35px rgba(32,238,120,.02)}
.card:hover{transform:translateY(-3px);border-color:#20ee78;box-shadow:0 0 25px rgba(32,238,120,.12)}.card.f{border-color:#5b2340;background:#160912}.card.f:hover{border-color:#ff4fa3;box-shadow:0 0 25px rgba(255,79,163,.12)}
.card.mix{border-color:#51404d;background:linear-gradient(110deg,#07170f,#140912)}.disabled{opacity:.38;cursor:default}.disabled:hover{transform:none;box-shadow:none}
.menu{max-width:760px;margin:70px auto}.back{color:#8ca096;text-decoration:none;font-weight:900}.contest{font-size:58px;margin:35px 0 10px}.hint{color:#81958b;margin-bottom:35px}
.menuGrid{display:grid;gap:14px}.action{display:flex;align-items:center;justify-content:space-between;padding:23px 25px;border-radius:17px;border:1px solid #20543a;background:#07170f;color:#fff;text-decoration:none;font-size:21px;font-weight:900}
.action:hover{border-color:#20ee78}.action span{color:#20ee78}.action.setup{background:#101713;border-color:#3c4b43}.small{font-size:13px;color:#82968c;margin-top:5px;font-weight:normal}
.launch{margin-top:26px}.launch button{width:100%;padding:22px;border:2px solid #2bf08a;border-radius:17px;background:#07170f;color:#2bf08a;font:900 19px Arial,sans-serif;cursor:pointer}.launch button.done{background:#2bf08a;color:#02140a}.launch button:disabled{opacity:.6}.launchhint{margin-top:10px;text-align:center;font-size:13px;color:#82968c}.launchhint.warn{color:#e8b24a}
.qrbox{margin-top:24px;padding:22px;border:1px solid #20543a;border-radius:17px;background:#07170f;display:flex;align-items:center;gap:22px}
.qrbox img{width:150px;height:150px;background:#fff;padding:8px;border-radius:12px}.qrtitle{font-size:19px;font-weight:900}.qrhint{font-size:13px;color:#82968c;margin-top:7px;line-height:1.4}
.toplinks{min-width:0;display:flex;gap:6px 22px;align-items:center;flex-wrap:wrap;justify-content:flex-end}.splashcard{font:inherit;color:inherit;cursor:pointer;font-size:inherit;border:1px solid #51404d}.splashcard.done{border-color:#2bf08a;box-shadow:0 0 25px rgba(43,240,138,.18)}.splashhint{align-self:center;color:#82968c;font-size:14px;line-height:1.4;grid-column:span 2}.splashhint.warn{color:#e8b24a}
.qrlink{color:#9db0a5;font-size:15px;font-weight:700;text-decoration:underline;text-underline-offset:4px}.qrlink:hover{color:#fff}
.qrov{position:fixed;inset:0;z-index:50;display:none;align-items:center;justify-content:center;padding:16px;background:rgba(3,6,5,.8);backdrop-filter:blur(6px)}.qrov.on{display:flex}
.qrmodal{width:min(360px,100%);text-align:center;padding:24px;border:1px solid #20543a;border-radius:20px;background:#07170f}.qrmodal img{display:block;width:240px;max-width:100%;height:auto;margin:16px auto;background:#fff;padding:10px;border-radius:14px}
.qrmodal button{margin-top:18px;width:100%;padding:13px;border:1px solid #3c4b43;border-radius:12px;background:none;color:#fff;font:700 16px Arial,sans-serif;cursor:pointer}
@media(max-width:520px){.logo{font-size:16px;white-space:nowrap}.m{padding:6px 8px}}
@media(max-width:520px){.qrbox{flex-direction:column;text-align:center}}
@media(max-width:800px){.grid{grid-template-columns:1fr}.card{height:120px}h1{font-size:39px}.contest{font-size:45px}}
"""
HOME="""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Мужское / Женское</title><style>{{css}}</style></head><body><div class="wrap">
<div class="top"><div class="logo"><span class="m">МУЖСКОЕ</span> <span class="slash">/</span> <span class="fword">ЖЕНСКОЕ</span></div>
<div class="toplinks"><a class="qrlink" href="/screen" target="_blank" rel="noopener">Гостевой экран</a><a class="qrlink" id="splashBtn" href="#" role="button">Вывести заставку на гостевой экран</a><a class="qrlink" id="qrOpen" href="#" role="button">QR для ведущего</a></div></div>
<h1>КОНКУРСЫ</h1>
<div class="section"><div class="title">МУЖСКОЕ</div><div class="grid">
<a class="card" href="/contest/voice">VOICE METER</a><a class="card" href="/contest/balls">ШАРИКИ</a><a class="card" href="/contest/hamster">ХОМЯК</a>
</div></div>
<div class="section"><div class="title f">ЖЕНСКОЕ</div><div class="grid">
<a class="card f" href="/contest/note">ТОЧНО В НОТУ</a><a class="card f" href="/contest/kolcebros">КОЛЬЦЕБРОС</a><a class="card f" href="/contest/diktant">ДИКТАНТ</a>
</div></div>
<div class="section"><div class="title mix">МУЖЧИНА VS ЖЕНЩИНА</div><div class="grid"><a class="card mix" href="/contest/final">ФИНАЛ</a></div></div>
</div>
<div class="qrov" id="qrOv"><div class="qrmodal"><div class="qrtitle">QR ДЛЯ ВЕДУЩЕГО</div><img src="{{qr}}" alt="QR для ведущего"><div class="qrhint">Отсканируйте телефоном, чтобы открыть эту страницу с конкурсами</div><button type="button" id="qrClose">Закрыть</button></div></div>
<script>(function(){var o=document.getElementById('qrOv');function s(v){o.classList.toggle('on',v)}
document.getElementById('qrOpen').addEventListener('click',function(e){e.preventDefault();s(true)});
document.getElementById('qrClose').addEventListener('click',function(){s(false)});
o.addEventListener('click',function(e){if(e.target===o)s(false)});
addEventListener('keydown',function(e){if(e.key==='Escape')s(false)});})();</script>
<script>(function(){var b=document.getElementById('splashBtn');
function paint(a){var on=!a.key&&a.screen_online;b.textContent=on?'✓ Заставка выведена':'Вывести заставку на гостевой экран';b.title=a.screen_online?'':'Гостевой экран не открыт'}
function load(){fetch('/api/active',{cache:'no-store'}).then(function(r){return r.json()}).then(paint).catch(function(){})}
b.addEventListener('click',function(e){e.preventDefault();fetch('/api/launch/splash',{method:'POST'}).then(load).catch(function(){})});
load();setInterval(load,3000)})();</script>
</body></html>"""

MENU="""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{{name}}</title><style>{{css}}</style></head><body><div class="wrap"><div class="menu">
<a class="back" href="/">← НАЗАД К КОНКУРСАМ</a><div class="contest">{{name}}</div><div class="hint">Выберите нужный режим.</div><div class="menuGrid">
{% for a in actions %}<a class="action {{a.get('class','')}}" href="{{a.url}}" {% if a.get('new') %}target="_blank"{% endif %}><div>{{a.title}}{% if a.get('desc') %}<div class="small">{{a.desc}}</div>{% endif %}</div><span>→</span></a>{% endfor %}
</div>
<div class="qrbox"><img src="{{qr}}" alt="QR для ведущего"><div><div class="qrtitle">QR ДЛЯ ВЕДУЩЕГО</div><div class="qrhint">Отсканируйте телефоном, чтобы открыть страницу управления конкурсом.</div></div></div>
</div>
<div class="launch" id="launch" data-key="{{key}}"><button type="button" id="launchBtn">Запустить на гостевом экране</button><div class="launchhint" id="launchHint"></div></div>
</div><script>(function(){var box=document.getElementById('launch'),btn=document.getElementById('launchBtn'),hint=document.getElementById('launchHint'),key=box.dataset.key;
function paint(a){var on=a.key===key;btn.classList.toggle('done',on);btn.textContent=on?'✓ Запущено на гостевом экране':'Запустить на гостевом экране';
 hint.textContent=a.screen_online?(on?'Гостевой экран показывает этот конкурс.':'Гостевой экран подключён.'):'Гостевой экран не открыт. Откройте его на главной странице («Гостевой экран»).';
 hint.className='launchhint'+(a.screen_online?'':' warn')}
function load(){fetch('/api/active',{cache:'no-store'}).then(function(r){return r.json()}).then(paint).catch(function(){})}
btn.addEventListener('click',function(){btn.disabled=true;fetch('/api/launch/'+key,{method:'POST'}).then(function(){btn.disabled=false;load()}).catch(function(){btn.disabled=false})});
load();setInterval(load,3000)})();</script></body></html>"""


SCREEN_SHELL = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Гостевой экран</title>
<style>*{box-sizing:border-box}html,body{margin:0;height:100%;background:#040b08;color:#f5f6f2;font-family:Arial,sans-serif;overflow:hidden}
iframe{position:fixed;inset:0;width:100%;height:100%;border:0;background:#040b08}
.wait{position:fixed;inset:0;display:grid;place-items:center;text-align:center;overflow:hidden;background:#030606}
.wait .glow{position:absolute;inset:0;background:radial-gradient(ellipse 75% 120% at -8% 50%,rgba(32,238,120,.30),rgba(32,238,120,.10) 45%,transparent 75%),radial-gradient(ellipse 75% 120% at 108% 50%,rgba(255,79,163,.28),rgba(255,79,163,.09) 45%,transparent 75%)}
.beam{position:absolute;bottom:-12vh;width:34vw;height:135vh;opacity:.5;filter:blur(3px);mix-blend-mode:screen;pointer-events:none}
.beam::before{content:'';position:absolute;inset:0;clip-path:polygon(44% 100%,56% 100%,100% 0,0 0)}
.beam.g{left:-6vw;transform-origin:50% 100%;--c:32,238,120}.beam.p{right:-6vw;transform-origin:50% 100%;--c:255,79,163}
.beam::before{background:linear-gradient(to top,rgba(var(--c),.95),rgba(var(--c),.28) 55%,rgba(var(--c),0))}
.corner{position:absolute;top:-34vh;width:90vw;height:78vh;border-radius:50%;pointer-events:none}
.corner.g{left:-26vw;transform-origin:30% 50%;background:radial-gradient(ellipse at center,rgba(32,238,120,.36),rgba(32,238,120,.13) 42%,transparent 70%);animation:cpulse 10s ease-in-out infinite}
.corner.p{right:-26vw;transform-origin:70% 50%;background:radial-gradient(ellipse at center,rgba(255,79,163,.34),rgba(255,79,163,.12) 42%,transparent 70%);animation:cpulse 10s ease-in-out -5s infinite}
@keyframes cpulse{0%,100%{transform:scale(.7);opacity:.55}50%{transform:scale(1.2);opacity:1}}
.beam.g.a{animation:swgA 17s ease-in-out infinite}.beam.g.b{animation:swgB 24s ease-in-out infinite;opacity:.4}
.beam.p.a{animation:swpA 19s ease-in-out infinite}.beam.p.b{animation:swpB 26s ease-in-out infinite;opacity:.4}
@keyframes swgA{0%,100%{transform:rotate(8deg)}50%{transform:rotate(52deg)}}
@keyframes swgB{0%,100%{transform:rotate(60deg)}50%{transform:rotate(16deg)}}
@keyframes swpA{0%,100%{transform:rotate(-8deg)}50%{transform:rotate(-52deg)}}
@keyframes swpB{0%,100%{transform:rotate(-60deg)}50%{transform:rotate(-16deg)}}
.wait>div:not(.glow):not(.beam):not(.corner){position:relative;z-index:2}
@media (prefers-reduced-motion:reduce){.beam{animation:none!important}}
.logo{font-size:min(7vw,90px);font-weight:900}.m{border:.06em solid #20ee78;padding:.12em .3em}.sl{color:#20ee78}.f{color:#ff4fa3}
.sub{margin-top:3vh;color:#81958b;font-size:min(2.4vw,30px)}
.start{position:fixed;inset:0;z-index:5;display:grid;place-items:start center;padding-top:68vh}
.start button{border:0;border-radius:999px;padding:.7em 1.6em;font:800 min(3vw,38px) Arial,sans-serif;background:linear-gradient(100deg,#2bf08a,#ff4fa3);color:#10120f;cursor:pointer}
.start p{color:#81958b;max-width:32em;margin:18px auto 0;text-align:center;line-height:1.45}.start p.w{color:#e8b24a}</style></head><body>
<div class="wait" id="wait"><div class="glow"></div><div class="beam g a"></div><div class="beam g b"></div><div class="beam p a"></div><div class="beam p b"></div><div class="corner g"></div><div class="corner p"></div><div><div class="logo"><span class="m">МУЖСКОЕ</span> <span class="sl">/</span> <span class="f">ЖЕНСКОЕ</span></div></div></div>
<iframe id="fr" title="Гостевой экран" allow="autoplay; microphone; fullscreen" hidden></iframe>
<div class="start" id="start"><div style="text-align:center"><div><button id="go" type="button">Включить звук</button></div><p id="hint" hidden></p></div></div>
<script>
(function(){var fr=document.getElementById('fr'),wait=document.getElementById('wait'),cur='',AC=null;
function unlock(){try{AC=AC||new (window.AudioContext||window.webkitAudioContext)();AC.resume();var b=AC.createBuffer(1,1,22050),s=AC.createBufferSource();s.buffer=b;s.connect(AC.destination);s.start(0)}catch(e){}}
function poke(){try{var d=fr.contentDocument;if(!d)return;
 ['soundBtn','snd'].forEach(function(id){var b=d.getElementById(id);if(b&&b.offsetParent!==null)b.click()});
 d.dispatchEvent(new Event('pointerdown'))}catch(e){}}
fr.addEventListener('load',function(){poke();setTimeout(poke,700);setTimeout(poke,2000)});
setInterval(function(){if(!fr.hidden)poke()},4000);
function apply(a){var k=a.key||'';if(k===cur)return;cur=k;
 if(!k){fr.hidden=true;fr.removeAttribute('src');wait.style.display='grid';return}
 wait.style.display='none';fr.hidden=false;fr.src=a.url}
function poll(){fetch('/api/active?hb=1',{cache:'no-store'}).then(function(r){return r.json()}).then(apply).catch(function(){})}
async function mic(){try{var s=await navigator.mediaDevices.getUserMedia({audio:true});s.getTracks().forEach(function(t){t.stop()});return true}catch(e){return false}}
async function wake(){try{if(navigator.wakeLock){var l=await navigator.wakeLock.request('screen');document.addEventListener('visibilitychange',function(){if(document.visibilityState==='visible')navigator.wakeLock.request('screen').catch(function(){})})}}catch(e){}}
document.getElementById('go').addEventListener('click',async function(){
 if(failed){go();return}
 var btn=this;btn.disabled=true;unlock();wake();
 try{document.documentElement.requestFullscreen&&document.documentElement.requestFullscreen()}catch(e){}
 var ok=await mic();
 if(!ok){var h=document.getElementById('hint');h.hidden=false;h.className='w';h.textContent='Доступ к микрофону не получен. Конкурсы «Voice meter» и «Точно в ноту» без него не заработают: разрешите микрофон в настройках сайта (значок замка в адресной строке) и обновите страницу. Остальные конкурсы можно запускать.';btn.disabled=false;btn.textContent='Продолжить без микрофона';failed=true;return}
 go()});
try{if(localStorage.getItem('gs_started')){document.getElementById('go').textContent='Включить звук'}}catch(e){}
var failed=false;function go(){try{localStorage.setItem('gs_started','1')}catch(e){}document.getElementById('start').style.display='none';unlock();poll();setInterval(poll,1000)}
})();
</script></body></html>"""


# Клавиатура для «Хомяка» работает на любой странице сборника: лаунчер, меню конкурсов, единый гостевой экран.
# Буква уходит на сервер Хомяка; тот засчитывает её только пока идёт игра, в остальное время игнорирует.
HAMSTER_KEYS = r"""<script>(function(){var q=Promise.resolve();   // нажатия уходят строго по очереди, чтобы не перепутались по дороге
addEventListener('keydown',function(e){
if(e.repeat||e.ctrlKey||e.metaKey||e.altKey)return;var t=e.target;
if(t&&(t.isContentEditable||/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)))return;
var m=/^Key([A-Z])$/.exec(e.code);if(!m)return;
try{var f=document.getElementById('fr'),w=f&&!f.hidden&&f.contentWindow;if(w&&typeof w.pressKey==='function'){w.pressKey(m[1]);return}}catch(x){}
var body=JSON.stringify({letter:m[1]});
q=q.then(function(){return fetch('/men/hamster/api/key',{method:'POST',headers:{'Content-Type':'application/json'},body:body,keepalive:true})}).catch(function(){})})})();</script>"""
HOME=HOME.replace("</body></html>",HAMSTER_KEYS+"</body></html>")
MENU=MENU.replace("</body></html>",HAMSTER_KEYS+"</body></html>")
SCREEN_SHELL=SCREEN_SHELL.replace("</body></html>",HAMSTER_KEYS+"</body></html>")


def qr_data(url):
    img=qrcode.make(url)
    buf=io.BytesIO()
    img.save(buf,format="PNG")
    return "data:image/png;base64,"+base64.b64encode(buf.getvalue()).decode("ascii")

def absolute(path):
    return request.url_root.rstrip("/") + path

@app.get("/api/active")
def api_active():
    from flask import jsonify
    if request.args.get("hb"): active.heartbeat()
    r = jsonify(active.get_active()); r.headers["Cache-Control"] = "no-store"; return r

@app.post("/api/launch/<key>")
def api_launch(key):
    from flask import jsonify
    ok = active.clear() if key == "splash" else active.launch(key)
    return jsonify(ok=ok), (200 if ok else 404)

@app.get("/screen")
def unified_screen():
    r = app.make_response(SCREEN_SHELL); r.headers["Cache-Control"] = "no-store"; return r

@app.get("/")
def home(): return render_template_string(HOME,css=CSS,qr=qr_data(absolute("/")))

@app.get("/contest/balls")
def balls_menu():
    return render_template_string(MENU,key=request.path.split('/')[2],css=CSS,name="ШАРИКИ",qr=qr_data(absolute("/men/balls/")),actions=[
        {"title":"ВЕДУЩИЙ","url":"/men/balls/","desc":"Управление участниками, таймером и результатами"},
        {"title":"ГОСТЕВОЙ ЭКРАН","url":"/men/balls/screen","desc":"Экран для проектора","new":True},
    ])

@app.get("/contest/hamster")
def hamster_menu():
    return render_template_string(MENU,key=request.path.split('/')[2],css=CSS,name="ХОМЯК",qr=qr_data(absolute("/men/hamster/")),actions=[
        {"title":"ВЕДУЩИЙ","url":"/men/hamster/","desc":"Управление конкурсом"},
        {"title":"ГОСТЕВОЙ ЭКРАН","url":"/men/hamster/screen","desc":"Игровой экран для участника / проектора","new":True},
    ])

@app.get("/contest/voice")
def voice_menu():
    return render_template_string(MENU,key=request.path.split('/')[2],css=CSS,name="VOICE METER",qr=qr_data(absolute("/men/voice/")),actions=[
        {"title":"ВЕДУЩИЙ","url":"/men/voice/","desc":"Управление участниками, таймером и результатами"},
        {"title":"ГОСТЕВОЙ ЭКРАН","url":"/men/voice/screen","desc":"Экран для проектора со шкалой громкости. Слушает микрофон","new":True},
        {"title":"SETUP","url":"/men/voice/setup","desc":"Выбор аудиовхода и усиления микрофона. Открывать на компьютере с микрофоном","class":"setup","new":True},
    ])

@app.get("/contest/note")
def note_menu():
    return render_template_string(MENU,key=request.path.split('/')[2],css=CSS,name="ТОЧНО В НОТУ",qr=qr_data(absolute("/women/note/")),actions=[
        {"title":"ВЕДУЩИЙ","url":"/women/note/","desc":"Управление участницами, таймером и результатами"},
        {"title":"ГОСТЕВОЙ ЭКРАН","url":"/women/note/screen","desc":"Экран для проектора с барьерами. Слушает микрофон","new":True},
        {"title":"SETUP","url":"/women/note/setup","desc":"Выбор аудиовхода, тюнер и проверка попадания. Открывать на компьютере с микрофоном","class":"setup","new":True},
    ])

@app.get("/contest/kolcebros")
def kolcebros_menu():
    return render_template_string(MENU,key=request.path.split('/')[2],css=CSS,name="КОЛЬЦЕБРОС",qr=qr_data(absolute("/women/kolcebros/")),actions=[
        {"title":"ВЕДУЩИЙ","url":"/women/kolcebros/","desc":"Управление участниками и баллами"},
        {"title":"ГОСТЕВОЙ ЭКРАН","url":"/women/kolcebros/screen","desc":"Экран для проектора с результатами","new":True},
    ])

@app.get("/contest/diktant")
def diktant_menu():
    return render_template_string(MENU,key=request.path.split('/')[2],css=CSS,name="ДИКТАНТ",qr=qr_data(absolute("/women/diktant/")),actions=[
        {"title":"ВЕДУЩИЙ","url":"/women/diktant/","desc":"Выбор набора слов, запуск на экран, кнопка «Озвучить слово»"},
        {"title":"ГОСТЕВОЙ ЭКРАН","url":"/women/diktant/screen","desc":"Экран с клавиатурой и колонками для проектора","new":True},
        {"title":"ПРОВЕРКА ОЗВУЧКИ","url":"/women/diktant/audio-check","desc":"Прослушать все 20 слов перед конкурсом","class":"setup","new":True},
    ])

@app.get("/contest/final")
def final_menu():
    return render_template_string(MENU,key=request.path.split('/')[2],css=CSS,name="ФИНАЛ",qr=qr_data(absolute("/mix/final/")),actions=[
        {"title":"ВЕДУЩИЙ","url":"/mix/final/","desc":"Категории, выбор Man/Woman, Правильно/Ошибка, ответ и 21 очко"},
        {"title":"ГОСТЕВОЙ ЭКРАН","url":"/mix/final/screen","desc":"Экран для проектора: 5 категорий, вопросы, музыка, счёт","new":True},
    ])

application=DispatcherMiddleware(app,{
    "/men/balls":balls_app,
    "/men/hamster":hamster_app,
    "/men/voice":voice_app,
    "/women/note":note_app,
    "/women/kolcebros":kolcebros_app,
    "/women/diktant":dictation_app,
    "/mix/final":final_app,
})
if __name__=="__main__":
    run_simple("0.0.0.0",5000,application,use_reloader=True)
