"""The live memory-map page for MIRSAD (local edition).

A single self-contained HTML page — no build step, no external assets —
served by the MCP server itself. It polls /graph and draws the memory as
a living network: entities, decisions and values as nodes; facts and
decision links as edges. New writes appear live while an agent works,
and clicking any node tells its story (facts, timeline, decisions,
provenance) in the side panel.
"""

VIEWER_HTML = r"""<!DOCTYPE html>
<html lang="ar">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>مِرصاد — خريطة الذاكرة الحية</title>
<link rel="icon" type="image/svg+xml" href="/logo.svg">
<style>
  :root{
    --bg:#0b1220; --bg2:#0e1830; --ink:#dbe7f5; --dim:#8ba3c2;
    --gold:#e8c268; --blue:#8fc1f0; --slate:#9fb4c8; --red:#e0655f;
    --panel:#101c33; --line:#22345a;
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%;background:radial-gradient(1200px 800px at 70% 20%,var(--bg2),var(--bg));color:var(--ink);
    font-family:"Segoe UI",system-ui,-apple-system,"Noto Kufi Arabic",Tahoma,sans-serif;overflow:hidden}
  header{position:fixed;top:0;left:0;right:0;height:58px;display:flex;align-items:center;gap:14px;
    padding:0 18px;background:rgba(11,18,32,.82);backdrop-filter:blur(6px);border-bottom:1px solid var(--line);z-index:5}
  header h1{font-size:19px;margin:0;font-weight:600;letter-spacing:.3px}
  header h1 b{color:var(--gold);font-weight:700}
  .live{display:flex;align-items:center;gap:7px;color:var(--dim);font-size:12.5px}
  .dot{width:9px;height:9px;border-radius:50%;background:#58d68a;box-shadow:0 0 8px #58d68a;animation:pulse 1.6s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
  .stats{display:flex;gap:16px;margin-inline-start:auto;font-size:13px;color:var(--dim)}
  .stats b{color:var(--ink);font-size:15px}
  .legend{display:flex;gap:12px;font-size:12px;color:var(--dim)}
  .chip{display:inline-block;width:10px;height:10px;border-radius:50%;margin-inline-end:5px;vertical-align:middle}
  #map{position:fixed;inset:58px 0 0 0;display:block;cursor:grab}
  #map:active{cursor:grabbing}
  aside{position:fixed;top:58px;bottom:0;inset-inline-end:0;width:340px;max-width:88vw;background:var(--panel);
    border-inline-start:1px solid var(--line);padding:18px;overflow-y:auto;transform:translateX(0);z-index:4}
  [dir="ltr"] aside{transform:none}
  aside.hidden{display:none}
  aside h2{margin:0 0 4px;font-size:17px;line-height:1.45}
  aside .kind{color:var(--gold);font-size:12px;text-transform:uppercase;letter-spacing:1px}
  aside h3{margin:16px 0 6px;font-size:13px;color:var(--dim);border-bottom:1px solid var(--line);padding-bottom:4px}
  aside p,aside li{font-size:13.5px;line-height:1.75}
  aside ul{margin:0;padding-inline-start:18px}
  .meta{color:var(--dim);font-size:12px}
  .fact{border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:7px 0;background:rgba(255,255,255,.02)}
  .fact .pred{color:var(--blue)}
  .who{color:var(--dim);font-size:11.5px}
  .hint{position:fixed;bottom:12px;inset-inline-start:14px;color:var(--dim);font-size:12px;z-index:3}
</style>
</head>
<body>
<header>
  <h1 style="display:flex;align-items:center;gap:9px"><img src="/logo.svg" width="29" height="29" alt="MIRSAD" style="border-radius:7px"><span><b>مِرصاد</b> · خريطة الذاكرة الحية</span></h1>
  <div class="live"><span class="dot"></span><span id="livetxt">متصل — تُبنى الذاكرة أمامك</span></div>
  <div class="legend">
    <span><span class="chip" style="background:var(--blue)"></span>كيان</span>
    <span><span class="chip" style="background:var(--gold)"></span>قرار</span>
    <span><span class="chip" style="background:var(--slate)"></span>قيمة</span>
    <span><span class="chip" style="background:var(--red)"></span>تعارض مفتوح</span>
  </div>
  <div class="stats">
    <span>العقد <b id="st-nodes">–</b></span>
    <span>الروابط <b id="st-edges">–</b></span>
    <span>القرارات <b id="st-dec">–</b></span>
    <span>الحقائق <b id="st-facts">–</b></span>
    <span>تعارضات <b id="st-conf">–</b></span>
  </div>
</header>
<canvas id="map"></canvas>
<aside id="panel" class="hidden" dir="rtl"></aside>
<div class="hint">اسحب للتنقل · عجلة الفأرة للتقريب · اضغط أي عقدة لتقرأ قصتها</div>

<script>
"use strict";
const cv = document.getElementById('map'), ctx = cv.getContext('2d');
let W=0,H=0,DPR=1;
function fit(){ DPR=Math.min(window.devicePixelRatio||1,2); W=window.innerWidth; H=window.innerHeight-58;
  cv.width=W*DPR; cv.height=H*DPR; cv.style.width=W+'px'; cv.style.height=H+'px'; }
window.addEventListener('resize', fit); fit();

const COL = {entity:'#8fc1f0', decision:'#e8c268', value:'#9fb4c8'};
let nodes = new Map(), edges = [], details = {}, version = -1;
let cam = {x:0,y:0,k:1}, selected = null, hover = null;
let pulses = []; // {node, t0}

function place(n, near){
  const a = Math.random()*Math.PI*2;
  if(near){ n.x = near.x + Math.cos(a)*70; n.y = near.y + Math.sin(a)*70; }
  else { n.x = (Math.random()-.5)*W*.6; n.y = (Math.random()-.5)*H*.6; }
  n.vx = 0; n.vy = 0; n.born = performance.now();
}

async function refresh(){
  try{
    const r = await fetch('/graph'); if(!r.ok) return;
    const g = await r.json();
    if(g.version === version && version !== -1) return;
    version = g.version; details = g.details;
    const seen = new Set();
    for(const sn of g.nodes){
      seen.add(sn.id);
      const ex = nodes.get(sn.id);
      if(ex){ Object.assign(ex, sn); }
      else{
        const nearEdge = g.edges.find(e => e.target===sn.id || e.source===sn.id);
        const anchor = nearEdge ? nodes.get(nearEdge.target===sn.id?nearEdge.source:nearEdge.target) : null;
        const n = {...sn}; place(n, anchor||null); nodes.set(sn.id, n);
        pulses.push({node:n, t0:performance.now()});
      }
    }
    for(const id of [...nodes.keys()]) if(!seen.has(id)) nodes.delete(id);
    edges = g.edges;
    document.getElementById('st-nodes').textContent = g.stats.nodes;
    document.getElementById('st-edges').textContent = g.stats.edges;
    document.getElementById('st-dec').textContent = g.stats.decisions;
    document.getElementById('st-facts').textContent = g.stats.facts_current;
    document.getElementById('st-conf').textContent = g.stats.open_conflicts;
    if(selected && details[selected]) showStory(selected);
  }catch(e){ /* server momentarily busy; next poll retries */ }
}
setInterval(refresh, 1200); refresh();

function step(){
  const arr = [...nodes.values()];
  for(let i=0;i<arr.length;i++){
    const a=arr[i];
    for(let j=i+1;j<arr.length;j++){
      const b=arr[j]; let dx=a.x-b.x, dy=a.y-b.y; let d2=dx*dx+dy*dy||1;
      let f=Math.min(2600/d2, 3.2); a.vx+=dx*f*.016; a.vy+=dy*f*.016; b.vx-=dx*f*.016; b.vy-=dy*f*.016;
    }
  }
  for(const e of edges){
    const a=nodes.get(e.source), b=nodes.get(e.target); if(!a||!b) continue;
    let dx=b.x-a.x, dy=b.y-a.y, d=Math.hypot(dx,dy)||1, want = e.kind==='decision'?150:120;
    let f=(d-want)*.012; a.vx+=dx/d*f*d*.08; a.vy+=dy/d*f*d*.08; b.vx-=dx/d*f*d*.08; b.vy-=dy/d*f*d*.08;
  }
  for(const n of arr){
    n.vx += -n.x*.0016; n.vy += -n.y*.0016;
    if(n !== dragNode){ n.x+=n.vx; n.y+=n.vy; }
    n.vx*=.86; n.vy*=.86;
  }
}

function draw(){
  ctx.setTransform(DPR,0,0,DPR,0,0); ctx.clearRect(0,0,W,H);
  ctx.translate(W/2+cam.x, H/2+cam.y); ctx.scale(cam.k, cam.k);
  const now = performance.now();
  for(const e of edges){
    const a=nodes.get(e.source), b=nodes.get(e.target); if(!a||!b) continue;
    ctx.strokeStyle = e.kind==='decision' ? 'rgba(232,194,104,.5)' : 'rgba(143,193,240,.34)';
    ctx.lineWidth = e.kind==='decision'?1.4:1.1;
    ctx.beginPath(); ctx.moveTo(a.x,a.y); ctx.lineTo(b.x,b.y); ctx.stroke();
    const mx=(a.x+b.x)/2, my=(a.y+b.y)/2;
    ctx.fillStyle='rgba(139,163,194,.9)'; ctx.font='10px system-ui';
    ctx.textAlign='center'; ctx.fillText(e.label, mx, my-3);
  }
  pulses = pulses.filter(p => now-p.t0 < 1500);
  for(const p of pulses){
    const t=(now-p.t0)/1500, n=p.node;
    ctx.strokeStyle=`rgba(232,194,104,${.65*(1-t)})`; ctx.lineWidth=2;
    ctx.beginPath(); ctx.arc(n.x,n.y, 14+34*t, 0, Math.PI*2); ctx.stroke();
  }
  for(const n of nodes.values()){
    const r = n.kind==='decision'?13 : n.kind==='entity'?11 : 8;
    const grow = Math.min(1,(now-n.born)/350), rr = r*(0.4+0.6*grow);
    ctx.beginPath(); ctx.arc(n.x,n.y,rr,0,Math.PI*2);
    ctx.fillStyle = COL[n.kind]||'#fff'; ctx.fill();
    ctx.lineWidth = (selected===n.id||hover===n.id)?3:1.4;
    ctx.strokeStyle = n.conflict ? '#e0655f' : 'rgba(11,18,32,.9)'; ctx.stroke();
    if(n.conflict){ ctx.beginPath(); ctx.arc(n.x,n.y,rr+4.5,0,Math.PI*2);
      ctx.strokeStyle='rgba(224,101,95,.55)'; ctx.lineWidth=1.2; ctx.stroke(); }
    ctx.fillStyle='#dbe7f5'; ctx.font=(n.kind==='value'?'10.5px':'12px')+' system-ui';
    ctx.textAlign='center';
    const label = n.label.length>30 ? n.label.slice(0,29)+'…' : n.label;
    ctx.fillText(label, n.x, n.y+rr+15);
  }
  requestAnimationFrame(frame);
}
function frame(){ step(); draw(); }
requestAnimationFrame(frame);

// ---- interaction ----
let dragNode=null, dragMap=null;
function toWorld(mx,my){ return {x:(mx-W/2-cam.x)/cam.k, y:(my-58-H/2-cam.y)/cam.k}; }
function pick(mx,my){ const p=toWorld(mx,my); let best=null,bd=26/cam.k;
  for(const n of nodes.values()){ const d=Math.hypot(n.x-p.x,n.y-p.y); if(d<bd){bd=d;best=n;} } return best; }
cv.addEventListener('mousedown', e=>{
  const n = pick(e.clientX, e.clientY);
  if(n){ dragNode=n; } else { dragMap={x:e.clientX,y:e.clientY,cx:cam.x,cy:cam.y}; }
});
window.addEventListener('mousemove', e=>{
  if(dragNode){ const p=toWorld(e.clientX,e.clientY); dragNode.x=p.x; dragNode.y=p.y; dragNode.vx=dragNode.vy=0; }
  else if(dragMap){ cam.x = dragMap.cx + (e.clientX-dragMap.x); cam.y = dragMap.cy + (e.clientY-dragMap.y); }
  else { const n=pick(e.clientX,e.clientY); hover = n?n.id:null; cv.style.cursor = n?'pointer':'grab'; }
});
window.addEventListener('mouseup', e=>{
  if(dragNode && Math.hypot(e.movementX,e.movementY) < 3){ /* click handled below */ }
  dragNode=null; dragMap=null;
});
cv.addEventListener('click', e=>{
  const n = pick(e.clientX, e.clientY);
  if(n){ selected=n.id; showStory(n.id); }
  else { selected=null; document.getElementById('panel').classList.add('hidden'); }
});
cv.addEventListener('wheel', e=>{
  e.preventDefault();
  cam.k = Math.max(.25, Math.min(2.6, cam.k * (e.deltaY<0?1.1:0.91)));
},{passive:false});

function esc(s){ return String(s??'').replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function showStory(id){
  const d = details[id]; if(!d) return;
  const p = document.getElementById('panel'); p.classList.remove('hidden');
  let h = `<div class="kind">${esc(d.kind)}</div><h2>${esc(d.title||'')}</h2>`;
  if(d.sub) h += `<div class="meta">${esc(d.sub)}</div>`;
  if(d.kind==='entity'){
    h += `<h3>الحقائق الحالية (${(d.facts||[]).length})</h3>`;
    if(!(d.facts||[]).length) h += `<p class="meta">لا حقائق حالية بعد.</p>`;
    for(const f of d.facts||[]) h += `<div class="fact"><span class="pred">${esc(f.predicate)}</span> ← ${esc(f.object_text)}<br><span class="who">✍ ${esc(f.source_agent)} · منذ ${esc(f.valid_from)}</span></div>`;
    h += `<h3>قرارات شارك فيها (${(d.decisions||[]).length})</h3>`;
    if(!(d.decisions||[]).length) h += `<p class="meta">لا قرارات مرتبطة بعد.</p>`;
    h += `<ul>` + (d.decisions||[]).map(t=>`<li>${esc(t)}</li>`).join('') + `</ul>`;
    h += `<p class="meta">إجمالي الحقائق عبر الزمن (بما فيها المنتهية): ${esc(d.timeline_count)}</p>`;
  }
  if(d.kind==='value'){
    h += `<h3>أين ذُكرت هذه القيمة؟</h3>`;
    for(const f of d.facts||[]) h += `<div class="fact">${esc(f.subject)} · <span class="pred">${esc(f.predicate)}</span><br><span class="who">✍ ${esc(f.source_agent)} · منذ ${esc(f.valid_from)}</span></div>`;
  }
  if(d.kind==='decision'){
    const txt = i => typeof i === 'string' ? i : (i.text || '');
    const grp = (name, items) => items && items.length ?
      `<h3>${name} (${items.length})</h3><ul>` + items.map(i=>`<li>${esc(txt(i))}${(typeof i==='object' && i.ref)?` <span class="meta">[${esc(i.ref)}]</span>`:''}</li>`).join('') + `</ul>` : '';
    h += `<p class="meta">اتخذه ${esc(d.made_by)} · ${esc(d.made_at)} · الحالة: ${esc(d.status)}</p>`;
    if(d.description) h += `<p>${esc(d.description)}</p>`;
    h += grp('الأسباب', d.reasons) + grp('الأدلة', d.evidence) + grp('البدائل المرفوضة', d.alternatives_rejected || d.rejected)
       + grp('الآثار', d.effects) + grp('النتائج', d.outcomes);
  }
  p.innerHTML = h;
}
</script>
</body>
</html>
"""
