export const valid=v=>typeof v==='number'&&Number.isFinite(v)&&v>=0;
export function deviceLevels(data,metrics,mode,now=Date.now()) {
 const hosts=metrics.hosts||[],used=new Set(),rows=[];
 const hostValue=h=>h?.status==='live'&&now/1000-h.sampled_at<15&&valid(h[mode==='ram'?'ram_percent':mode==='disk'?'disk_busy_percent':'network_mbps'])?h[mode==='ram'?'ram_percent':mode==='disk'?'disk_busy_percent':'network_mbps']:null;
 const capture=data.link_traffic, fresh=capture&&!capture.capture_error&&now-Date.parse(capture.updated)<10000;
 for(const d of data.devices||[]){
  const h=hosts.find(h=>Boolean(d.hostname)&&(h.host===d.hostname||h.observed_hostname===d.hostname.replace(/\.local$/,''))||h.addresses?.includes(d.ip)||d.self&&h.host==='compai.local');if(h)used.add(h.host);
  let value=hostValue(h),basis='SSH host telemetry';
  if(mode==='network'&&value===null&&!d.self){const p=fresh?capture.peers?.[d.mac?.toLowerCase()]:null;value=p&&valid(p.rx_mbps)&&valid(p.tx_mbps)?p.rx_mbps+p.tx_mbps:null;basis='Gateway-observed Ethernet traffic';}
  rows.push({id:d.mac||d.ip,name:d.hostname||d.ip,value,basis});
 }
 for(const h of hosts)if(!used.has(h.host))rows.push({id:h.host,name:h.host,value:hostValue(h),basis:'SSH host telemetry'});
 return rows;
}
export function deviceColor(id){let hash=0;for(const c of id)hash=(hash*31+c.charCodeAt(0))|0;return `hsl(${(hash>>>0)%360} 78% 68%)`;}
