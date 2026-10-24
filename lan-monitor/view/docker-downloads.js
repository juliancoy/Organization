const el=(tag,text,cls)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node;};
let target=null,snapshot=null,metric='network',run=null,contextAt=0;
const names={pulling:'Registry pull',distributing:'LAN image transfer',cached:'Cached image',ready:'Image ready',building:'Building image'};
function draw(){
 if(!target)return;target.replaceChildren();target.hidden=metric!=='network';if(target.hidden)return;
 const now=Date.now()/1000,feed=snapshot?.docker_downloads;
 const fresh=feed?.status==='available'&&now-feed.sampled_at<15;
 const images=new Map();
 const coordinatorLive=now-contextAt<8&&!document.getElementById('history')?.value;
 const progress=coordinatorLive?run?.image_progress:null;
 if(progress&&typeof progress.image==='string'&&(run.status==='running'||now-Date.parse(run.updated)/1000<900)){
  const active=Boolean(run.status==='running'&&['pulling','distributing','building'].includes(progress.phase));
  images.set(progress.image,{image:progress.image,state:active?names[progress.phase]:'Last coordinator image · '+(names[progress.phase]||progress.phase),
   detail:[progress.source,progress.destination].filter(Boolean).join(' → '),active,evidence:'Exact image reference reported by the distributed coordinator; image-specific network rate is unknown'});
 }
 if(fresh){
  for(const pull of feed.active||[])if(!images.has(pull.image))images.set(pull.image,{...pull,state:'Pull command active',detail:'PID '+pull.pid+' · daemon context unverified',active:true});
  for(const pull of feed.recent||[])if(!images.has(pull.image))images.set(pull.image,{...pull,state:'Recent engine pull event',detail:new Date(pull.observed_at*1000).toLocaleTimeString(),active:false});
 }
 const head=el('div',undefined,'docker-pull-heading');head.append(el('strong','Docker images'),el('small',fresh?'Host observations + coordinator'+(feed.engine_status!=='available'?' · engine events unavailable':''):'Coordinator observations · host telemetry unavailable'));target.append(head);
 if(!images.size)target.append(el('p',fresh?'No observed pull commands or recent pull events. Docker API, Compose and BuildKit activity may be unobserved.':'No coordinator image activity observed. General Docker pull telemetry is unavailable.','muted'));
 for(const image of [...images.values()].slice(0,8)){
  const card=el('div',undefined,'docker-pull-item');card.dataset.active=String(image.active);card.title=image.evidence;
  const identity=el('div');identity.append(el('code',image.image),el('small',image.detail||'compai.local'));
  card.append(identity,el('span',image.state,'docker-pull-state'));target.append(card);
 }
 if(images.size>8)target.append(el('small','Showing 8 of '+images.size+' observed image references.'));
 target.append(el('small','Image labels are operation context for the chart. Device throughput includes other traffic; per-image Mb/s and layer progress are not measured.','docker-pull-scope'));
}
export function renderDockerDownloads(root,data,mode){target=root;snapshot=data;metric=mode;draw();}
document.addEventListener('test-context',event=>{run=event.detail?.run||null;contextAt=Date.now()/1000;draw();});
