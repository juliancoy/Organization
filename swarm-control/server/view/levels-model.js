import {hostCpuDetail,hostCpuContext,cpuStatusLabel} from './cpu-report.js';
export const valid=v=>typeof v==='number'&&Number.isFinite(v)&&v>=0;
export function deviceLevels(data,metrics,mode,now=Date.now()) {
 const hosts=metrics.hosts||[],used=new Set(),rows=[];
 if(['gpu','vram'].includes(mode))return hosts.flatMap(h=>(h.hardware?.gpus||[]).map((gpu,index)=>{
  const fresh=h.status==='live'&&now/1000-h.sampled_at<15&&!h.hardware.stale;
  let value=mode==='gpu'?gpu.utilization_percent:valid(gpu.memory_total_mib)&&gpu.memory_total_mib>0&&valid(gpu.memory_free_mib)?100*(1-gpu.memory_free_mib/gpu.memory_total_mib):null;
  if(!fresh||!valid(value))value=null;
  return {id:h.host+':gpu:'+(gpu.pci_id||index),name:h.host+' · '+gpu.name,value,basis:mode==='gpu'?'Live GPU utilization; unavailable backends remain unmeasured':'Dedicated GPU memory used; unavailable memory counters remain unmeasured'};
 }));

 if(['network','send','receive'].includes(mode))for(const host of hosts){
  if(!host.network_interfaces?.length)continue;
  used.add(host.host);
  for(const iface of host.network_interfaces){
   const field=mode==='send'?'tx_mbps':mode==='receive'?'rx_mbps':'network_mbps';
   const fresh=iface.status==='live'&&now/1000-iface.sampled_at<15;
   rows.push({id:`${host.host}:interface:${iface.ifindex}:${iface.name}`,name:`${host.host} · ${iface.name}`,machine:host.host,interface:iface.name,
    value:fresh&&valid(iface[field])?iface[field]:null,basis:`Physical interface ${iface.name} · ${iface.state||'unknown state'} · ${iface.window_seconds??'unknown'}s counter interval`});
  }
 }
 const field={network:'network_mbps',disk:'disk_busy_percent',ram:'ram_percent',cpu:'percent',send:'network_tx_mbps',receive:'network_rx_mbps'}[mode];
 const hostValue=h=>h?.status==='live'&&now/1000-h.sampled_at<15&&valid(h[field])?h[field]:null;
 const cpuInfo=h=>mode==='cpu'?{basis:h?hostCpuDetail(h)+' · '+hostCpuContext(h):'No host telemetry',unavailableLabel:h?cpuStatusLabel(now/1000-h.sampled_at>=15?'stale':h.status):'No telemetry'}:{};
 const capture=data.link_traffic, fresh=capture&&!capture.capture_error&&now-Date.parse(capture.updated)<10000;
 for(const d of data.devices||[]){
  const h=hosts.find(h=>Boolean(d.hostname)&&(h.host===d.hostname||h.observed_hostname===d.hostname.replace(/\.local$/,''))||h.addresses?.includes(d.ip)||d.self&&h.host==='compai.local');if(h&&used.has(h.host))continue;if(h)used.add(h.host);
  let value=hostValue(h),basis='Host telemetry';
  if(mode==='network'&&value===null&&!d.self){const p=fresh?capture.peers?.[d.mac?.toLowerCase()]:null;value=p&&valid(p.rx_mbps)&&valid(p.tx_mbps)?p.rx_mbps+p.tx_mbps:null;basis='Gateway-observed Ethernet traffic';}
  if(['send','receive'].includes(mode)&&value===null){
   const p=d.self?data.interface_traffic?.[data.interface]:fresh?capture.peers?.[d.mac?.toLowerCase()]:null;
   const available=d.self?p&&now-Date.parse(p.updated)<10000:!!p;
   const field=mode==='send'?(d.self?'tx_mbps':'rx_mbps'):(d.self?'rx_mbps':'tx_mbps');
   value=available&&valid(p[field])?p[field]:null;
   basis=d.self?'Coordinator LAN interface only':'Gateway-observed device traffic; switched traffic may be missing';
  }
  rows.push({id:d.mac||d.ip,name:(d.hostname||d.ip)+(['network','send','receive'].includes(mode)?' · interface unknown':''),value,basis,...cpuInfo(h)});
 }
 for(const h of hosts)if(!used.has(h.host))rows.push({id:h.host,name:h.host+(['network','send','receive'].includes(mode)?' · interface unknown':''),value:hostValue(h),basis:'Host telemetry',...cpuInfo(h)});
 return rows;
}
export function deviceColor(id){let hash=0;for(const c of id)hash=(hash*31+c.charCodeAt(0))|0;return `hsl(${(hash>>>0)%360} 78% 68%)`;}
