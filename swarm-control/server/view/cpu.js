import {snapshotFetch} from './snapshot-fetch.js';
import './breadcrumbs.js';
import './host-terminal.js';
import {hardwareLines,probeBrowserGPU} from './hardware.js';
const total=document.getElementById('cpu-total'),coverage=document.getElementById('cpu-coverage'),hosts=document.getElementById('cpu-hosts');
let busy=false;
async function refresh(){
 if(busy)return;busy=true;
 try{
  const response=await snapshotFetch('/api/cpu',{cache:'no-store'});
  if(!response.ok)throw Error('CPU monitoring unavailable');
  const data=await response.json();
  document.dispatchEvent(new CustomEvent('cpu-sample',{detail:data}));
  total.textContent=data.total_percent==null?'Unavailable':data.total_percent.toFixed(1)+'%';
  coverage.textContent=data.live_hosts+' / '+data.total_hosts+' hosts measured'+(data.live_hosts<data.total_hosts?' · Partial total':'');
  hosts.replaceChildren();
  for(const host of data.hosts){
   const item=document.createElement('div');item.className='cpu-host';item.dataset.terminalHost=host.host;item.tabIndex=0;item.setAttribute('aria-haspopup','dialog');
   const label=document.createElement('span');label.textContent=host.host+' · '+(host.percent==null?host.status:host.percent.toFixed(1)+'% · '+host.cpus+' CPUs');
   const meter=document.createElement('meter');meter.min=0;meter.max=100;meter.value=host.percent??0;meter.setAttribute('aria-label',host.host+' CPU utilization');meter.hidden=host.percent==null;
   item.append(label,meter);
   for(const line of hardwareLines(host.hardware)){const detail=document.createElement('small');detail.textContent=line;item.append(detail);}
   hosts.append(item);
  }
 }catch(error){total.textContent='Unavailable';coverage.textContent='CPU readout disconnected';hosts.replaceChildren();}
 finally{busy=false;}
}
setInterval(()=>{if(document.visibilityState==='visible')void refresh();},5000);
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')void refresh();});
void refresh();
const browserPanel=document.createElement('div');browserPanel.className='browser-gpu';
const browserStatus=document.createElement('span');browserStatus.textContent='This browser: '+(navigator.gpu?'WebGPU available; compute untested':'WebGPU unavailable in this context');
const button=document.createElement('button');button.type='button';button.textContent='Test this browser’s GPU';
button.addEventListener('click',async()=>{button.disabled=true;try{const result=await probeBrowserGPU();browserStatus.textContent=result.status+': '+result.reason;}catch(error){browserStatus.textContent='GPU check failed: '+error.message;}finally{button.disabled=false;}});
browserPanel.append(browserStatus,button);document.getElementById('cpu-readout').append(browserPanel);
