import './machine-heat.js';
import './host-terminal.js';
const $=id=>document.getElementById(id);
const terminal=new Set(['passed','failed','unavailable','timed_out','interrupted','stopped']);
const suiteEntries=host=>Object.entries(host.jobs||{}).sort(([,a],[,b])=>Number(terminal.has(a.status))-Number(terminal.has(b.status)));
let data=null,selected=null,chosen='',polling=false;
let terminalChoice='';
function renderTerminal(run){
 const jobs=(run?.hosts||[]).flatMap(host=>suiteEntries(host).map(([id,job])=>({key:host.host+'/'+id,host:host.host,job})));
 const selector=$('terminal-job');
 if(document.activeElement!==selector){selector.replaceChildren(new Option('All running suites',''));for(const item of jobs)selector.append(new Option(item.host+' · '+item.job.name+' · '+label(item.job.status),item.key));selector.value=terminalChoice;}
 const active=jobs.filter(item=>['running','preparing','transferring'].includes(item.job.status));
 const visible=terminalChoice?jobs.filter(item=>item.key===terminalChoice):(active.length?active:jobs);
 const output=visible.map(({host,job})=>'── '+host+' · '+job.name+' · '+label(job.status)+' · '+(job.phase||'waiting')+' ──\n'+((job.logs||[]).join('\n')||'Waiting for process output…')).join('\n\n');
 const pane=$('live-terminal');
 if(pane.textContent!==output){pane.textContent=output||'No test output yet.';if($('follow-output').checked)pane.scrollTop=pane.scrollHeight;}
 $('terminal-status').textContent=(run?.status==='running'?'Live · refreshes every second':'Recorded output')+' · '+active.length+' active suites · Latest 80 lines per suite';
}
$('terminal-job').onchange=()=>{terminalChoice=$('terminal-job').value;renderTerminal(data?.run);};
const el=(tag,text='',className='')=>{const node=document.createElement(tag);node.textContent=text;if(className)node.className=className;return node;};
const label=status=>({stopped:'Stopped',stopping:'Stopping',timed_out:'Timed out',unavailable:'Unavailable',interrupted:'Interrupted',queued:'Queued',transferring:'Transferring',preparing:'Preparing',running:'Running',passed:'Passed',failed:'Failed'}[status]||'Unknown');
const badge=status=>el('span',label(status),'badge '+status);
const seconds=value=>Number.isFinite(value)?value<60?value.toFixed(1)+' s':(value/60).toFixed(1)+' min':'—';
const duration=job=>job.duration_seconds??(job.started&&!terminal.has(job.status)?Math.max(0,(Date.now()-Date.parse(job.started))/1000):null);
function coordinatorNode(){
 const node=el('button','','host coordinator-host');node.type='button';node.dataset.terminalHost='compai.local';node.setAttribute('aria-haspopup','dialog');node.setAttribute('aria-pressed',String(selected==='compai.local'));
 node.append(el('strong','compai.local'),el('small','Local machine · Test coordinator'),el('span','Coordinator','badge passed'),el('small','Hover for live CPUs, containers, and console'));
 node.onclick=()=>{selected='compai.local';render();};return node;
}
function render(){
 const run=data?.run;
 document.dispatchEvent(new CustomEvent('test-context',{detail:{run,host:selected}}));
 renderTerminal(run);
 $('run').disabled=Boolean(data?.running);$('run').textContent=data?.running?'Tests running…':'Start all tests';
 $('stop').disabled=!data?.running||data?.run?.status==='stopping';
 $('stop').textContent=data?.run?.status==='stopping'?'Stopping…':'Stop all tests';
 if(document.activeElement!==$('history')){
  $('history').replaceChildren(new Option('Latest run',''));
  for(const item of data.history||[])$('history').append(new Option(new Date(item.started).toLocaleString()+' · '+label(item.status),item.id));
  $('history').value=chosen;
 }
 if(!run){$('status').textContent='No distributed run yet. Run application tests to test the saved SSH machines.';$('topology').replaceChildren(...(hosts.some(host=>host.local)?[]:[coordinatorNode()]));return;}
 $('status').textContent=label(run.status)+' · Started '+new Date(run.started).toLocaleString()+(run.finished?' · Finished '+new Date(run.finished).toLocaleString():'');
 const image=run.image_progress;
 if(image&&image.phase!=='ready')$('status').textContent+=' · Image '+image.phase+': '+image.image+(image.source?' · '+image.source:'')+(image.destination?' → '+image.destination:'');
 $('error').textContent=run.error||'';
 if(data.running&&run.preparation)$('status').append(' · '+run.preparation);
 const hosts=run.hosts||[];
 $('command-scope').textContent='All '+hosts.length+' worker machines · compai coordinates and runs suites';
 const jobs=hosts.flatMap(host=>Object.values(host.jobs||{}));
 $('summary').replaceChildren();
 for(const [name,value]of [['Worker machines',hosts.length],['Passed suites',jobs.filter(j=>j.status==='passed').length],['Failed / unavailable',jobs.filter(j=>['failed','unavailable','timed_out','interrupted'].includes(j.status)).length],['Completed',jobs.filter(j=>terminal.has(j.status)).length+' / '+jobs.length],['Snapshot files',run.source_files??'Preparing…']]){const card=el('div','','metric');card.append(el('span',name),el('strong',String(value)));$('summary').append(card);}
 if(selected!=='compai.local'&&!hosts.some(host=>host.host===selected))selected=hosts[0]?.host;
 const focusedHost=document.activeElement?.closest('#topology [data-terminal-host]')?.dataset.terminalHost;
 $('topology').replaceChildren(...(hosts.some(host=>host.local)?[]:[coordinatorNode()]));
 for(const host of hosts){const node=el('button','','host '+host.status);node.type='button';node.dataset.terminalHost=host.host;node.setAttribute('aria-pressed',String(host.host===selected));node.append(el('strong',host.host),el('small',host.user+(host.local?' · Local coordinator and worker':'')+' · '+(host.observed_hostname||'Identity not observed yet')),badge(host.status));const jobs=Object.values(host.jobs||{}),finished=jobs.filter(j=>terminal.has(j.status)).length;const progress=el('div','','host-progress'),fill=el('span');fill.style.width=(jobs.length?100*finished/jobs.length:0)+'%';progress.append(fill);if(host.session)node.append(el('small','Launcher session · '+host.session.status+(host.session.error?' · '+host.session.error:'')));node.append(progress,el('small',finished+' / '+jobs.length+' suites finished · '+seconds(duration(host))));
 for(const completed of [false,true]){
 const entries=suiteEntries(host).filter(([,job])=>terminal.has(job.status)===completed);
 if(!entries.length)continue;
 const suites=el('span','','machine-suites');suites.setAttribute('role','list');suites.setAttribute('aria-label',host.host+(completed?' completed suites':' pending suites'));
 if(completed)suites.append(el('span','Completed · '+entries.length,'suite-group-label'));
 for(const [id,job] of entries){
  const tile=el('span','','suite-tile '+job.status);tile.dataset.suite=id;tile.setAttribute('role','listitem');
  tile.append(el('strong',job.name),el('span',label(job.status),'suite-state'));
  const counts=job.test_counts;
  const detail=[job.phase,counts?`${counts.passed??'—'} passed · ${counts.failed??0} failed`:null].filter(Boolean).join(' · ');
  if(detail)tile.append(el('small',detail));
  tile.title=job.name+' · '+label(job.status)+(detail?' · '+detail:'');suites.append(tile);
 }
 node.append(suites);}
 node.onclick=()=>{selected=host.host;render();};$('topology').append(node);}
 if(focusedHost)Array.from($('topology').children).find(node=>node.dataset.terminalHost===focusedHost)?.focus({preventScroll:true});
 $('updated').textContent=run.updated?'Last result '+new Date(run.updated).toLocaleTimeString():'';
 const completedEverywhere=id=>hosts.filter(host=>host.jobs?.[id]).every(host=>terminal.has(host.jobs[id].status));
 const ids=[...new Set(hosts.flatMap(host=>host.suites||[]))].sort((a,b)=>Number(completedEverywhere(a))-Number(completedEverywhere(b)));const header=el('tr');header.append(el('th','Application suite'));for(const host of hosts)header.append(el('th',host.host));$('matrix-head').replaceChildren(header);$('matrix').replaceChildren();
 for(const id of ids){const row=el('tr');row.append(el('th',hosts.find(h=>h.jobs[id])?.jobs[id].name||id));for(const host of hosts){const cell=el('td'),job=host.jobs[id];if(job){cell.append(badge(job.status),el('small',(job.phase||'Waiting')+' · '+seconds(duration(job))+(job.test_counts?' · '+(job.test_counts.passed??'—')+' passed / '+(job.test_counts.failed??0)+' failed / '+(job.test_counts.skipped??0)+' skipped':'')));}else cell.append(el('span','Not assigned','muted'));row.append(cell);}$('matrix').append(row);}
 $('timeline').replaceChildren();const first=Date.parse(run.started),end=run.finished?Date.parse(run.finished):Date.now(),span=Math.max(1,(end-first)/1000);
 for(const host of hosts)for(const [,job] of suiteEntries(host)){if(!job.started)continue;const row=el('div','','timeline-row'),track=el('div','','track'),bar=el('span','','bar '+job.status);bar.style.marginLeft=Math.max(0,100*(Date.parse(job.started)-first)/1000/span)+'%';bar.style.width=Math.min(100,100*(duration(job)||0)/span)+'%';track.append(bar);row.append(el('span',host.host+' · '+job.name),track,el('span',seconds(duration(job))));$('timeline').append(row);}
 const host=hosts.find(host=>host.host===selected);$('jobs').replaceChildren();
 if(selected==='compai.local'){$('details-title').textContent='compai.local';$('details-meta').textContent='Local test coordinator · Hover for CPU utilization, running containers, and container console output.';$('jobs').append(el('p','Dispatches source snapshots and collects results from the SSH machines.','muted'));}
 if(host){$('details-title').textContent=host.host;$('details-meta').textContent=[host.user+(host.local?' · Local execution':' via saved LAN key'),host.cpus?host.cpus+' CPUs':null,host.python?'Host Python '+host.python:null,host.error].filter(Boolean).join(' · ');let completedHeading=false;for(const [,job] of suiteEntries(host)){if(terminal.has(job.status)&&!completedHeading){$('jobs').append(el('h3','Completed suites','completed-suites-heading'));completedHeading=true;}const details=el('details','','job'),title=el('summary');title.append(el('span',job.name+' · '+(job.phase||'Waiting')+' · '+seconds(duration(job))+(job.test_counts?' · '+(job.test_counts.passed??'—')+' passed / '+(job.test_counts.failed??0)+' failed / '+(job.test_counts.skipped??0)+' skipped':'')),badge(job.status));details.append(title,el('pre',(job.failure_details?.length?'Failure details:\n'+job.failure_details.join('\n')+'\n\nRecent output:\n':'')+(job.logs||[]).join('\n')||'No output recorded yet.'));$('jobs').append(details);}}
 $('snapshot').textContent=run.source_sha256?'Source SHA-256 '+run.source_sha256:'Preparing source snapshot…';
 $('scope').textContent=(run.scope||'')+' '+Object.entries(run.repositories||{}).map(([name,repo])=>(name==='.'?'Organization':name)+' '+repo.commit.slice(0,12)+(repo.dirty?' + working changes':'')).join(' · ');
}
async function refresh(){if(polling)return;polling=true;try{const response=await fetch('/api/distributed-tests'+(chosen?'?id='+encodeURIComponent(chosen):''),{cache:'no-store'});if(!response.ok)throw Error('Could not load distributed test results ('+response.status+')');data=await response.json();render();}catch(error){$('error').textContent=error.message;}finally{polling=false;}}
$('run').onclick=async()=>{if(data?.running)return;$('run').disabled=true;try{const response=await fetch('/api/distributed-tests/start',{method:'POST'});if(!response.ok)throw Error(await response.text());chosen='';data=await response.json();render();}catch(error){$('error').textContent=error.message;$('run').disabled=false;}};
$('history').onchange=()=>{chosen=$('history').value;void refresh();};
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')void refresh();});
setInterval(()=>{if(document.visibilityState==='visible')void refresh();},1000);
void refresh();

$('stop').onclick=async()=>{$('stop').disabled=true;try{const response=await fetch('/api/distributed-tests/stop',{method:'POST'});if(!response.ok)throw Error(await response.text());chosen='';data=await response.json();render();}catch(error){$('error').textContent=error.message;void refresh();}};

$('test-commands').append($('run'),$('stop'));
