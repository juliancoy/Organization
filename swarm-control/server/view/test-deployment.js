const root=document.querySelector('#test-deployment');
if(root){
const el=(tag,text)=>{const e=document.createElement(tag);if(text)e.textContent=text;return e};
const heading=el('h2','Swarm Control · Test deployment');
const description=el('p','Connect this device directly to CompAI, verify the test APK, then install it on your phone.');
const status=el('p','Not connected. Your existing console pairing authorizes this connection.');status.setAttribute('role','status');
const connect=el('button','Connect P2P'),download=el('button','Receive test APK'),disconnect=el('button','Disconnect');
download.disabled=true;disconnect.disabled=true;
const actions=el('div');actions.className='test-deployment-actions';actions.append(connect,download,disconnect);
const fallback=el('a','Download test APK over HTTPS');fallback.href='/api/v1/test-app/download';fallback.download='swarm-control-test.apk';
const life=el('a','Open Swarm Control');life.href='/';life.target='_blank';life.rel='noopener';
const links=el('p');links.append(fallback,document.createTextNode(' · '),life);
const progress=el('progress');progress.hidden=true;progress.max=1;
const note=el('small','The APK is a separate Swarm Control launcher using your normal browser session. Android asks you to approve installation. P2P uses direct WebRTC; HTTPS is an explicitly separate fallback.');
root.append(heading,description,actions,status,progress,links,note);
let pc,channel,sequence=0,pending=new Map(),blobURL;
const api=async(path,body)=>{const response=await fetch('/api/v1/test-app/'+path,{method:body?'POST':'GET',credentials:'same-origin',cache:'no-store',headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined,signal:AbortSignal.timeout(40000)});const value=await response.json();if(!response.ok)throw Error(value.error?.message||'Pair this browser with CompAI first.');return value};
const reset=()=>{for(const entry of pending.values()){clearTimeout(entry.timer);entry.reject(Error('Connection closed'))}pending.clear();if(channel){channel.onclose=null;channel.close()}pc?.close();pc=null;channel=null;download.disabled=true;disconnect.disabled=true;connect.disabled=false;progress.hidden=true};
const request=(op,args={})=>new Promise((resolve,reject)=>{if(channel?.readyState!=='open'){reject(Error('P2P is not connected'));return}const id=sequence++;const timer=setTimeout(()=>{pending.delete(id);reject(Error('P2P request timed out'))},15000);pending.set(id,{resolve,reject,timer});channel.send(JSON.stringify({id,op,...args}))});
const digest=async bytes=>[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(v=>v.toString(16).padStart(2,'0')).join('');
connect.onclick=async()=>{
 reset();connect.disabled=true;status.textContent='Checking pairing and gathering connection candidates…';
 try{
  const info=await api('status');if(!info.app)throw Error('No Swarm Control test APK is published yet.');
  const connection=new RTCPeerConnection({iceServers:[{urls:'stun:stun.l.google.com:19302'}]});pc=connection;
  channel=pc.createDataChannel('swarm-control-test.v1');
  channel.onmessage=event=>{try{const value=JSON.parse(event.data),entry=pending.get(value.id);if(!entry)return;pending.delete(value.id);clearTimeout(entry.timer);value.ok?entry.resolve(value):entry.reject(Error(value.error||'P2P request rejected'))}catch{reset();status.textContent='Invalid P2P response.'}};
  const opened=new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('Direct P2P connection failed. HTTPS download remains available; some networks require a TURN relay.')),30000);channel.onopen=()=>{clearTimeout(timer);resolve()};channel.onerror=()=>{clearTimeout(timer);reject(Error('P2P channel failed'))}});
  // Keep a rejection handler attached while gathering/signaling is in progress.
  opened.catch(()=>{});
  await pc.setLocalDescription(await pc.createOffer());
  await new Promise((resolve,reject)=>{if(connection.iceGatheringState==='complete'){resolve();return}const timer=setTimeout(()=>reject(Error('ICE gathering timed out')),12000);connection.addEventListener('icegatheringstatechange',()=>{if(connection.iceGatheringState==='complete'){clearTimeout(timer);resolve()}})});
  const answer=await api('offer',{type:pc.localDescription.type,sdp:pc.localDescription.sdp});
  await pc.setRemoteDescription({type:answer.type,sdp:answer.sdp});await opened;
  await request('ping');status.textContent='Direct WebRTC connected to CompAI. Ready to receive Swarm Control.';download.disabled=false;disconnect.disabled=false;
  channel.onclose=()=>{reset();status.textContent='P2P disconnected. Reconnect to continue.'};
 }catch(error){reset();status.textContent=error.message}
};
download.onclick=async()=>{
 download.disabled=true;progress.hidden=false;progress.value=0;
 try{
  const manifest=await request('manifest');if(manifest.package!=='app.swarmcontrol.test'||!Number.isSafeInteger(manifest.size)||manifest.size<1||manifest.size>16*1024*1024||manifest.chunkSize!==16384||!/^[a-f0-9]{64}$/.test(manifest.sha256))throw Error('Invalid test APK manifest');
  const bytes=new Uint8Array(manifest.size);
  for(let offset=0;offset<bytes.length;offset+=manifest.chunkSize){const part=await request('chunk',{offset});const chunk=Uint8Array.from(atob(part.data),v=>v.charCodeAt(0));if(part.offset!==offset||chunk.length!==Math.min(manifest.chunkSize,bytes.length-offset))throw Error('Invalid P2P chunk');bytes.set(chunk,offset);progress.value=(offset+chunk.length)/bytes.length;await new Promise(resolve=>setTimeout(resolve,15))}
  const hash=await digest(bytes);if(hash!==manifest.sha256)throw Error('APK checksum mismatch; download discarded');
  await request('received',{sha256:hash});
  if(blobURL)URL.revokeObjectURL(blobURL);blobURL=URL.createObjectURL(new Blob([bytes],{type:'application/vnd.android.package-archive'}));
  const save=el('a');save.href=blobURL;save.download='swarm-control-test.apk';save.click();status.textContent='APK received and SHA-256 verified over WebRTC. Open the download on your phone and approve installation.';
 }catch(error){status.textContent=error.message}finally{download.disabled=channel?.readyState!=='open';progress.hidden=true}
};
disconnect.onclick=()=>{reset();status.textContent='Disconnected from CompAI.'};
window.addEventListener('pagehide',()=>{reset();if(blobURL)URL.revokeObjectURL(blobURL)});
}
