import {statusClass,progressText,groupRows,readEta} from './task-display.js';
const el=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
const $=id=>document.getElementById(id);
const previousPercent=new Map(),previousState=new Map(),openCards=new Map(),closedGroups=new Set();
let latest=null,paused=false,feed=null,polling=false,first=true;
function matches(row){const filter=$('task-filter').value,query=$('task-search').value.toLowerCase();return (filter==='all'||statusClass(row.status)==='status-'+filter)&&(!query||JSON.stringify(row).toLowerCase().includes(query));}
function visible(row){return matches(row)||(row.children||[]).some(visible);}
function badge(status){const n=el('span',status||'Unknown');n.className='badge '+statusClass(status);return n;}
function pct(value){return Number.isFinite(value)?Math.max(0,Math.min(100,value)):null;}
function meter(value,label,eta){const n=el('div');n.className='progress-track';n.role='progressbar';n.setAttribute('aria-label',label);n.setAttribute('aria-valuemin','0');n.setAttribute('aria-valuemax','100');n.setAttribute('aria-valuenow',String(value??0));if(value==null)n.removeAttribute('aria-valuenow');const fill=el('div');fill.className='progress-fill';fill.style.width=(value??0)+'%';n.append(fill);if(eta){n.classList.add('task-meter');const text=el('span',eta.text);text.className='meter-label';n.append(text);n.setAttribute('aria-valuetext',(value==null?'Progress unmeasured':value+'%')+' · '+eta.text);}return n;}
function workerName(name,row){return name&&name!=='unassigned'?name.replaceAll('.local',''):row.children?.length?'Workers shown in steps':statusClass(row.status)==='status-completed'?'Verified evidence':row.status==='not scheduled'?'Not scheduled':'Awaiting worker';}
function renderTask(task,changes){
 const box=el('article');box.className='job-card '+statusClass(task.status);box.dataset.task=task.id;
 const header=el('div');header.className='job-header';const title=el('div');title.className='job-title';title.append(el('h3',task.name));header.append(title,badge(task.status));box.append(header);
 const p=task.progress,percent=pct(p?.work_percent??p?.percent);const observedAt=task.updated||latest?.sampled_at;const metrics=el('div');metrics.className='job-metrics';
 if(p){const count=el('span');count.append(el('strong',`${p.completed} of ${p.total}`),document.createTextNode(' work items verified'));metrics.append(count);if(percent!=null)metrics.append(el('strong',percent+'%'));}else metrics.append(el('span','Completion not yet measured'));
 box.append(metrics,meter(percent,(p?.work_percent!=null?'Input scan: ':'Verified completion: ')+task.name,readEta(task,observedAt)));
 if(p?.work_percent!=null&&statusClass(task.status)!=='status-completed'){const scan=el('p');scan.className='scan-summary';scan.append(el('strong',p.work_percent+'% input scanned'),document.createTextNode(' · '+progressText(p)));box.append(scan);}
 if(task.stale){const stale=el('p','Observation is stale — awaiting a fresh worker report');stale.className='warning';box.append(stale);}
 const rows=groupRows(task.children||[]);
 if(rows.length||task.detail){const details=el('details');details.className='job-details';details.open=openCards.get(task.id)??statusClass(task.status)==='status-running';details.ontoggle=()=>openCards.set(task.id,details.open);const summary=el('summary','Steps & details');summary.dataset.focus='summary:'+task.id;details.append(summary);if(task.detail){const desc=el('p',task.detail);desc.className='job-description';details.append(desc);}const tree=el('div');tree.className='step-tree';
 function add(child,path,depth=0){if(!visible(child))return null;const key=task.id+':'+path;const branch=el('div');branch.className='step '+statusClass(child.status);const row=el('div');row.className='step-row';const name=el('div');name.className='step-name';const nested=child.children?.length;
 const disclosure=el('details');disclosure.className='step-disclosure';disclosure.open=openCards.get(key)??(statusClass(child.status)==='status-running');disclosure.ontoggle=()=>openCards.set(key,disclosure.open);
 const summary=el('summary');summary.dataset.focus=key;name.append(el('span',child.name));if(child.input){const file=el('small',child.input);file.className='step-file';name.append(file);}const state=badge(child.status);const info=el('div');info.className='step-progress';const value=pct(child.progress?.work_percent??child.progress?.percent??child.percent);info.append(el('strong',value==null?'—':value+'%'));const worker=el('span',workerName(child.worker,child));worker.className='worker';row.append(name,state,info,worker);summary.append(row,meter(value,child.name,readEta(child,observedAt)));disclosure.append(summary);
 const content=el('div');content.className='step-content';if(child.input){const input=el('p',child.input);input.className='file-name';content.append(input);}for(const text of [child.detail,progressText(child),child.basis,child.evidence?'Evidence: '+child.evidence:null,child.depends_on?.length?'Requires: '+child.depends_on.join(', '):null,child.retry?.next_due?'Retry after '+new Date(child.retry.next_due*1000).toLocaleTimeString():null])if(text)content.append(el('p',text));
 for(const [i,c] of (child.children||[]).entries()){const item=add(c,path+'/'+i,depth+1);if(item)content.append(item);}if(!content.children.length)content.append(el('p','No additional details recorded.'));disclosure.append(content);branch.append(disclosure);return branch;}
 for(const [i,child] of rows.entries()){const item=add(child,String(i));if(item)tree.append(item);}details.append(tree);box.append(details);}
 return box;
}
function render(data,changes){if(!data)return;const focus=document.activeElement?.dataset.focus;const all=[...data.tasks,...data.planned];const finished=t=>statusClass(t.status)==='status-completed';const rank=t=>({'status-failed':0,'status-running':1,'status-queued':2}[statusClass(t.status)]??3);const active=data.tasks.filter(t=>!finished(t)).filter(visible).sort((a,b)=>rank(a)-rank(b)||(b.priority||0)-(a.priority||0));
 $('tasks').replaceChildren(...active.map(t=>renderTask(t,changes)));if(!active.length)$('tasks').append(el('p','No current jobs match this view.'));
 $('planned').replaceChildren(...data.planned.filter(t=>!finished(t)).filter(visible).map(t=>renderTask(t,changes)));$('planned-section').hidden=!$('planned').children.length;
 const completed=all.filter(finished).filter(visible);$('completed').replaceChildren(...completed.map(t=>renderTask(t,changes)));$('completed-count').textContent=completed.length;$('completed-section').hidden=!completed.length;
 $('overview').replaceChildren(...[['running','Running'],['queued','Waiting'],['failed','Need attention'],['completed','Completed']].map(([state,label])=>{const item=el('div');item.className='overview-item status-'+state;item.append(el('strong',String(all.filter(t=>statusClass(t.status)==='status-'+state).length)),el('span',label));return item;}));
 $('updated').textContent='Updated '+new Date(data.sampled_at*1000).toLocaleTimeString();$('errors').textContent=data.errors.join(' · ');$('retry-count').textContent=(data.retry_jobs||[]).length;
 $('retry-jobs').replaceChildren(...(data.retry_jobs||[]).map(r=>{const n=el('li',r.task+' · attempt '+r.attempts+'/3 · '+r.state+(r.next_due?' · next '+new Date(r.next_due*1000).toLocaleTimeString():''));return n;}));if(!data.retry_jobs?.length)$('retry-jobs').append(el('li','No jobs waiting for retry.'));
 if(focus)[...document.querySelectorAll('[data-focus]')].find(n=>n.dataset.focus===focus)?.focus({preventScroll:true});}
function accept(data){if(paused)return;const changes=new Set();
 function visit(task,row){const key=task.id+':'+row.id;const value=row.status+'|'+(row.percent??row.progress?.percent??'');if(!first&&previousState.has(key)&&previousState.get(key)!==value){changes.add(key);const line=el('li',new Date().toLocaleTimeString()+' · '+row.name+' → '+row.status+(row.percent!=null?' · '+row.percent+'%':''));line.className='new-activity';$('live-activity').prepend(line);while($('live-activity').children.length>30)$('live-activity').lastChild.remove();}previousState.set(key,value);for(const child of row.children||[])visit(task,child);}
 for(const task of [...data.tasks,...data.planned])visit(task,task);first=false;latest=data;render(data,changes);}
async function refresh(){if(polling||paused)return;polling=true;try{const response=await fetch('/api/tasks',{cache:'no-store',signal:AbortSignal.timeout(15000)});if(!response.ok)throw Error('Task status unavailable');accept(await response.json());}catch(e){$('errors').textContent=e.message+'; displayed observations may be stale';}finally{polling=false;}}
function live(){feed?.close();if(paused)return;feed=new EventSource('/api/tasks/stream');feed.addEventListener('snapshot',event=>{accept(JSON.parse(event.data));$('live-status').className='';$('live-status').replaceChildren(el('span'));$('live-status').firstChild.className='live-dot';$('live-status').append('Live · updates every 2 seconds');});feed.onerror=()=>{$('live-status').className='live-offline';$('live-status').textContent='Reconnecting · last observations retained';};}
$('task-search').oninput=()=>{render(latest,new Set());if($('task-search').value.trim())$('completed-section').open=true;};$('task-filter').onchange=()=>{render(latest,new Set());if($('task-filter').value==='completed')$('completed-section').open=true;};$('pause-live').onclick=()=>{paused=!paused;$('pause-live').textContent=paused?'Resume live updates':'Pause live updates';if(paused){feed?.close();$('live-status').className='live-offline';$('live-status').textContent='Live updates paused';}else{void refresh();live();}};
void refresh();live();setInterval(refresh,15000);
let eventCursor=0;
async function history(reset=false){
 if(reset)eventCursor=0;
 try{
  const select=document.getElementById('event-run'),run=select.value;
  const response=await fetch('/api/tasks/events?after='+eventCursor+(run?'&run='+encodeURIComponent(run):''),{cache:'no-store'});
  if(!response.ok)throw Error('Event history unavailable');const data=await response.json();
  if(select.options.length===1)for(const item of data.runs){const option=el('option',item.name);option.value=item.id;select.append(option);}
  document.getElementById('event-summary').textContent=`${data.total_runs} recorded runs · ${data.total_events} events in this selection · cursor ${data.cursor}`;
  document.getElementById('event-list').replaceChildren(...data.events.map(event=>el('li',`#${event.id} · ${event.run_id} · ${event.source} · ${event.kind}${event.step?' · '+event.step:''}${event.status?' · '+event.status:''} · recorded ${new Date(event.recorded_at*1000).toLocaleString()}`)));
  eventCursor=data.cursor;document.getElementById('event-next').disabled=!data.events.length;
 }catch(error){document.getElementById('event-summary').textContent=error.message;}
}
document.getElementById('event-run').onchange=()=>void history(true);
document.getElementById('event-next').onclick=()=>void history();
void history();
