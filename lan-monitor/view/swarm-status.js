const $=id=>document.getElementById(id),feedback=$('feedback');let loading=false;
function element(tag,text,cls){const node=document.createElement(tag);node.textContent=text;if(cls)node.className=cls;return node}
function date(value){if(!value)return 'Not recorded';const parsed=new Date(value);return Number.isNaN(parsed.valueOf())?'Not recorded':parsed.toLocaleString()}
async function refresh(){
 if(loading)return;loading=true;$('refresh').disabled=true;
 try{
  const response=await fetch('/api/v1/swarm/status',{cache:'no-store',signal:AbortSignal.timeout(10000)});
  if(response.status===401){for(const id of ['devices','workers','interfaces','network','observed','run'])$(id).replaceChildren();throw Error('Pair this browser in Swarm Control to view your swarm.');}
  if(!response.ok)throw Error('Swarm status is unavailable. Try Refresh.');
  const data=await response.json();
  $('network').textContent=`${data.network.interface||'Interface not recorded'} · ${data.network.subnet||'Subnet not recorded'} · Gateway ${data.network.gateway||'not recorded'}`;
  const rate=value=>Number.isFinite(value)?value.toFixed(2)+' Mb/s':'Not measured';
  $('interfaces').replaceChildren(...data.network.interfaces.map(link=>{const card=element('article','','status-card');card.append(element('h3',link.name),element('p',`Receive ${rate(link.rx_mbps)} · Send ${rate(link.tx_mbps)}`),element('small',`Reported capacity ${rate(link.capacity_mbps)} · Measured ${date(link.updated)}`));return card}));
  $('observed').textContent=`${data.devices.length} devices · Network observed ${date(data.updated)}`;
  $('devices').replaceChildren(...data.devices.map(device=>{
   const card=element('article','','status-card');card.append(element('h3',device.hostname),element('p',device.address),element('p',device.reachable?'Ping responded':'No ping response',device.reachable?'reachable':''));if(device.coordinator)card.append(element('small','Coordinator'));return card
  }));
  if(!data.devices.length)$('devices').append(element('p','No devices have been observed yet.'));
  $('run').textContent=`${data.run.status}${data.run.id?' · '+data.run.id:''}`;
  $('workers').replaceChildren(...data.run.hosts.map(host=>{
   const card=element('article','','status-card'),jobs=element('ul','');card.append(element('h3',host.host),element('p',host.status));for(const job of host.jobs)jobs.append(element('li',job.name+' · '+job.status));card.append(jobs);return card
  }));
  feedback.dataset.error='false';feedback.textContent=`Status refreshed ${new Date().toLocaleTimeString()}${data.scanning?' · Network scan in progress':''}`;
 }catch(error){feedback.dataset.error='true';feedback.textContent=error.message+' Previously displayed observations may be out of date.'}
 finally{loading=false;$('refresh').disabled=false}
}
$('refresh').onclick=refresh;void refresh();setInterval(()=>{if(!document.hidden)void refresh()},10000);
