export function messageQueue(api,getThread){
 const root=document.createElement('section');root.className='codex-message-queue';root.setAttribute('aria-label','Queued messages');
 const heading=document.createElement('h3');heading.textContent='Message queue';const list=document.createElement('div'),status=document.createElement('p');status.setAttribute('role','status');root.append(heading,status,list);let epoch=0;
 async function refresh(){const thread=getThread();if(!thread){clear();return;}const current=++epoch;
  try{const data=await api('queue?thread_id='+encodeURIComponent(thread));if(current!==epoch||thread!==getThread())return;
   status.textContent='Messages send in order when this thread is idle and connected.';list.replaceChildren();
   for(const item of data.messages){const card=document.createElement('article'),title=document.createElement('strong'),text=document.createElement('p'),note=document.createElement('small');title.textContent=item.status;card.dataset.status=item.status;text.textContent=item.text||'Screenshot message';note.textContent=item.note;card.append(title,text,note);
    if(['queued','uncertain'].includes(item.status)){const cancel=document.createElement('button');cancel.type='button';cancel.textContent=item.status==='uncertain'?'I checked the conversation · mark reviewed':'Cancel queued message';cancel.onclick=async()=>{cancel.disabled=true;try{await api('queue/cancel',{id:item.id});await refresh();}catch(error){status.textContent=error.message;cancel.disabled=false;}};card.append(cancel);}list.append(card);
   }if(!data.messages.length)status.textContent='No queued messages for this thread.';
  }catch(error){if(current===epoch){status.textContent=error.message;list.replaceChildren();}}
 }
 function clear(){epoch++;list.replaceChildren();status.textContent='';}
 return {root,refresh,clear};
}
