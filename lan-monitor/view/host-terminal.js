import {breadcrumbNav,setBreadcrumbs} from './breadcrumbs.js';
import {cpuColor} from './cpu-colors.js';
let host=null,report=null,cpuReport=null,busy=false,hideTimer,openTimer,pendingHost=null,pinned=false;
const panel=document.createElement('section');panel.id='host-terminal';panel.hidden=true;panel.setAttribute('role','dialog');panel.setAttribute('aria-label','Machine test terminal');
const heading=document.createElement('strong'),status=document.createElement('p'),output=document.createElement('pre'),close=document.createElement('button');
close.type='button';close.textContent='Close';output.tabIndex=0;output.setAttribute('aria-label','Machine terminal output');
const cpuSummary=document.createElement('p'),grid=document.createElement('div'),legend=document.createElement('div');grid.className='logical-cpu-grid';grid.setAttribute('aria-label','Logical CPU utilization');legend.className='cpu-heat-legend';legend.textContent='0% · Black → Red → Orange → Yellow → White · 100%';
const containersTitle=document.createElement('h3'),containerGrid=document.createElement('div');containersTitle.textContent='Running containers';containerGrid.className='container-grid';
const pin=document.createElement('button'),hint=document.createElement('p');pin.type='button';pin.textContent='Pin open';pin.className='preview-pin';hint.className='preview-hint';hint.textContent='Hover to preview · Click a machine to pin · Esc to close';pin.onclick=()=>{pinned=!pinned;pin.textContent=pinned?'Unpin':'Pin open';panel.classList.toggle('is-pinned',pinned);};
const machineCrumbs=breadcrumbNav('Machine inspection');
panel.append(close,pin,heading,machineCrumbs,hint,cpuSummary,grid,legend,containersTitle,containerGrid,status,output);document.body.append(panel);
function render(){
 if(!host)return;
 setBreadcrumbs(machineCrumbs,[{label:'Network',action:dismiss},{label:host}]);
 const machine=report?.run?.hosts?.find(item=>item.host===host);
 heading.textContent=host+' · CPU & test terminal';
 const cpu=cpuReport?.hosts?.find(item=>item.host===host);
 cpuSummary.textContent=cpu?.status==='live'?cpu.cpus+' logical CPUs · '+cpu.percent.toFixed(1)+'% total utilization':'CPU measurements '+(cpu?.status||'unavailable');
 grid.replaceChildren();
 if(cpu?.status==='live')for(const core of cpu.logical_cpus||[]){
  const cell=document.createElement('div');cell.className='logical-cpu-cell';cell.style.backgroundColor=cpuColor(core.percent);cell.style.color=core.percent>=65?'#171009':'#fff';
  cell.textContent='CPU '+core.id+'\n'+(core.percent==null?'Unknown':core.percent.toFixed(1)+'%');
  cell.title='Logical CPU '+core.id+' · '+(core.percent==null?'Unavailable':core.percent+'% utilization');grid.append(cell);
 }

 status.textContent=machine?(machine.status+' · '+(report.run.finished?'Recorded output':'Live output · updates every second')):'No distributed test output for this machine.';
 const jobs=Object.values(machine?.jobs||{}),active=jobs.filter(job=>job.status==='running');
 const text=(active.length?active:jobs).map(job=>'── '+job.name+' · '+job.status+' · '+(job.phase||'waiting')+' ──\n'+((job.logs||[]).join('\n')||'Waiting for output…')).join('\n\n')||'Start application tests to collect output. This panel shows test process output, not an interactive shell.';
 const follow=output.scrollHeight-output.scrollTop-output.clientHeight<40;
 if(output.textContent!==text){output.textContent=text;if(follow)output.scrollTop=output.scrollHeight;}
}
async function refresh(){
 if(!host||busy)return;busy=true;
 const results=await Promise.allSettled(['/api/distributed-tests','/api/cpu'].map(async url=>{const response=await fetch(url,{cache:'no-store'});if(!response.ok)throw Error();return response.json();}));
 report=results[0].status==='fulfilled'?results[0].value:null;
 cpuReport=results[1].status==='fulfilled'?results[1].value:null;
 render();if(results[0].status==='rejected')status.textContent='Disconnected · could not refresh terminal output';busy=false;
}
function openHost(name){
 const changed=host!==name;host=name;panel.hidden=false;
 if(changed){output.textContent='';render();void refresh();}
}
export function showHostTerminal(name,immediate=false){
 clearTimeout(hideTimer);if(!name||(pinned&&name!==host))return;
 if(immediate){clearTimeout(openTimer);pendingHost=null;openHost(name);return;}
 if(name===host&&!panel.hidden||pendingHost===name)return;
 clearTimeout(openTimer);pendingHost=name;
 openTimer=setTimeout(()=>{pendingHost=null;openHost(name);},300);
}
export function pinHostTerminal(name){pinned=false;showHostTerminal(name,true);pinned=true;pin.textContent='Unpin';panel.classList.add('is-pinned');}
export function hideHostTerminal(){
 clearTimeout(openTimer);pendingHost=null;if(pinned)return;
 clearTimeout(hideTimer);hideTimer=setTimeout(()=>{panel.hidden=true;host=null;},650);
}
function dismiss(){clearTimeout(openTimer);clearTimeout(hideTimer);pendingHost=null;pinned=false;pin.textContent='Pin open';panel.classList.remove('is-pinned');panel.hidden=true;host=null;}
close.onclick=dismiss;panel.onpointerenter=()=>{clearTimeout(hideTimer);clearTimeout(openTimer);pendingHost=null;};panel.onpointerleave=hideHostTerminal;
document.addEventListener('keydown',event=>{if(event.key==='Escape')dismiss();
 const node=event.target.closest('[data-terminal-host]');if(node&&(event.key==='Enter'||event.key===' ')){event.preventDefault();pinned=false;showHostTerminal(node.dataset.terminalHost,true);pin.click();}
});
document.addEventListener('pointerover',event=>{const node=event.target.closest('[data-terminal-host]');if(node)showHostTerminal(node.dataset.terminalHost);});
document.addEventListener('pointermove',event=>{const node=event.target.closest('[data-terminal-host]');if(node)showHostTerminal(node.dataset.terminalHost);else if(event.target.closest('#host-terminal,#container-hover'))clearTimeout(hideTimer);else if(event.target.tagName!=='CANVAS')hideHostTerminal();});
document.addEventListener('focusin',event=>{const node=event.target.closest('[data-terminal-host]');if(node)showHostTerminal(node.dataset.terminalHost,true);});
document.addEventListener('click',event=>{const node=event.target.closest('[data-terminal-host]');if(node){pinned=false;showHostTerminal(node.dataset.terminalHost,true);pin.click();}});
setInterval(()=>{if(!panel.hidden&&document.visibilityState==='visible')void refresh();},1000);

let selectedContainer=null,containerHost=null,containerBusy=false;
const containerPanel=document.createElement('section');containerPanel.id='container-hover';containerPanel.hidden=true;containerPanel.setAttribute('role','dialog');containerPanel.setAttribute('aria-label','Container details and console');
const containerHeading=document.createElement('strong'),containerDetails=document.createElement('pre'),containerConsole=document.createElement('pre'),containerClose=document.createElement('button');
containerClose.textContent='Close container';containerClose.type='button';containerConsole.tabIndex=0;containerConsole.setAttribute('aria-label','Container console output');
const consoleTools=document.createElement('div'),consoleSearch=document.createElement('input'),consolePause=document.createElement('button'),consoleInfo=document.createElement('span');
consoleTools.className='console-tools';consoleSearch.type='search';consoleSearch.placeholder='Filter console output';consoleSearch.setAttribute('aria-label','Filter console output');consolePause.type='button';consolePause.textContent='Pause output';let consolePaused=false,consoleText='';
function renderConsole(){const query=consoleSearch.value.toLowerCase(),lines=consoleText.split('\n');const visible=query?lines.filter(line=>line.toLowerCase().includes(query)):lines;const follow=containerConsole.scrollHeight-containerConsole.scrollTop-containerConsole.clientHeight<40;containerConsole.textContent=visible.join('\n')||'No matching output';consoleInfo.textContent=consolePaused?'Paused':query?visible.length+' matching lines':'Live · latest 100 lines';if(follow)containerConsole.scrollTop=containerConsole.scrollHeight;}
consoleSearch.oninput=renderConsole;consolePause.onclick=()=>{consolePaused=!consolePaused;consolePause.textContent=consolePaused?'Resume output':'Pause output';renderConsole();};
consoleTools.append(consoleSearch,consolePause,consoleInfo);
const containerCrumbs=breadcrumbNav('Container inspection');
containerPanel.append(containerClose,containerHeading,containerCrumbs,containerDetails,consoleTools,containerConsole);document.body.append(containerPanel);
function backToMachine(){selectedContainer=null;containerPanel.hidden=true;pinHostTerminal(host);pin.focus({preventScroll:true});}
containerClose.onclick=backToMachine;
containerPanel.onpointerenter=()=>clearTimeout(hideTimer);
containerPanel.onpointerleave=hideHostTerminal;
async function refreshContainers(){
 if(!host||panel.hidden||containerBusy)return;containerBusy=true;
 const requestedHost=host,requestedId=selectedContainer;
 try{
  const response=await fetch('/api/containers?host='+encodeURIComponent(requestedHost)+(requestedId?'&id='+requestedId:''),{cache:'no-store'});
  if(!response.ok)throw Error();const data=await response.json();
  if(host!==requestedHost||selectedContainer!==requestedId)return;
  containersTitle.textContent='Running containers · '+(data.status==='live'?data.containers.length:'Unavailable');
  const signature=JSON.stringify(data.containers);
  if(containerGrid.dataset.signature!==signature){
   containerGrid.dataset.signature=signature;containerGrid.replaceChildren();
   for(const item of data.containers){
    const button=document.createElement('button');button.type='button';button.className='container-tile';button.textContent=item.name+'\n'+item.image;button.setAttribute('aria-haspopup','dialog');
    const open=()=>{if(selectedContainer===item.id&&!containerPanel.hidden)return;selectedContainer=item.id;containerHeading.textContent=item.name;containerDetails.textContent=item.image;containerConsole.textContent='Loading console…';containerPanel.hidden=false;setBreadcrumbs(containerCrumbs,[{label:'Network',action:()=>{dismiss();containerPanel.hidden=true;selectedContainer=null;}},{label:host,action:backToMachine},{label:item.name}]);setTimeout(()=>void refreshContainers(),0);};
    let previewTimer;button.onpointerenter=()=>{previewTimer=setTimeout(open,250);};button.onpointerleave=()=>clearTimeout(previewTimer);button.onfocus=open;button.onclick=()=>{open();pinned=true;pin.textContent='Unpin';panel.classList.add('is-pinned');};containerGrid.append(button);
   }
  }
  if(requestedId&&!data.detail){containerDetails.textContent='Container stopped or is no longer available.';containerConsole.textContent='';}
  if(data.detail){
   const detail=data.detail;containerHeading.textContent=detail.name+' · '+requestedHost;
   containerDetails.textContent=JSON.stringify({image:detail.image,id:detail.id,state:detail.state,health:detail.health,started:detail.started_at,restarts:detail.restart_count,ports:detail.ports},null,2);
   if(!consolePaused){consoleText=detail.console||'No console output recorded.';renderConsole();}
  }
 }catch{if(host===requestedHost){containersTitle.textContent='Running containers · Disconnected';containerGrid.replaceChildren();containerGrid.dataset.signature='';if(requestedId)containerConsole.textContent='Console unavailable · retrying…';}}
 finally{containerBusy=false;}
}
setInterval(()=>{
 if(!host||panel.hidden){containerPanel.hidden=true;selectedContainer=null;containerHost=null;return;}
 if(containerHost!==host){containerHost=host;selectedContainer=null;containerPanel.hidden=true;containerGrid.replaceChildren();containerGrid.dataset.signature='';containersTitle.textContent='Running containers · Loading…';}
 if(document.visibilityState==='visible')void refreshContainers();
},1000);
document.addEventListener('keydown',event=>{if(event.key==='Escape'){selectedContainer=null;containerPanel.hidden=true;}});
