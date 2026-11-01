export const numeric=value=>typeof value==='number'&&Number.isFinite(value)&&value>=0;
export function currentPerformance(host,now=Date.now()/1000){
 const p=host?.performance;
 return p&&p.schema_version===1&&!p.stale&&numeric(p.sampled_at)&&now-p.sampled_at>=-5&&now-p.sampled_at<15?p:null;
}
export function memorySeries(host,now=Date.now()/1000){
 const p=currentPerformance(host,now),m=p?.memory;
 return [
  {key:'dram_read_bytes_per_second',title:'DRAM read',unit:'B/s',value:m?.dram_read_bytes_per_second??null},
  {key:'dram_write_bytes_per_second',title:'DRAM write',unit:'B/s',value:m?.dram_write_bytes_per_second??null},
  {key:'cache references',title:'Cache references',unit:'events/s',value:m?.cache?.['cache references']??null},
  {key:'cache misses',title:'Cache misses',unit:'events/s',value:m?.cache?.['cache misses']??null},
  {key:'mbm_total_bytes',title:'Root-group MBM total',unit:'B/s · LLC ↔ memory',value:m?.mbm?.mbm_total_bytes??null},
  {key:'mbm_local_bytes',title:'Root-group MBM local',unit:'B/s · overlaps total',value:m?.mbm?.mbm_local_bytes??null},
 ].map(row=>({...row,value:numeric(row.value)?row.value:null,sampledAt:p?.sampled_at??null}));
}
export function sortedRows(rows,index,reverse){
 const known=rows.filter(row=>row[index]!==null&&row[index]!==undefined);
 known.sort((a,b)=>{const x=a[index],y=b[index];return (typeof x==='number'&&typeof y==='number'?x-y:String(x).localeCompare(String(y)))*(reverse?-1:1);});
 return [...known,...rows.filter(row=>row[index]===null||row[index]===undefined)];
}
