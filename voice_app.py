from flask import Flask, render_template_string, redirect, request, jsonify
import os, time, threading

app = Flask(__name__)

HTML = r"""
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Voice Meter — тест аудиовхода</title>
<style>
*{box-sizing:border-box}
html,body{margin:0;min-height:100%;background:#020b07;color:#f4f5ef;font-family:Arial,sans-serif}
body:before{content:"";position:fixed;inset:0;background:radial-gradient(circle at 0 25%,rgba(0,255,115,.15),transparent 36%);pointer-events:none}
.wrap{max-width:1000px;margin:auto;padding:28px}
.top{display:flex;justify-content:space-between;align-items:center;gap:20px;margin-bottom:24px}
.brand{font-size:25px;font-weight:900}.m{border:2px solid #20ee78;padding:7px 10px}.slash,.accent{color:#20ee78}.w{color:#747d78}
.panel{border:1px solid #174b30;background:rgba(8,24,16,.92);border-radius:22px;padding:24px;margin-bottom:18px}
h1{margin:0 0 8px;font-size:38px}.sub{color:#8ba095;margin-bottom:24px}
.label{color:#84968c;font-size:13px;font-weight:900;letter-spacing:2px;margin-bottom:8px}
select,button{width:100%;border-radius:14px;padding:14px 16px;font-size:17px}
select{background:#07110c;border:1px solid #22543a;color:#fff;margin-bottom:14px}
button{border:0;background:#20eb72;color:#001b0d;font-weight:900;cursor:pointer}
button.secondary{margin-top:10px;background:#18231d;color:#d6e0da;border:1px solid #405047}
.meter{height:58px;background:#06110b;border:1px solid #205239;border-radius:16px;overflow:hidden;margin-top:18px;position:relative}
.fill{height:100%;width:0%;background:linear-gradient(90deg,#16d965 0%,#9fea3b 65%,#f2dc36 82%,#ff5353 100%);transition:width .05s linear}
.scale{display:flex;justify-content:space-between;color:#73877b;font-size:12px;margin-top:7px}
.readout{display:flex;gap:18px;margin-top:22px;flex-wrap:wrap}.card{flex:1;min-width:180px;background:#06140c;border:1px solid #16452c;border-radius:18px;padding:18px}
.value{font-size:46px;font-weight:900;color:#20ee78;margin-top:4px}
.status{margin-top:15px;color:#9caf9f}.warn{color:#ffcf65}
.small{font-size:13px;color:#82958a;line-height:1.45;margin-top:16px}
</style>
</head>
<body><div class="wrap">
<div class="top"><div class="brand"><span class="m">VOICE METER</span></div><b class="accent">VOICE METER</b></div>
<div class="panel">
<h1>ТЕСТ АУДИОВХОДА</h1>
<div class="sub">Сначала проверяем, что браузер видит нужную звуковую карту и корректно измеряет уровень микрофона.</div>
<div class="label">АУДИОВХОД / МИКРОФОН</div>
<select id="devices"><option>Нажмите «Разрешить микрофон»</option></select>
<button id="permission">РАЗРЕШИТЬ МИКРОФОН</button>
<button class="secondary" id="refresh">ОБНОВИТЬ СПИСОК УСТРОЙСТВ</button>
<div class="meter"><div class="fill" id="fill"></div></div>
<div class="scale"><span>ТИХО</span><span>ГРОМКО</span></div>
<div class="readout">
<div class="card"><div class="label">ТЕКУЩИЙ УРОВЕНЬ</div><div class="value" id="level">0</div></div>
<div class="card"><div class="label">ПИК</div><div class="value" id="peak">0</div></div>
</div>
<div class="status" id="status">Микрофон ещё не подключён.</div>
<div class="small">Выбранное устройство запоминается в этом браузере. При смене входа браузер переподключится к выбранному устройству. Для работы микрофона страница должна быть открыта по HTTPS или на localhost.</div>
</div></div>
<script>
let lastLevelSend = 0;
function publishVoiceLevel(v){
    const now = Date.now();
    if (now - lastLevelSend < 70) return;
    lastLevelSend = now;
    fetch('/men/voice/api/level', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({level:v}),
        cache: 'no-store'
    }).catch(()=>{});
}

let ctx=null, analyser=null, stream=null, raf=null, peak=0;
const devices=document.getElementById('devices'), status=document.getElementById('status'),
fill=document.getElementById('fill'), level=document.getElementById('level'), peakEl=document.getElementById('peak');

async function enumerate(){
  const list=await navigator.mediaDevices.enumerateDevices();
  const ins=list.filter(d=>d.kind==='audioinput');
  const saved=localStorage.getItem('voiceMeterDevice');
  devices.innerHTML='';
  ins.forEach((d,i)=>{
    const o=document.createElement('option');
    o.value=d.deviceId; o.textContent=d.label || ('Аудиовход '+(i+1));
    if(saved && saved===d.deviceId)o.selected=true;
    devices.appendChild(o);
  });
  if(!ins.length){devices.innerHTML='<option>Аудиовходы не найдены</option>';}
}

async function connect(deviceId){
  try{
    if(stream)stream.getTracks().forEach(t=>t.stop());
    if(raf)cancelAnimationFrame(raf);
    const constraints={audio:{
      deviceId:deviceId?{exact:deviceId}:undefined,
      echoCancellation:false, noiseSuppression:false, autoGainControl:false
    }};
    stream=await navigator.mediaDevices.getUserMedia(constraints);
    if(!ctx)ctx=new (window.AudioContext||window.webkitAudioContext)();
    if(ctx.state==='suspended')await ctx.resume();
    analyser=ctx.createAnalyser(); analyser.fftSize=2048; analyser.smoothingTimeConstant=.2;
    ctx.createMediaStreamSource(stream).connect(analyser);
    await enumerate();
    const track=stream.getAudioTracks()[0];
    const settings=track.getSettings();
    if(settings.deviceId){devices.value=settings.deviceId;localStorage.setItem('voiceMeterDevice',settings.deviceId);}
    status.textContent='Подключено: '+(track.label||'аудиовход');
    peak=0; draw();
  }catch(e){
    status.textContent='Не удалось открыть микрофон: '+e.message;
    status.className='status warn';
  }
}

function draw(){
  const a=new Float32Array(analyser.fftSize); analyser.getFloatTimeDomainData(a);
  let sum=0,max=0;
  for(let i=0;i<a.length;i++){sum+=a[i]*a[i];max=Math.max(max,Math.abs(a[i]));}
  const rms=Math.sqrt(sum/a.length);
  // visual 0..100 scale, intentionally sensitive enough for speech/shouting
  const val=Math.max(0,Math.min(100,Math.round((20*Math.log10(Math.max(rms,0.00001))+60)*1.67)));
  peak=Math.max(peak,val);
  fill.style.width=val+'%'; level.textContent=val; peakEl.textContent=peak; publishVoiceLevel(val);
  raf=requestAnimationFrame(draw);
}

document.getElementById('permission').onclick=async()=>{
  await connect(localStorage.getItem('voiceMeterDevice')||'');
};
document.getElementById('refresh').onclick=enumerate;
devices.onchange=()=>{localStorage.setItem('voiceMeterDevice',devices.value);connect(devices.value);}
navigator.mediaDevices?.addEventListener?.('devicechange',enumerate);
if(!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia){
  status.textContent='Этот браузер не поддерживает доступ к микрофону.'; status.className='status warn';
}else enumerate();
</script><div class="panel" style="margin-top:18px">
<div class="label" style="margin-bottom:12px">СТРАНИЦЫ КОНКУРСА</div>
<div style="display:flex;gap:10px;flex-wrap:wrap">
<a href="/men/voice/setup" style="text-decoration:none"><button type="button">SETUP</button></a>
<a href="/men/voice/control" target="_blank" rel="noopener" style="text-decoration:none"><button type="button">УПРАВЛЕНИЕ</button></a>
<a href="/men/voice/screen" target="_blank" rel="noopener" style="text-decoration:none"><button type="button">ГОСТЕВОЙ ЭКРАН</button></a>
</div></div></body></html>
"""


lock = threading.Lock()
game = {"participants":[],"current":-1,"phase":"idle","started":None,"peak":0.0,"live":0.0}
PREP=5
ROUND=10

def update_game():
    if game["phase"]=="prep" and time.time()-game["started"]>=PREP:
        game["phase"]="play"; game["started"]=time.time(); game["peak"]=0.0
    elif game["phase"]=="play" and time.time()-game["started"]>=ROUND:
        i=game["current"]
        if 0<=i<len(game["participants"]):
            game["participants"][i]["score"]=round(game["peak"])
            game["participants"][i]["done"]=True
        game["phase"]="timeup"

def game_data():
    update_game()
    rem=0
    if game["phase"]=="prep": rem=max(0,PREP-(time.time()-game["started"]))
    elif game["phase"]=="play": rem=max(0,ROUND-(time.time()-game["started"]))
    return {**game,"participants":[dict(x) for x in game["participants"]],"remaining":rem}

CONTROL_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Voice Meter — ведущий</title><style>
body{margin:0;background:#020b07;color:#f4f5ef;font-family:Arial,sans-serif}.wrap{max-width:950px;margin:auto;padding:25px}.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}.brand{font-size:25px;font-weight:900}.m{border:2px solid #20ee78;padding:7px 10px}.w{color:#747d78}.a{color:#20ee78}.p{border:1px solid #174b30;background:#081810;border-radius:22px;padding:22px;margin:16px 0}.label{color:#84968c;font-size:13px;font-weight:900;letter-spacing:2px}button,a{border:0;border-radius:14px;padding:15px 22px;font-size:16px;font-weight:900;text-decoration:none;display:inline-block;cursor:pointer}.g{background:#20eb72;color:#001b0d}.d{background:#18231d;color:#d5ded9}.r{background:#3b171d;color:#ff9da9}input{background:#07110c;border:1px solid #22543a;border-radius:14px;color:#fff;padding:14px;font-size:18px}.row{display:flex;gap:12px;align-items:end;flex-wrap:wrap}.name{font-size:38px;font-weight:900}.big{font-size:75px;color:#20ee78;font-weight:900}.res{display:flex;justify-content:space-between;border-bottom:1px solid #12301f;padding:11px 2px}.res b{color:#20ee78}.sep{margin-top:28px;padding-top:20px;border-top:1px solid #174b30}

.retryActions{margin-top:28px;padding-top:18px;border-top:1px solid #294238;display:flex;flex-direction:column;gap:12px}
.nextBtn{width:100%;background:#20ee78;color:#001b0d;border:0;border-radius:14px;padding:16px 22px;font-size:16px;font-weight:900;cursor:pointer}
.retryBtn{width:100%;background:#18231d;color:#aab6b0;border:1px solid #405047;border-radius:14px;padding:13px 22px;font-size:14px;font-weight:800;cursor:pointer}
</style></head><body><div class="wrap"><div class="top"><div class="brand"><span class="m">VOICE METER</span></div></div>
<div class="p"><div class="label">СТРАНИЦА ВЕДУЩЕГО</div><div class="row" style="margin-top:12px"><div><div class="label">КОЛИЧЕСТВО УЧАСТНИКОВ</div><input id="n" type="number" min="1" max="10" value="4"></div><button class="g" onclick="init()">НАЧАТЬ КОНКУРС</button><button class="r" onclick="post('/men/voice/api/reset')">СБРОСИТЬ</button><a class="d" href="/men/voice/screen" target="_blank">ГОСТЕВОЙ ЭКРАН</a></div></div><div class="p" id="game">ОЖИДАНИЕ</div></div>
<script>
function toDb(level){
  return Math.max(-60,Math.min(0,-60+(Math.max(0,Math.min(100,Number(level)||0))/100)*60));
}
function fmtDb(level){ return toDb(level).toFixed(1)+' dB'; }

let controlStream=null;
async function activateSavedAudio(){
  if(controlStream) return true;
  try{
    const id=localStorage.getItem('voiceMeterDevice');
    controlStream=await navigator.mediaDevices.getUserMedia({
      audio:{deviceId:id?{exact:id}:undefined,echoCancellation:false,noiseSuppression:false,autoGainControl:false}
    });
    return true;
  }catch(e){ return false; }
}

async function post(u,b={}){return fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}).then(r=>r.json())}async function init(){await activateSavedAudio();post('/men/voice/api/init',{count:+n.value})}
function draw(s){let e=document.getElementById('game');if(!s.participants.length){e.innerHTML='ОЖИДАНИЕ';return}let p=s.participants[s.current],t=s.phase==='prep'?'ОТСЧЁТ: '+Math.max(1,Math.ceil(s.remaining)):s.phase==='play'?'ЗАМЕР · '+Math.ceil(s.remaining)+' СЕК.':s.phase==='timeup'?'ВРЕМЯ!':s.phase==='finished'?'КОНКУРС ЗАВЕРШЁН':'ГОТОВ';let b=s.phase==='ready'?'<button class="g" onclick="post(\\'/men/voice/api/start\\')">СТАРТ</button>':s.phase==='timeup'?'<div class="retryActions"><button class="nextBtn" onclick="post(\\'/men/voice/api/next\\')">СЛЕДУЮЩИЙ УЧАСТНИК →</button><button class="retryBtn" onclick="post(\\'/men/voice/api/retry\\')">НАЧАТЬ ЗАНОВО</button></div>':'';let rs=s.participants.filter(x=>x.done).map(x=>`<div class="res"><span>${x.name}</span><b>${fmtDb(x.score)}</b></div>`).join('');e.innerHTML=`<div class="label">СЕЙЧАС ИГРАЕТ</div><div class="name">${p?p.name:''}</div><h2>${t}</h2><div class="big">${s.peak}</div>${b}<div class="sep"><div class="label">РЕЗУЛЬТАТЫ</div>${rs}</div>`}
async function poll(){try{draw(await fetch('/men/voice/api/state?_='+Date.now(),{cache:'no-store',headers:{'Cache-Control':'no-cache'}}).then(r=>r.json()))}catch(e){}setTimeout(poll,150)}poll()
</script></body></html>"""

SCREEN_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Voice Meter</title><style>
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 15% 50%,#062419 0,#07110d 38%,#030806 75%);color:#f4f5ef;font-family:Arial,sans-serif;overflow:hidden}
.wrap{max-width:1250px;height:100vh;margin:auto;padding:24px 36px;display:flex;flex-direction:column}.top{display:flex;justify-content:space-between;align-items:center}.brand{font-size:25px;font-weight:900}.m{border:2px solid #20ee78;padding:7px 10px}.w{color:#747d78}.a{color:#20ee78}
.main{flex:1;display:grid;grid-template-columns:1fr 330px;gap:55px;align-items:center}.info{text-align:center}.name{font-size:50px;font-weight:900;letter-spacing:1px}.status{font-size:28px;margin-top:12px}.db{font-size:150px;font-weight:900;line-height:1;margin:30px 0 8px;transition:color .08s}.unit{font-size:36px;margin-left:8px}.timer{font-size:42px;font-weight:900}.hint{color:#81988c;font-size:18px;margin-top:18px}
.meterbox{display:flex;align-items:center;justify-content:center;gap:18px}.scale{height:590px;display:flex;flex-direction:column;justify-content:space-between;text-align:right;color:#a9b8b0;font-size:17px;font-weight:800}.meter{position:relative;width:125px;height:590px;border:2px solid #27513c;border-radius:26px;overflow:hidden;background:#06100b;box-shadow:0 0 40px rgba(32,238,120,.08)}
.zones{position:absolute;inset:0;background:linear-gradient(to top,#19df68 0%,#8ee63e 55%,#ffd640 75%,#ff5b55 100%);opacity:.18}.fill{position:absolute;left:0;right:0;bottom:0;height:0;background:linear-gradient(to top,#19df68 0%,#8ee63e 55%,#ffd640 75%,#ff5b55 100%);transition:height .06s linear;box-shadow:0 0 24px rgba(32,238,120,.35)}
.peakline{position:absolute;left:0;right:0;height:4px;background:white;bottom:0;transition:bottom .08s}.results{min-height:74px;border-top:1px solid #174b30;padding-top:13px;display:flex;gap:14px;overflow:hidden}.res{min-width:180px;border:1px solid #174b30;border-radius:12px;padding:10px 14px;display:flex;justify-content:space-between}.res b{color:#20ee78}

.count{display:block!important;font-size:260px!important;font-weight:900;line-height:1;color:#fff!important;margin:28px 0}
.finalResult{font-size:190px;font-weight:900;line-height:1;margin:30px 0 12px}
.finalResult .unit{font-size:44px}
</style></head><body><div class="wrap"><div class="top"><div class="brand"><span class="m">VOICE METER</span></div></div>
<button id="audioStart" onclick="startScreenAudio()" style="position:absolute;top:90px;right:36px;background:#20ee78;color:#001b0d;border:0;border-radius:12px;padding:12px 18px;font-weight:900;cursor:pointer">ПОДКЛЮЧИТЬ VOICE METER</button><div class="main"><div class="info" id="info"></div><div class="meterbox"><div class="scale"><span>0</span><span>-10</span><span>-20</span><span>-30</span><span>-40</span><span>-50</span><span>-60</span></div><div class="meter"><div class="zones"></div><div class="fill" id="fill"></div><div class="peakline" id="peakline"></div></div></div></div><div class="results" id="results"></div></div>
<script>
let screenStream=null,screenCtx=null,screenAnalyser=null,screenData=null,screenPeak=0;
async function startScreenAudio(){
  try{
    const id=localStorage.getItem('voiceMeterDevice');
    screenStream=await navigator.mediaDevices.getUserMedia({
      audio:{deviceId:id?{exact:id}:undefined,echoCancellation:false,noiseSuppression:false,autoGainControl:false}
    });
    screenCtx=new (window.AudioContext||window.webkitAudioContext)();
    if(screenCtx.state==='suspended') await screenCtx.resume();
    screenAnalyser=screenCtx.createAnalyser();
    screenAnalyser.fftSize=1024;
    screenAnalyser.smoothingTimeConstant=.12;
    screenData=new Float32Array(screenAnalyser.fftSize);
    screenCtx.createMediaStreamSource(screenStream).connect(screenAnalyser);
    document.getElementById('audioStart').style.display='none';
    measureScreen();
  }catch(e){
    document.getElementById('audioStart').textContent='РАЗРЕШИТЬ МИКРОФОН';
  }
}
function measureScreen(){
  if(!screenAnalyser)return;
  screenAnalyser.getFloatTimeDomainData(screenData);
  let sum=0;
  for(let i=0;i<screenData.length;i++){let v=screenData[i];sum+=v*v;}
  const rms=Math.sqrt(sum/screenData.length);
  const level=Math.max(0,Math.min(100,Math.round((20*Math.log10(Math.max(rms,.00001))+60)*1.67)));
  screenPeak=Math.max(screenPeak,level);
  const f=document.getElementById('fill');
  const db=toDb(level), num=document.getElementById('liveDb');
  if(window.currentGamePhase!=='timeup' && window.currentGamePhase!=='finished'){
    if(f)f.style.height=level+'%';
    if(num){num.textContent=(db<=-60?'-60':db.toFixed(1));num.style.color=dbColor(db);}
  }
  fetch('/men/voice/api/level',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({level:level}),cache:'no-store'}).catch(()=>{});
  requestAnimationFrame(measureScreen);
}

let shownPeak=0;
function toDb(level){return Math.max(-60,Math.min(0,-60+(Math.max(0,Math.min(100,level))/100)*60))}
function dbColor(db){if(db>=-15)return '#ff5b55';if(db>=-27)return '#ffd640';if(db>=-40)return '#9be63e';return '#20ee78'}
function draw(s){window.currentGamePhase=s.phase;
 let p=s.participants[s.current], live=Math.max(0,Math.min(100,Number(s.live)||0)), db=toDb(live), peakLevel=Math.max(0,Math.min(100,Number(s.peak)||0));
 if(s.phase==='ready'||s.phase==='prep') shownPeak=0; else shownPeak=Math.max(shownPeak,peakLevel);
 if(!screenAnalyser){fill.style.height=live+'%';} peakline.style.bottom=Math.max(0,Math.min(100,shownPeak))+'%';
 let status='',timer='';
 if(!s.participants.length){status='ОЖИДАНИЕ';p=null}
 else if(s.phase==='prep'){status='ПРИГОТОВЬТЕСЬ';timer='<span class="count">'+Math.max(1,Math.ceil(s.remaining))+'</span>'}
 else if(s.phase==='play'){status='КРИЧИ!';timer=Math.ceil(s.remaining)+' СЕК.'}
 else if(s.phase==='timeup'){status='РЕЗУЛЬТАТ';live=Math.max(0,Math.min(100,Number(p.score)||0));db=toDb(live);timer=''}
 else if(s.phase==='finished'){status='КОНКУРС ЗАВЕРШЁН';p=null}
 else status='ПРИГОТОВЬТЕСЬ';
 let c=dbColor(db), dbText=(db<=-60?'-60':db.toFixed(1));
 if(s.phase==='timeup'){
   info.innerHTML=`<div class="name">${p?p.name:''}</div><div class="status">МАКСИМАЛЬНЫЙ РЕЗУЛЬТАТ</div><div class="finalResult" style="color:${c}">${dbText}<span class="unit"> dB</span></div>`;
 }else{
   info.innerHTML=`<div class="name">${p?p.name:''}</div><div class="status">${status}</div><div class="db"><span id="liveDb" style="color:${c}">${dbText}</span><span class="unit" style="color:${c}"> dB</span></div><div class="timer">${timer}</div><div class="hint">Чем громче звук, тем выше показатель</div>`;
 }
 results.innerHTML=s.participants.filter(x=>x.done).map(x=>`<div class="res"><span>${x.name}</span><b>${toDb(Number(x.score)||0).toFixed(1)} dB</b></div>`).join('');
}
async function poll(){try{draw(await fetch('/men/voice/api/state?_='+Date.now(),{cache:'no-store',headers:{'Cache-Control':'no-cache'}}).then(r=>r.json()))}catch(e){}setTimeout(poll,100)}poll()
</script></body></html>"""

@app.get("/api/state")
def api_state():
    with lock:return jsonify(game_data())

@app.post("/api/init")
def api_init():
    d=request.get_json(silent=True) or {}
    try:n=max(1,min(10,int(d.get("count",4))))
    except:n=4
    with lock:
        game.update(participants=[{"name":f"УЧАСТНИК {i+1}","score":None,"done":False} for i in range(n)],current=0,phase="ready",started=None,peak=0.0,live=0.0)
        return jsonify(game_data())

@app.post("/api/start")
def api_start():
    with lock:
        if game["phase"]=="ready":game.update(phase="prep",started=time.time(),peak=0.0,live=0.0)
        return jsonify(game_data())

@app.post("/api/level")
def api_level():
    d=request.get_json(silent=True) or {}
    try:v=max(0.0,min(100.0,float(d.get("level",0))))
    except:v=0.0
    with lock:
        update_game();game["live"]=v
        if game["phase"]=="play":game["peak"]=max(game["peak"],v)
        return jsonify(ok=True)

@app.post("/api/retry")
def api_retry():
    with lock:
        i=game["current"]
        if 0 <= i < len(game["participants"]):
            game["participants"][i]["score"]=None
            game["participants"][i]["done"]=False
            game.update(phase="ready",started=None,peak=0.0,live=0.0)
        return jsonify(game_data())

@app.post("/api/next")
def api_next():
    with lock:
        if game["phase"]=="timeup":
            if game["current"]+1<len(game["participants"]):game.update(current=game["current"]+1,phase="ready",started=None,peak=0.0,live=0.0)
            else:game.update(current=len(game["participants"]),phase="finished",started=None,peak=0,live=0)
        return jsonify(game_data())

@app.post("/api/reset")
def api_reset():
    with lock:
        game.update(participants=[],current=-1,phase="idle",started=None,peak=0.0,live=0.0)
        return jsonify(game_data())

@app.get("/")
def index():
    return redirect("/men/voice/setup")

@app.get("/setup")
def setup():
    return render_template_string(HTML)

@app.get("/control")
def control():
    return render_template_string(CONTROL_HTML)

@app.get("/screen")
def screen():
    return render_template_string(SCREEN_HTML)

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
