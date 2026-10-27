export function messageQueue(api,getThread){
 const root=document.createElement('section');root.className='codex-message-queue';root.setAttribute('aria-label','Queued messages');root.hidden=true;
 const heading=document.createElement('h3');heading.textContent='Pending messages';const list=document.createElement('div'),status=document.createElement('p');status.setAttribute('role','status');root.append(heading,status,list);let epoch=0;const cancelling=new Set();
 async function refresh(){const thread=getThread();if(!thread){clear();return;}const current=++epoch;
  try{const data=await api('queue?thread_id='+encodeURIComponent(thread));if(current!==epoch||thread!==getThread())return;
   const messages=data.messages.filter(item=>['queued','delivering','uncertain'].includes(item.status));root.hidden=!messages.length;heading.textContent='Pending messages · '+messages.length;status.textContent='Queued messages send when the conversation is idle.';list.replaceChildren();
   for(const item of messages){const card=document.createElement('article'),title=document.createElement('strong'),text=document.createElement('p'),note=document.createElement('small');title.textContent={queued:'Waiting to send',delivering:'Sending…',uncertain:'Check delivery'}[item.status];card.dataset.status=item.status;text.textContent=item.text||'Screenshot message';text.title=text.textContent;note.textContent=item.note;card.append(title,text);if(item.status==='uncertain')card.append(note);
    if(['queued','uncertain'].includes(item.status)){const cancel=document.createElement('button');cancel.type='button';cancel.textContent=item.status==='uncertain'?'I checked the conversation · mark reviewed':'×';if(item.status==='queued'){cancel.className='queue-dismiss';cancel.setAttribute('aria-label','Cancel queued message');cancel.title='Cancel queued message';}cancel.disabled=cancelling.has(item.id);cancel.onclick=async()=>{if(cancelling.has(item.id))return;cancelling.add(item.id);cancel.disabled=true;const thread=getThread();try{await api('queue/cancel',{id:item.id});await refresh();}catch(error){if(thread===getThread())status.textContent=error.message;cancel.disabled=false;}finally{cancelling.delete(item.id);}};card.append(cancel);}list.append(card);
   }if(!messages.length)status.textContent='No queued messages for this thread.';
  }catch(error){if(current===epoch){root.hidden=false;status.textContent=error.message;list.replaceChildren();}}
 }
 function clear(){epoch++;root.hidden=true;list.replaceChildren();status.textContent='';}
 return {root,refresh,clear};
}
