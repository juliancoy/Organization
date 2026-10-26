const jobs=document.getElementById('jobs'),feedback=document.getElementById('feedback'),signIn=document.getElementById('sign-in');
const kind=document.getElementById('kind'),state=document.getElementById('state'),search=document.getElementById('search');
const active=new Set(['queued','preparing','running','stopping']);
const labels={passed:'Completed',reused:'Reused',running:'Running',queued:'Queued',preparing:'Preparing',stopping:'Stopping',stopped:'Stopped',failed:'Failed',partial:'Partial',interrupted:'Interrupted',timed_out:'Timed out',unavailable:'Unavailable'};
const el=(tag,text,className)=>{const node=document.createElement(tag);node.textContent=text;if(className)node.className=className;return node;};
let rows=[],busy=false;
function render(){
 const expanded=new Set([...jobs.querySelectorAll('details[open]')].map(node=>node.dataset.id));
 const visible=rows.filter(row=>(kind.value==='all'||row.kind===kind.value)&&(state.value==='all'||(state.value==='active')===active.has(row.status))&&`${row.title} ${row.run_id||''}`.toLowerCase().includes(search.value.trim().toLowerCase()));
 jobs.replaceChildren();
 if(!visible.length){jobs.append(el('p',rows.length?'No runs match these filters.':'No workload history is recorded yet.'));return;}
 for(const row of visible){
  const card=el('article','','history-card'),heading=el('div','','history-heading');
  heading.append(el('span',row.kind==='genome'?'Genome analysis':'Test run','history-kind'),el('span',labels[row.status]||'Unknown','history-state'));card.append(heading,el('h2',row.title));
  const date=row.started_at||row.submitted_at;
  if(date){const time=el('time',(row.started_at?'Started ':'Queued ')+new Date(date).toLocaleString());time.dateTime=date;card.append(time);}
  if(row.finished_at)card.append(el('p','Finished '+new Date(row.finished_at).toLocaleString(),'history-date'));
  const progress=row.progress||{},total=progress.total,completed=progress.completed??0,failed=progress.failed??0;
  if(Number.isFinite(total)&&total>0){
   card.append(el('p',`${completed} / ${total} ${row.kind==='genome'?'batches':'tasks'} complete · ${failed} failed`));
   const bar=document.createElement('progress');bar.max=total;bar.value=Math.min(completed+failed,total);bar.setAttribute('aria-label',`Progress for ${row.title}`);card.append(bar);
  }else if(active.has(row.status))card.append(el('p','Waiting for task counts.'));
  if(row.machines?.length){
   const details=document.createElement('details');details.dataset.id=row.id;details.open=expanded.has(row.id);details.append(el('summary',`Machines and tasks (${row.machines.length})`));
   for(const machine of row.machines){const section=el('div','','history-machine');section.append(el('h3',`${machine.name} · ${labels[machine.status]||machine.status||'Unknown'}`));const list=document.createElement('ul');for(const title of machine.tasks||[])list.append(el('li',title));section.append(list);details.append(section);}card.append(details);
  }
  if(row.run_id)card.append(el('small','Run '+row.run_id,'history-id'));
  jobs.append(card);
 }
}
async function refresh(){
 if(busy)return;busy=true;document.getElementById('refresh').disabled=true;
 try{
  const response=await fetch('/api/v1/swarm/history',{cache:'no-store',signal:AbortSignal.timeout(15000)});
  if(response.status===401){rows=[];jobs.replaceChildren();signIn.hidden=false;feedback.textContent='Sign in to view workload history.';return;}
  if(!response.ok)throw Error('History is unavailable. Showing the last loaded entries; try Refresh.');
  const data=await response.json();rows=data.jobs||[];signIn.hidden=true;render();feedback.textContent=`${rows.length} recent runs · Updated ${new Date().toLocaleTimeString()}`;
 }catch(error){feedback.textContent=error.message;}
 finally{busy=false;document.getElementById('refresh').disabled=false;}
}
for(const control of [kind,state])control.addEventListener('change',render);search.addEventListener('input',render);
document.getElementById('refresh').onclick=refresh;
document.addEventListener('visibilitychange',()=>{if(!document.hidden)void refresh();});
setInterval(()=>{if(!document.hidden)void refresh();},5000);void refresh();
