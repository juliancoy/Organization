import {deviceLevels,deviceColor} from './levels-model.js';
import {renderDockerDownloads} from './docker-downloads.js';
const root=document.getElementById('device-levels');
root.innerHTML=`<div class="panel-heading"><div><div class="eyebrow">ALL DEVICES · LIVE OVERLAY</div><h2>Device levels</h2><p id="levels-description" class="muted"></p></div><label>Measure <select id="levels-mode"><option value="network">Network</option><option value="disk">Disk</option><option value="ram">RAM</option><option value="cpu">CPU (%)</option></select></label></div><section id="levels-docker" aria-label="Docker image download context"></section><div class="levels-key"><span id="levels-scale"></span><span id="levels-status" role="status">Connecting…</span></div><div id="levels-plot" tabindex="0" aria-label="Overlaid device measurements"></div><div class="levels-time"><span id="levels-window">Collecting history…</span><span>Now</span></div><div id="levels-legend"></div><p id="levels-coverage" class="muted"></p>`;
const selector=document.getElementById('levels-mode'),histories={network:new Map(),disk:new Map(),ram:new Map(),cpu:new Map()},hidden=new Set();
let mode='network',latest=[],busy=false,lastData=null;
try{const saved=localStorage.getItem('lan-map.levels');if(['network','disk','ram','cpu'].includes(saved))mode=saved;}catch{}
selector.value=mode;
selector.onchange=()=>{mode=selector.value;try{localStorage.setItem('lan-map.levels',mode);}catch{}if(lastData)update(...lastData,false);};
function draw(){
 const now=Date.now(),history=histories[mode],visible=latest.filter(r=>!hidden.has(r.id));
 const starts=visible.flatMap(r=>(history.get(r.id)||[]).map(p=>p.time));
 const span=Math.min(300000,Math.max(3000,now-(starts.length?Math.min(...starts):now)));
 document.getElementById('levels-window').textContent=(span<60000?Math.ceil(span/1000)+' seconds':(span/60000).toFixed(1)+' minutes')+' ago'+(span<299000?' · building 5-minute history':'');
 const peak=mode==='network'?Math.max(1,...visible.flatMap(r=>(history.get(r.id)||[]).map(p=>p.value||0)))*1.1:100;
 const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 1000 270');svg.setAttribute('preserveAspectRatio','none');svg.setAttribute('role','img');svg.setAttribute('aria-label',mode+' levels for '+visible.length+' devices over five minutes');
 const add=(tag,attrs,text)=>{const e=document.createElementNS(ns,tag);for(const [k,v]of Object.entries(attrs))e.setAttribute(k,v);if(text)e.textContent=text;svg.append(e);return e;};
 for(let i=0;i<=4;i++){const y=15+i*58;add('line',{x1:58,x2:990,y1:y,y2:y,stroke:'#ffffff10'});add('text',{x:48,y:y+4,'text-anchor':'end',fill:'#91a4b2','font-size':11},(peak*(4-i)/4).toFixed(peak>10?0:1));}
 for(const row of visible){const points=(history.get(row.id)||[]).filter(p=>p.time>=now-300000);let segment=[];
 const flush=()=>{if(!segment.length)return;const line=segment.map((p,i)=>(i?'L':'M')+p).join(' '),color=deviceColor(row.id);if(segment.length>1)add('path',{d:line+' L'+segment.at(-1).split(',')[0]+',247 L'+segment[0].split(',')[0]+',247 Z',fill:color,'fill-opacity':.10});const path=add('path',{d:line,fill:'none',stroke:color,'stroke-width':2,'vector-effect':'non-scaling-stroke'});const title=document.createElementNS(ns,'title');title.textContent=row.name;path.append(title);segment=[];};
 let prev=null;for(const p of points){if(p.value===null||prev!==null&&p.time-prev>10000)flush();if(p.value!==null)segment.push((58+932*(p.time-now+span)/span).toFixed(2)+','+(247-232*p.value/peak).toFixed(2));prev=p.time;}flush();
 const last=points.at(-1);if(last?.value!==null&&last&&now-last.time<10000)add('circle',{cx:58+932*(last.time-now+span)/span,cy:247-232*last.value/peak,r:3,fill:deviceColor(row.id)});
 }
 document.getElementById('levels-plot').replaceChildren(svg);
 document.getElementById('levels-scale').textContent=mode==='network'?'Mb/s · receive + send · shared auto-scale':mode==='disk'?'% I/O busy · busiest physical disk':mode==='cpu'?'% CPU busy · across all logical cores':'% RAM used · excludes available cache';
}
function update(data,metrics,record=true){
 renderDockerDownloads(document.getElementById('levels-docker'),data,mode);
 lastData=[data,metrics];latest=deviceLevels(data,metrics,mode);const now=Date.now();
 if(record)for(const metric of ['network','disk','ram','cpu'])for(const row of deviceLevels(data,metrics,metric)){const h=histories[metric];if(!h.has(row.id))h.set(row.id,[]);const points=h.get(row.id);points.push({time:now,value:row.value});while(points.length&&points[0].time<now-300000)points.shift();}
 document.getElementById('levels-description').textContent=mode==='network'?'Every measured device on one time axis. Each color follows the same device across all four views.':mode==='disk'?'Physical disk activity over time. 100% means a disk was busy for the entire sampling interval.':mode==='cpu'?'CPU utilization across all logical cores. 100% means all cores were busy for the entire sampling interval.':'Memory in use over time. Available memory, including reclaimable cache, is excluded.';
 const legend=document.getElementById('levels-legend');legend.replaceChildren();
 for(const row of latest){const b=document.createElement('button');b.className='levels-device';b.style.setProperty('--device-color',deviceColor(row.id));b.setAttribute('aria-pressed',String(!hidden.has(row.id)));b.title=row.basis+' · click to hide or show';const name=document.createElement('span');name.textContent=row.name;const value=document.createElement('strong');value.textContent=row.value===null?'No data':row.value.toFixed(1)+(mode==='network'?' Mb/s':'%');b.append(name,value);b.onclick=()=>{hidden.has(row.id)?hidden.delete(row.id):hidden.add(row.id);update(...lastData,false);};legend.append(b);}
 const count=latest.filter(r=>r.value!==null).length;
 document.getElementById('levels-coverage').textContent=count+' / '+latest.length+' devices measured. '+(mode==='network'?'Hosts with telemetry report physical-interface totals; other devices use gateway-visible traffic, which excludes switched traffic. These scopes differ; values are not additive.':'CPU, disk, and RAM require live host telemetry. Devices without telemetry remain listed as “No data”.')+' History is kept in this tab for five minutes.';
 document.getElementById('levels-status').textContent='Updated '+new Date().toLocaleTimeString();draw();
}
async function refresh(){if(busy||document.hidden)return;busy=true;try{const responses=await Promise.all(['/api/devices?resolve=0','/api/cpu'].map(async url=>{const r=await fetch(url,{cache:'no-store',signal:AbortSignal.timeout(6000)});if(!r.ok)throw Error();return r.json();}));update(...responses);}catch{if(lastData)update({...lastData[0],link_traffic:null,docker_downloads:{status:'unavailable'}}, {hosts:lastData[1].hosts.map(h=>({...h,status:'unavailable'}))});document.getElementById('levels-status').textContent='Disconnected · retrying';}finally{busy=false;}}
setInterval(refresh,3000);document.addEventListener('visibilitychange',refresh);void refresh();
