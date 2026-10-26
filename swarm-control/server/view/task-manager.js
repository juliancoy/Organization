import './machine-connections.js';
const summary=document.getElementById('task-summary'),hosts=document.getElementById('task-hosts');
const element=(tag,text)=>{const node=document.createElement(tag);node.textContent=text;return node;};
let signature='';
document.addEventListener('test-context',event=>{
 const run=event.detail?.run,next=JSON.stringify(run);if(next===signature)return;signature=next;
 const expanded=new Set([...hosts.querySelectorAll('details[open]')].map(node=>node.dataset.host));hosts.replaceChildren();
 if(!run){summary.textContent='Task activity unavailable. Sign in or wait for the connection to recover.';return;}
 summary.textContent=run.id?`${run.finished?'Recorded run':'Current run'}: ${run.status} · ${run.id}`:'No task run recorded yet.';
 for(const host of run.hosts||[]){
  const details=document.createElement('details');details.dataset.host=host.host;details.open=expanded.has(host.host);
  details.append(element('summary',`${host.host} · ${host.status} · ${host.jobs.length} tasks`));
  for(const job of host.jobs){const row=element('p',`${job.name} · ${job.status}`);details.append(row);}
  hosts.append(details);
 }
});
