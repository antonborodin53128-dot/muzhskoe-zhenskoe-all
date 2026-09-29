from flask import Flask, request, jsonify, render_template_string
import os
import time
from threading import Lock

app = Flask(__name__)
lock = Lock()

state = {
    "participant_count": 0,
    "participants": [],
    "current": -1,
    "finished": False,
    "timer_started": False,
    "timer_started_at": None,
    "timer_duration": 40,
    "version": 0
}

CONTROL_HTML = r"""
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Шарики — управление</title>
<style>
*{box-sizing:border-box} body{margin:0;background:#020b07;color:#f4f5ef;font-family:Arial,Helvetica,sans-serif}
body:before{content:"";position:fixed;inset:0;background:radial-gradient(circle at 0 20%,rgba(0,255,115,.14),transparent 34%);pointer-events:none}
.wrap{max-width:900px;margin:auto;padding:20px}.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px}
.brand{font-size:25px;font-weight:900}.brand .m{border:2px solid #20ee78;padding:7px 10px}.brand .slash,.accent{color:#20ee78}.brand .w{color:#747d78}
.panel,.game{border:1px solid #174b30;background:rgba(8,24,16,.9);border-radius:22px;padding:22px;margin-bottom:18px}
.row{display:flex;gap:14px;align-items:end;flex-wrap:wrap}.field label{display:block;color:#81938a;font-size:13px;margin:0 0 7px}
input{width:165px;background:#07110c;border:1px solid #22543a;border-radius:14px;color:white;padding:14px;font-size:20px}
button{border:0;border-radius:14px;padding:15px 22px;font-size:16px;font-weight:900;cursor:pointer}
.start,.plus{background:#20eb72;color:#001b0d}.reset{background:#3b171d;color:#ff9da9}.minus{background:#15251b;color:white}
.timerBox{display:flex;justify-content:space-between;align-items:center;gap:14px;margin:12px 0}
.timer{font-size:42px;font-weight:950;color:#20ee78}.timer.danger{color:#fff}
.timerStart{background:#20eb72;color:#001b0d;min-width:220px}.timerStart:disabled,.plus:disabled,.minus:disabled{opacity:.35;cursor:not-allowed}
.timeup{font-size:28px;font-weight:950;color:#fff;letter-spacing:2px}
.next{background:white;color:#07110c;width:100%;margin-top:12px}.screenlink{font-size:13px;color:#81938a;margin-top:15px}.screenlink a{color:#20ee78}
.game{display:none}.game.on{display:block}.label{color:#84968c;font-size:14px;font-weight:800;letter-spacing:1px}
.current{font-size:34px;font-weight:900;margin-top:8px}.score{font-size:72px;color:#20ee78;font-weight:900}
.controls{display:grid;grid-template-columns:1fr 135px;gap:12px}.plus{font-size:32px}.minus{font-size:26px}
.doneTitle{color:#81938a;font-size:12px;font-weight:900;margin-top:15px}.doneRow{display:flex;justify-content:space-between;padding:10px 4px;border-bottom:1px solid #10281b}.doneRow b{color:#20ee78}
.finishedMsg{font-size:34px;font-weight:900;color:#20ee78}
@media(max-width:600px){.controls{grid-template-columns:1fr 90px}.current{font-size:28px}.score{font-size:58px}}
</style>
</head>
<body>
<div class="wrap">
 <div class="top"><div class="brand"><span class="m">МУЖСКОЕ</span> <span class="slash">/</span> <span class="w">ЖЕНСКОЕ</span></div><b class="accent">ШАРИКИ · УПРАВЛЕНИЕ</b></div>
 <div class="panel">
   <div class="row">
    <div class="field"><label>Количество участников</label><input id="count" type="number" min="1" max="30" value="4"></div>
    <button class="start" onclick="startGame()">НАЧАТЬ КОНКУРС</button>
    <button class="reset" onclick="resetGame()">СБРОСИТЬ</button>
   </div>
   <div class="screenlink">Зрительский экран: <a href="/men/balls/screen" target="_blank">открыть /screen</a></div>
 </div>
 <div class="game" id="game">
   <div id="activeBox">
    <div class="label">СЕЙЧАС ИГРАЕТ</div>
    <div class="current" id="currentName"></div>
    <div class="timerBox"><div><div class="label">ВРЕМЯ</div><div class="timer" id="timer">40</div></div><button class="timerStart" id="timerStart" onclick="startTimer()">СТАРТ — 40 СЕКУНД</button></div>
    <div class="score" id="score">0</div>
    <div class="controls"><button class="plus" id="plusBtn" onclick="score(1)" disabled>+1 ШАРИК</button><button class="minus" id="minusBtn" onclick="score(-1)" disabled>−1</button></div>
    <button class="next" onclick="nextPlayer()">СЛЕДУЮЩИЙ УЧАСТНИК →</button>
   </div>
   <div id="finishedBox" class="finishedMsg" style="display:none">КОНКУРС ЗАВЕРШЁН ✓</div>
   <div class="doneTitle">УЖЕ СЫГРАЛИ</div>
   <div id="done"></div>
 </div>
</div>
<script>
async function api(url,body){
 const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
 return r.json()
}
async function startGame(){await api('/men/balls/api/start',{count:Number(document.getElementById('count').value)});refresh()}
async function resetGame(){await api('/men/balls/api/reset');refresh()}
async function startTimer(){await api('/men/balls/api/timer/start');refresh()}
async function score(delta){await api('/men/balls/api/score',{delta});refresh()}
async function nextPlayer(){await api('/men/balls/api/next');refresh()}
async function refresh(){
 const s=await fetch('/men/balls/api/state',{cache:'no-store'}).then(r=>r.json());
 const game=document.getElementById('game');
 if(!s.participants.length){game.classList.remove('on');return}
 game.classList.add('on');
 const valid=s.current>=0 && s.current<s.participants.length;
 document.getElementById('activeBox').style.display=valid&&!s.finished?'block':'none';
 document.getElementById('finishedBox').style.display=s.finished?'block':'none';
 if(valid){
   document.getElementById('currentName').textContent=s.participants[s.current].name;
   document.getElementById('score').textContent=s.participants[s.current].score;
   const remaining=Math.max(0,Math.ceil(Number(s.remaining)));
   const running=!!s.timer_started && remaining>0;
   document.getElementById('timer').textContent=remaining;
   document.getElementById('timer').classList.toggle('danger',running && remaining<=5);
   document.getElementById('timerStart').disabled=!!s.timer_started;
   document.getElementById('timerStart').textContent=s.timer_started?(remaining>0?'ИДЁТ ВРЕМЯ':'ВРЕМЯ!'):'СТАРТ — 40 СЕКУНД';
   document.getElementById('plusBtn').disabled=!running;
   document.getElementById('minusBtn').disabled=!running;
 }
 const completed=s.participants.filter((p,i)=>i<s.current || s.finished);
 document.getElementById('done').innerHTML=completed.map(p=>`<div class="doneRow"><span>${p.name}</span><b>${p.score}</b></div>`).join('');
}
setInterval(refresh,1000);refresh();
</script>
</body></html>
"""

SCREEN_HTML = r"""
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Шарики — экран</title>
<style>
*{box-sizing:border-box}html,body{width:100%;height:100%;margin:0;overflow:hidden}
body{background:#020905;color:#f5f6ef;font-family:Arial,Helvetica,sans-serif}
body:before{content:"";position:fixed;inset:0;background:radial-gradient(circle at 0 30%,rgba(0,255,112,.16),transparent 35%);pointer-events:none}
.wrap{height:100vh;padding:28px 5vw 34px;display:flex;flex-direction:column}
.top{display:flex;justify-content:space-between;align-items:center}.brand{font-size:28px;font-weight:900}
.brand .m{border:2px solid #20ed76;padding:8px 12px}.brand .slash,.title{color:#20ed76}.brand .w{color:#747c78}
.title{font-size:30px;font-weight:900;letter-spacing:6px}
.main{flex:1;display:grid;grid-template-columns:minmax(0,1fr) 280px;align-items:center;gap:35px}
.kicker{color:#87998f;font-weight:900;letter-spacing:5px;font-size:20px;margin-bottom:24px}
.name{font-size:clamp(58px,7vw,118px);font-weight:950;line-height:.92;word-break:break-word}
.score{font-size:clamp(150px,19vw,330px);font-weight:950;color:#20ed76;text-align:center;text-shadow:0 0 35px rgba(32,237,118,.25);transition:transform .16s ease}
.screenTimer{font-size:clamp(54px,6vw,100px);font-weight:950;color:#20ed76;margin-top:22px}.screenTimer.danger{font-size:clamp(80px,10vw,170px);color:#fff}.waiting{color:#87998f;font-size:24px;font-weight:900;margin-top:22px}.timeup{font-size:clamp(55px,7vw,115px);font-weight:950;color:#fff;margin-top:20px}
.score.bump{transform:scale(1.12)}
.results{min-height:130px;border-top:1px solid #123c27;padding-top:18px}
.resultsTitle{color:#87998f;font-size:17px;font-weight:900;letter-spacing:4px;margin-bottom:12px}
.resultList{display:flex;gap:12px;flex-wrap:wrap}.result{border:1px solid #1c5638;border-radius:13px;padding:11px 16px;font-size:18px;background:#07150d}
.result b{color:#20ed76;margin-left:12px}.empty{color:#526158}
.finish{text-align:center;font-size:clamp(54px,7vw,110px);font-weight:950}.finish span{display:block;color:#20ed76;margin-top:18px}
@media(max-width:850px){.main{grid-template-columns:1fr 180px}.brand{font-size:20px}.title{font-size:20px}.name{font-size:55px}}
</style>
</head>
<body>
<div class="wrap">
 <div class="top"><div class="brand"><span class="m">МУЖСКОЕ</span> <span class="slash">/</span> <span class="w">ЖЕНСКОЕ</span></div><div class="title">ШАРИКИ</div></div>
 <div class="main" id="main">
   <div><div class="kicker" id="kicker">СЕЙЧАС ИГРАЕТ</div><div class="name" id="name">ОЖИДАНИЕ</div></div>
   <div class="score" id="score">0</div>
 </div>
 <div class="results">
   <div class="resultsTitle">РЕЗУЛЬТАТЫ</div>
   <div class="resultList" id="results"><span class="empty">Участники ещё не играли</span></div>
 </div>
</div>
<script>
let lastScore = null;
let pollTimer = null;

function escapeHtml(v){
  return String(v).replace(/[&<>"']/g,c=>({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}

async function refresh(){
  try{
    const r = await fetch('/men/balls/api/state?_=' + Date.now(), {
      method:'GET',
      cache:'no-store',
      headers:{'Cache-Control':'no-cache'}
    });
    if(!r.ok) throw new Error('state');
    const s = await r.json();

    const main = document.getElementById('main');
    const results = document.getElementById('results');
    const hasPlayers = Array.isArray(s.participants) && s.participants.length > 0;
    const current = Number(s.current);
    const active = hasPlayers && !s.finished &&
                   current >= 0 && current < s.participants.length;

    if(active){
      const p = s.participants[current];
      const remaining=Math.max(0,Math.ceil(Number(s.remaining)));
      const timerStarted=!!s.timer_started;
      let timerHtml = !timerStarted
        ? `<div class="waiting">ГОТОВЬТЕСЬ · 40 СЕКУНД</div>`
        : remaining>0
          ? `<div class="screenTimer ${remaining<=5?'danger':''}">${remaining}</div>`
          : `<div class="timeup">ВРЕМЯ!</div>`;
      main.innerHTML =
        `<div><div class="kicker">СЕЙЧАС ИГРАЕТ</div>`+
        `<div class="name">${escapeHtml(p.name)}</div>${timerHtml}</div>`+
        `<div class="score" id="score">${Number(p.score)||0}</div>`;

      const scoreEl = document.getElementById('score');
      if(lastScore !== null && Number(p.score) !== lastScore){
        scoreEl.classList.add('bump');
        setTimeout(()=>scoreEl.classList.remove('bump'),180);
      }
      lastScore = Number(p.score)||0;
    }else if(s.finished && hasPlayers){
      main.innerHTML =
        `<div class="finish" style="grid-column:1/-1">`+
        `КОНКУРС ЗАВЕРШЁН<span>✓</span></div>`;
      lastScore = null;
    }else{
      main.innerHTML =
        `<div><div class="kicker">ШАРИКИ</div>`+
        `<div class="name">ОЖИДАНИЕ</div></div>`+
        `<div class="score" id="score">0</div>`;
      lastScore = null;
    }

    const completed = hasPlayers
      ? s.participants.filter((p,i)=>s.finished || i < current)
      : [];

    results.innerHTML = completed.length
      ? completed.map(p=>
          `<div class="result">${escapeHtml(p.name)} <b>${Number(p.score)||0}</b></div>`
        ).join('')
      : '<span class="empty">Участники ещё не играли</span>';

  }catch(e){
    // Keep the last visible state and simply retry.
  }finally{
    clearTimeout(pollTimer);
    pollTimer = setTimeout(refresh, 250);
  }
}

refresh();
</script>
</body></html>
"""

@app.after_request
def no_cache(resp):
    if request.path.startswith("/api/") or request.path == "/screen":
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp

@app.get("/")
def control():
    return render_template_string(CONTROL_HTML)

@app.get("/screen")
def screen():
    return render_template_string(SCREEN_HTML)

def timer_remaining_locked():
    if not state["timer_started"] or state["timer_started_at"] is None:
        return state["timer_duration"]
    return max(0.0, state["timer_duration"] - (time.time() - state["timer_started_at"]))

def state_payload_locked():
    payload = dict(state)
    payload["participants"] = [dict(p) for p in state["participants"]]
    payload["remaining"] = timer_remaining_locked()
    return payload

@app.get("/api/state")
def get_state():
    with lock:
        return jsonify(state_payload_locked())

@app.post("/api/start")
def start():
    data=request.get_json(silent=True) or {}
    try: count=int(data.get("count",4))
    except: count=4
    count=max(1,min(count,30))
    with lock:
        state["participant_count"]=count
        state["participants"]=[{"name":f"УЧАСТНИК {i+1}","score":0} for i in range(count)]
        state["current"]=0
        state["finished"]=False
        state["timer_started"]=False
        state["timer_started_at"]=None
        state["version"]+=1
        return jsonify(state_payload_locked())

@app.post("/api/timer/start")
def start_timer():
    with lock:
        i=state["current"]
        if not state["finished"] and 0<=i<len(state["participants"]) and not state["timer_started"]:
            state["timer_started"]=True
            state["timer_started_at"]=time.time()
            state["version"]+=1
        return jsonify(state_payload_locked())

@app.post("/api/score")
def change_score():
    data=request.get_json(silent=True) or {}
    try: delta=int(data.get("delta",0))
    except: delta=0
    with lock:
        i=state["current"]
        if (not state["finished"] and 0<=i<len(state["participants"])
                and state["timer_started"] and timer_remaining_locked()>0):
            state["participants"][i]["score"]=max(0,state["participants"][i]["score"]+delta)
            state["version"]+=1
        return jsonify(state_payload_locked())

@app.post("/api/next")
def next_player():
    with lock:
        if not state["participants"]:
            return jsonify(state_payload_locked())
        if state["current"] < len(state["participants"])-1:
            state["current"]+=1
            state["timer_started"]=False
            state["timer_started_at"]=None
        else:
            state["finished"]=True
            state["current"]=len(state["participants"])
            state["timer_started"]=False
            state["timer_started_at"]=None
        state["version"]+=1
        return jsonify(state_payload_locked())

@app.post("/api/reset")
def reset():
    with lock:
        state["participant_count"]=0
        state["participants"]=[]
        state["current"]=-1
        state["finished"]=False
        state["timer_started"]=False
        state["timer_started_at"]=None
        state["version"]+=1
        return jsonify(state_payload_locked())

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
