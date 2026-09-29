from flask import Flask, request, jsonify, render_template_string
import os, time, random, string
from threading import Lock

app = Flask(__name__)
lock = Lock()
PREP = 5
ROUND = 30
state = {"participants":[],"current":-1,"finished":False,"phase":"idle","started":None,"letter":None,"wrong":0}

def tick():
    now=time.time()
    if state["phase"]=="prep" and now-state["started"]>=PREP:
        state["phase"]="play"; state["started"]=now; state["letter"]=random.choice(string.ascii_uppercase.replace("O",""))
    elif state["phase"]=="play" and now-state["started"]>=ROUND:
        state["phase"]="timeup"; state["letter"]=None

def snap():
    tick()
    d={**state,"participants":[dict(p) for p in state["participants"]]}
    if state["phase"]=="prep": d["remaining"]=max(0,PREP-(time.time()-state["started"]))
    elif state["phase"]=="play": d["remaining"]=max(0,ROUND-(time.time()-state["started"]))
    else: d["remaining"]=0
    return d

CONTROL = """
<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Хомяк — управление</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#020b07;color:#f4f5ef;font-family:Arial,sans-serif}
body:before{content:"";position:fixed;inset:0;background:radial-gradient(circle at 0 25%,rgba(0,255,115,.15),transparent 36%);pointer-events:none}
.wrap{max-width:950px;margin:auto;padding:22px}.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:22px}
.brand{font-size:25px;font-weight:900}.m{border:2px solid #20ee78;padding:7px 10px}.slash,.accent,.value{color:#20ee78}.w{color:#747d78}
.panel{border:1px solid #174b30;background:#081810;border-radius:22px;padding:22px;margin-bottom:18px}.row{display:flex;gap:14px;align-items:end;flex-wrap:wrap}
.label{color:#84968c;font-size:13px;font-weight:900;letter-spacing:2px}input{width:150px;background:#07110c;border:1px solid #22543a;border-radius:14px;color:white;padding:14px;font-size:20px}
button{border:0;border-radius:14px;padding:15px 22px;font-size:16px;font-weight:900;cursor:pointer}.green{background:#20eb72;color:#001b0d}.red{background:#3b171d;color:#ff9da9}.muted{background:#18231d;color:#cbd5cf;border:1px solid #405047}
.game{display:none}.game.on{display:block}.name{font-size:36px;font-weight:900;margin:8px 0 20px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
.stat{background:#06140c;border:1px solid #16452c;border-radius:18px;padding:18px}.value{font-size:54px;font-weight:900;margin-top:6px}.letter{font-size:82px}
.start{width:100%;margin-top:16px;font-size:22px}.start:disabled{opacity:.35}.phase{font-size:24px;font-weight:900;margin-top:15px}
.nextZone{margin-top:34px;padding-top:20px;border-top:1px solid #174b30;display:flex;justify-content:flex-end}.next{min-width:270px}
.doneRow{display:flex;justify-content:space-between;border-bottom:1px solid #10281b;padding:10px 4px}.doneRow b{color:#20ee78}.hint{color:#82958a;font-size:13px;margin-top:13px}
</style></head><body><div class="wrap">
<div class="top"><div class="brand"><span class="m">МУЖСКОЕ</span> <span class="slash">/</span> <span class="w">ЖЕНСКОЕ</span></div><b class="accent">ХОМЯК · УПРАВЛЕНИЕ</b></div>
<div class="panel"><div class="row"><div><div class="label">КОЛИЧЕСТВО УЧАСТНИКОВ</div><input id="count" type="number" min="1" max="30" value="4"></div>
<button class="green" id="new">НАЧАТЬ КОНКУРС</button><button class="red" id="reset">СБРОСИТЬ</button></div>
<div class="hint">Зрительский экран: <a href="/men/hamster/screen" target="_blank" style="color:#20ee78">открыть /screen</a> · A–Z работают на обеих страницах.</div></div>
<div class="panel game" id="game"><div class="label">СЕЙЧАС ИГРАЕТ</div><div class="name" id="name"></div>
<div class="grid"><div class="stat"><div class="label">БУКВА</div><div class="value letter" id="letter">—</div></div><div class="stat"><div class="label">ВРЕМЯ</div><div class="value" id="timer">30</div></div><div class="stat"><div class="label">РЕЗУЛЬТАТ</div><div class="value" id="score">0</div></div></div>
<div class="phase" id="phase">ГОТОВ</div><button class="green start" id="go">СТАРТ</button>
<div class="nextZone"><button class="muted next" id="next">СЛЕДУЮЩИЙ УЧАСТНИК →</button></div><div class="label" style="margin-top:18px">УЖЕ СЫГРАЛИ</div><div id="done"></div></div></div>
<script>
const $=x=>document.getElementById(x);
async function post(u,b={}){return fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}).then(r=>r.json())}
$('new').onclick=()=>post('/men/hamster/api/start',{count:+$('count').value}).then(render);$('reset').onclick=()=>post('/men/hamster/api/reset').then(render);$('go').onclick=()=>post('/men/hamster/api/go').then(render);$('next').onclick=()=>post('/men/hamster/api/next').then(render);
document.addEventListener('keydown',e=>{if(!e.repeat&&/^Key[A-Z]$/.test(e.code)){e.preventDefault();post('/men/hamster/api/key',{letter:e.code.slice(3)}).then(render)}});
function render(s){let has=s.participants.length>0;$('game').classList.toggle('on',has);if(!has)return;let valid=s.current>=0&&s.current<s.participants.length,p=valid?s.participants[s.current]:null;if(p){$('name').textContent=p.name;$('score').textContent=p.score}
$('letter').textContent=s.phase==='play'?(s.letter||'—'):'—';let r=Math.max(0,Math.ceil(s.remaining));$('timer').textContent=s.phase==='play'?r:'30';
$('phase').textContent=s.phase==='prep'?'ПРИГОТОВЬТЕСЬ · '+r:s.phase==='play'?'ИГРА!':s.phase==='timeup'?'ВРЕМЯ!':s.finished?'КОНКУРС ЗАВЕРШЁН':'ГОТОВ';$('go').disabled=s.phase!=='idle'||s.finished;
let a=s.participants.filter((p,i)=>s.finished||i<s.current);$('done').innerHTML=a.map(p=>`<div class="doneRow"><span>${p.name}</span><b>${p.score}</b></div>`).join('')}
async function poll(){try{render(await fetch('/men/hamster/api/state?_='+Date.now(),{cache:'no-store'}).then(r=>r.json()))}catch(e){}setTimeout(poll,150)}poll();
</script></body></html>
"""

SCREEN = """
<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Хомяк</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#020b07;color:#f4f5ef;font-family:Arial,sans-serif}.wrap{padding:28px 5vw}.top{display:flex;justify-content:space-between;align-items:center}.brand{font-size:25px;font-weight:900}.m{border:2px solid #20ee78;padding:7px 10px}.slash,.accent{color:#20ee78}.w{color:#747d78}
.stage{min-height:560px;display:grid;grid-template-columns:1fr 1fr;align-items:center;gap:30px}.name{font-size:clamp(42px,5vw,82px);font-weight:900}.status{color:#84968c;font-size:20px;font-weight:900;letter-spacing:4px;margin-bottom:18px}
.letter{font-size:clamp(190px,30vw,480px);font-weight:900;color:#20ee78;text-align:center;line-height:.85}.prep{font-size:clamp(150px,24vw,380px);font-weight:900;color:#ffffff;text-align:center}.timeup{font-size:clamp(65px,10vw,150px);font-weight:900;text-align:center}
.info{display:flex;gap:50px;margin-top:28px}.label{color:#84968c;font-size:13px;font-weight:900;letter-spacing:2px}.num{font-size:70px;font-weight:900;color:#20ee78}.results{border-top:1px solid #174b30;padding-top:16px}.resultList{display:flex;gap:12px;flex-wrap:wrap;margin-top:10px}.result{border:1px solid #1c5638;border-radius:13px;padding:10px 15px}.result b{color:#20ee78;margin-left:10px}.wrong{animation:bad .18s 2}@keyframes bad{50%{color:#ff5b68;transform:scale(.94)}}
</style></head><body><div class="wrap"><div class="top"><div class="brand"><span class="m">МУЖСКОЕ</span> <span class="slash">/</span> <span class="w">ЖЕНСКОЕ</span></div><b class="accent">ХОМЯК</b></div>
<div class="stage"><div><div class="status" id="status">ОЖИДАНИЕ</div><div class="name" id="name">ХОМЯК</div><div class="info"><div><div class="label">ВРЕМЯ</div><div class="num" id="timer">30</div></div><div><div class="label">РЕЗУЛЬТАТ</div><div class="num" id="score">0</div></div></div></div><div id="center" class="letter">—</div></div>
<div class="results"><div class="label">РЕЗУЛЬТАТЫ</div><div class="resultList" id="results"></div></div></div>
<script>
const $=x=>document.getElementById(x);let wrong=-1;
document.addEventListener('keydown',e=>{if(!e.repeat&&/^Key[A-Z]$/.test(e.code)){e.preventDefault();send(e.code.slice(3))}});
async function send(l){render(await fetch('/men/hamster/api/key',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({letter:l})}).then(r=>r.json()))}
function render(s){let valid=s.current>=0&&s.current<s.participants.length,p=valid?s.participants[s.current]:null;$('name').textContent=p?p.name:'ХОМЯК';$('score').textContent=p?p.score:0;let r=Math.max(0,Math.ceil(s.remaining));$('timer').textContent=s.phase==='play'?r:'30';
if(s.phase==='prep'){$('status').textContent='ПРИГОТОВЬТЕСЬ';$('center').className='prep';$('center').textContent=r}else if(s.phase==='play'){$('status').textContent='НАЖМИ БУКВУ';$('center').className='letter';$('center').textContent=s.letter||'—'}else if(s.phase==='timeup'){$('status').textContent='';$('center').className='timeup';$('center').textContent='ВРЕМЯ!'}else if(s.finished){$('status').textContent='';$('center').className='timeup';$('center').textContent='КОНКУРС ЗАВЕРШЁН'}else{$('status').textContent=valid?'ГОТОВЬТЕСЬ':'ОЖИДАНИЕ';$('center').className='letter';$('center').textContent='—'}
if(wrong>=0&&s.wrong!==wrong&&s.phase==='play'){let c=$('center');c.classList.add('wrong');setTimeout(()=>c.classList.remove('wrong'),380)}wrong=s.wrong;let a=s.participants.filter((p,i)=>s.finished||i<s.current);$('results').innerHTML=a.map(p=>`<div class="result">${p.name}<b>${p.score}</b></div>`).join('')}
async function poll(){try{render(await fetch('/men/hamster/api/state?_='+Date.now(),{cache:'no-store'}).then(r=>r.json()))}catch(e){}setTimeout(poll,120)}poll();
</script></body></html>
"""

@app.after_request
def no_cache(r):
    r.headers["Cache-Control"]="no-store, no-cache, must-revalidate, max-age=0"; return r

@app.get("/")
def control(): return render_template_string(CONTROL)
@app.get("/screen")
def screen(): return render_template_string(SCREEN)
@app.get("/api/state")
def api_state():
    with lock: return jsonify(snap())

@app.post("/api/start")
def api_start():
    d=request.get_json(silent=True) or {}
    try: n=max(1,min(30,int(d.get("count",4))))
    except: n=4
    with lock:
        state.update(participants=[{"name":f"УЧАСТНИК {i+1}","score":0} for i in range(n)],current=0,finished=False,phase="idle",started=None,letter=None,wrong=0)
        return jsonify(snap())

@app.post("/api/go")
def api_go():
    with lock:
        if state["phase"]=="idle" and not state["finished"] and 0<=state["current"]<len(state["participants"]):
            state["phase"]="prep"; state["started"]=time.time()
        return jsonify(snap())

@app.post("/api/key")
def api_key():
    d=request.get_json(silent=True) or {}; letter=str(d.get("letter","")).upper()
    with lock:
        tick()
        if state["phase"]=="play" and len(letter)==1 and letter in string.ascii_uppercase:
            if letter==state["letter"]:
                state["participants"][state["current"]]["score"]+=1
                state["letter"]=random.choice(string.ascii_uppercase.replace(state["letter"],""))
            else: state["wrong"]+=1
        return jsonify(snap())

@app.post("/api/next")
def api_next():
    with lock:
        if state["participants"]:
            if state["current"]<len(state["participants"])-1:
                state.update(current=state["current"]+1,phase="idle",started=None,letter=None)
            else:
                state.update(current=len(state["participants"]),finished=True,phase="idle",started=None,letter=None)
        return jsonify(snap())

@app.post("/api/reset")
def api_reset():
    with lock:
        state.update(participants=[],current=-1,finished=False,phase="idle",started=None,letter=None,wrong=0)
        return jsonify(snap())

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
