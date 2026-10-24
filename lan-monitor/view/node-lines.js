const valid=v=>Number.isFinite(v)&&v>=0;
const palettes={send:['#08080c','#780800','#e52b05','#ff9a14','#ffe37a','#fffbea'],receive:['#f04438','#ffe347','#22c96c']};
function heat(value,colors){const t=Math.max(0,Math.min(1,value))*(colors.length-1),i=Math.min(colors.length-2,Math.floor(t)),f=t-i;const rgb=s=>s.slice(1).match(/../g).map(v=>parseInt(v,16));const a=rgb(colors[i]),b=rgb(colors[i+1]);return `rgb(${a.map((v,j)=>Math.round(v+(b[j]-v)*f)).join(',')})`;}
export class NodeLines{
 constructor(){this.history=new Map();this.scale=1;}
 update(nodes,data,now=Date.now()){
  const capture=data.link_traffic,stamp=Date.parse(capture?.updated),fresh=Number.isFinite(stamp)&&now-stamp<10000&&!capture.capture_error;
  for(const node of nodes){
   let send=null,receive=null;
   if(fresh){
    const peer=capture.peers?.[node.mac];
    if(peer){send=peer.rx_mbps;receive=peer.tx_mbps;}
    if(node.self){const t=data.interface_traffic?.[data.interface];if(t&&now-Date.parse(t.updated)<10000){send=t.tx_mbps;receive=t.rx_mbps;}}
    if(node.external){const rows=(capture.flows?.records||[]).filter(r=>r.remote_ip===node.ip);if(rows.length){send=rows.reduce((n,r)=>n+(r.download_mbps||0),0);receive=rows.reduce((n,r)=>n+(r.upload_mbps||0),0);}}
   }
   const record=this.history.get(node.id)||{points:[]};
   if(record.stamp!==stamp){record.points.push({time:now,send:valid(send)?send:null,receive:valid(receive)?receive:null});record.stamp=stamp;}
   record.points=record.points.filter(p=>now-p.time<=120000);record.seen=now;
   record.send=valid(send)?send:null;record.receive=valid(receive)?receive:null;
   for(const side of ['send','receive']){const values=record.points.map(p=>p[side]).filter(valid);record[side+'Mean']=values.length?values.reduce((a,b)=>a+b,0)/values.length:0;record[side+'Peak']=Math.max(0,...values);}
   this.history.set(node.id,record);
  }
  for(const [id,r] of this.history)if(now-r.seen>120000)this.history.delete(id);
  this.scale=Math.max(.001,...[...this.history.values()].flatMap(r=>[r.sendMean,r.receiveMean]));
 }
 draw(ctx,node,p,radius){
  const r=this.history.get(node.id)||{},length=Math.max(16,radius*2),offset=6;
  for(const [side,y,shift] of [['send',-offset,-3],['receive',offset,3]]){
   const value=r[side],known=valid(value),ratio=known?Math.min(1,value/Math.max(.001,r[side+'Peak'])):0;
   const x=p.x-length/2+shift;
   ctx.lineWidth=2+6*(r[side+'Mean']||0)/this.scale;ctx.lineCap='round';
   ctx.strokeStyle='#657582';ctx.setLineDash(known?[]:[3,3]);ctx.beginPath();ctx.moveTo(x,p.y+y);ctx.lineTo(x+length,p.y+y);ctx.stroke();ctx.setLineDash([]);
   if(known){ctx.strokeStyle=heat(ratio,palettes[side]);ctx.beginPath();ctx.moveTo(x,p.y+y);ctx.lineTo(x+Math.max(1,length*ratio),p.y+y);ctx.stroke();}
  }
  if(node.perspective){ctx.strokeStyle='#f3f6f9';ctx.lineWidth=1;ctx.strokeRect(p.x-length/2-7,p.y-13,length+14,26);}
  ctx.lineWidth=1;
 }
}
