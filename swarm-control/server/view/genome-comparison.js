/** Percentages and differences share one genome denominator per dataset. */
export function categoryPercent(sample,key){
 const c=sample.genome_composition;
 if(key==='same'&&c.same===null)return null;
 return (c.counts[key]||0)/c.reference_positions*100;
}
export function categoryMean(samples,key){
 const available=samples.map(sample=>categoryPercent(sample,key)).filter(value=>value!==null);
 return available.length?available.reduce((sum,value)=>sum+value,0)/available.length:null;
}
export function categoryDelta(samples,sample,key){const value=categoryPercent(sample,key),mean=categoryMean(samples,key);return value===null||mean===null?null:value-mean;}
/** Symmetric log mapping: zero stays zero, and equal +/- differences have equal length. */
export function deviationScale(value,maximum,strength){
 if(!maximum)return 0;
 const ratio=Math.min(1,Math.abs(value)/maximum);
 return Math.sign(value)*(strength===0?ratio:Math.log1p(ratio*10**strength)/Math.log1p(10**strength));
}
