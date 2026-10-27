import data from './genome-data.js';
import {categoryPercent,categoryMean as meanPercent,categoryDelta,deviationScale} from './genome-comparison.js';
const $=s=>document.querySelector(s),fmt=n=>Number(n).toLocaleString('en-US');
const colors=['#a8ecc6','#bdacfa','#f5bb83','#8fcce8','#eb97b9'];
const names={SNV:'Single-letter changes',indel:'Insertions / deletions',MNV:'Multi-letter changes',mixed:'Mixed changes',symbolic:'Other variants',heterozygous:'Different alleles',hom_alternate:'Matching alternate alleles',alternate_haploid:'Single-copy alternate',hom_reference:'Matching reference alleles',missing_or_partial:'Unknown / incomplete'};
const node=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n};
const svgNode=(tag,attrs={})=>{const n=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [k,v]of Object.entries(attrs))n.setAttribute(k,v);return n};
function svg(label,w=800,h=210){const n=svgNode('svg',{viewBox:`0 0 ${w} ${h}`,role:'img','aria-label':label});const title=svgNode('title');title.textContent=label;n.append(title);return n}
function bars(target,entries,{color=colors[1],suffix='',denominator=null}={}){const parent=$(target);parent.replaceChildren();const max=denominator||Math.max(1,...entries.map(([,v])=>v));for(const [name,value]of entries){const row=node('div'),label=node('div',undefined,'bar-label');label.append(node('span',names[name]||name.replaceAll('_',' ')),node('span',fmt(value)+suffix));const track=node('div',undefined,'track'),fill=node('span');fill.style.width=`${value/max*100}%`;fill.style.background=color;track.append(fill);row.append(label,track);parent.append(row)}}
const samples=data.samples;
const sampleColor=i=>['#a8ecc6','#bdacfa','#f5bb83','#8fcce8','#eb97b9','#f6df83'][i];
function legend(){const parent=$('#dataset-legend');for(const [i,s] of samples.entries()){const row=node('div',undefined,'dataset-key'),dot=node('span',undefined,'dot');dot.style.background=sampleColor(i);row.append(dot,node('span',`${s.name} · ${s.assembly}`));parent.append(row)}}
// Every category shares a scale across datasets. Missing values are never zeroes.
function comparison(target,keys,value,{percent=false}={}){const parent=$(target);parent.replaceChildren();const values=keys.flatMap(k=>samples.map(s=>value(s,k))).filter(v=>v!==null);const max=percent?100:Math.max(1,...values);for(const k of keys){const group=node('div',undefined,'comparison-group');group.append(node('h4',names[k]||k.replaceAll('_',' ')));for(const [i,s] of samples.entries()){const v=value(s,k),row=node('div',undefined,'comparison-row'),label=node('div',undefined,'bar-label');label.append(node('span',s.name),node('span',v===null?'Unavailable':percent?`${v.toFixed(1)}%`:fmt(v)));row.append(label);if(v!==null){const track=node('div',undefined,'track'),fill=node('span');fill.style.width=`${v/max*100}%`;fill.style.background=sampleColor(i);track.append(fill);row.append(track)}group.append(row)}parent.append(group)}}
let chrom='1';
function plot(){const available=samples.filter(s=>s.bins[chrom]);const last=Math.max(0,...available.flatMap(s=>Object.keys(s.bins[chrom]).map(Number))),max=Math.max(1,...available.flatMap(s=>Object.values(s.bins[chrom]))),w=Math.max(300,$('#line').clientWidth),h=235,left=58,right=w-18,top=18,bottom=190;const chart=svg(`Chromosome ${chrom}: all datasets, variant records per 10 million bases; dashed lines use GRCh37 coordinates.`,w,h);for(let i=0;i<4;i++){const y=bottom-(bottom-top)*i/3;chart.append(svgNode('line',{x1:left,x2:right,y1:y,y2:y,stroke:'#303b49'}));const text=svgNode('text',{x:left-8,y:y+4,'text-anchor':'end',fill:'#a8b5c7','font-size':10});text.textContent=fmt(Math.round(max*i/3));chart.append(text)}for(const [i,s] of samples.entries()){const counts=s.bins[chrom];if(!counts)continue;let segment=[];const flush=()=>{if(segment.length)chart.append(svgNode('polyline',{points:segment.join(' '),fill:'none',stroke:sampleColor(i),'stroke-width':2,'stroke-dasharray':s.assembly==='GRCh37'?'6 4':'none','data-sample':s.name}));segment=[]};for(let k=0;k<=last;k++){if(counts[k]===undefined){flush();continue}const x=left+(k+.5)/(last+1)*(right-left),y=bottom-counts[k]/max*(bottom-top);segment.push(`${x},${y}`);const point=svgNode('circle',{cx:x,cy:y,r:3,fill:sampleColor(i),'data-sample':s.name});const title=svgNode('title');title.textContent=`${s.name} · ${s.assembly} · ${k*10}–${(k+1)*10} Mb: ${fmt(counts[k])}`;point.append(title);chart.append(point)}flush()}for(const fraction of [0,.25,.5,.75,1]){const text=svgNode('text',{x:left+fraction*(right-left),y:220,'text-anchor':'middle',fill:'#a8b5c7','font-size':10});text.textContent=Math.round((last+1)*10*fraction)+' Mb';chart.append(text)}$('#line').replaceChildren(chart);$('#line-title').textContent=`Chromosome ${chrom} · all available datasets`;$('#chromosome').value=chrom;$('#chromosome-counts').textContent=samples.map(s=>`${s.name}: ${s.chromosomes[chrom]===undefined?'unavailable':fmt(s.chromosomes[chrom])+' sites'}`).join(' · ')}
const genomeCategories=[['same','Confirmed same','#23c997'],['single','Single-letter variant','#b68cff'],['insertions','Insertion anchor','#27c8ed'],['deletions','Deletion start','#ff9c42'],['other','Other / mixed variant start','#f264a5'],['unknown','No recorded difference from reference','#718198']];
const alternateColors=['#009e73','#cc79a7','#56b4e9','#e69f00','#f0e442','#778592'];
let categoryPalette=false,logStrength=8;
const percentText=n=>n===0?'0%':n<0.0001?'<0.0001%':n.toFixed(4)+'%';
const compositionColor=key=>{const i=genomeCategories.findIndex(c=>c[0]===key);return categoryPalette?alternateColors[i]:genomeCategories[i][2]};
// The same rarest-first category order is used on every dataset ring.
const categoryMean=key=>meanPercent(samples,key);
const sharedCategoryOrder=genomeCategories.slice().sort((a,b)=>categoryMean(a[0])-categoryMean(b[0]));
const orderedCategories=()=>sharedCategoryOrder;
const maximumDelta=Math.max(...samples.flatMap(sample=>genomeCategories.map(([key])=>Math.abs(categoryDelta(samples,sample,key)||0))));
const signedText=(value,suffix=' pp')=>value===null?'N/A':value===0?'0'+suffix:(value>0?'+':'−')+(Math.abs(value)<.0001?'<0.0001':Math.abs(value).toFixed(4))+suffix;
function categorySectors(){return orderedCategories().map(([key,label],i)=>({key,label,mean:categoryMean(key),center:(i+.5)*100/6,half:100/6*.43}));}
function percentageGuide(chart){
 const guide=svgNode('g',{'class':'percentage-guide','aria-label':'Each category uses the same symmetric difference scale. Counterclockwise is below the mean; clockwise is above.'});
 guide.append(svgNode('circle',{cx:320,cy:320,r:312,fill:'none',stroke:'#a8b5c7','stroke-width':1}));
 for(const {center,half} of categorySectors()){
  for(const [fraction,label] of [[-1,'−'],[0,'0'],[1,'+']]){
   const angle=(center+fraction*half)/100*2*Math.PI-Math.PI/2,x=Math.cos(angle),y=Math.sin(angle);
   guide.append(svgNode('line',{x1:320+x*304,y1:320+y*304,x2:320+x*318,y2:320+y*318,stroke:'#eef3f7','stroke-width':fraction===0?2:1}));
   const text=svgNode('text',{x:320+x*332,y:320+y*332,'text-anchor':'middle','dominant-baseline':'central',fill:'#eef3f7','font-size':19,'font-weight':600});text.textContent=label;guide.append(text);
  }
 }
 chart.append(guide);
}
function differenceScale(){
 const key=node('div',undefined,'difference-scale'),title=node('p','Shared difference scale · percentage points','footnote');
 const axis=svg('Signed percentage-point scale used in every category',700,86);
 axis.append(svgNode('line',{x1:40,x2:660,y1:25,y2:25,stroke:'#a8b5c7','stroke-width':2}));
 const ticks=logStrength===0?[-maximumDelta,-maximumDelta/2,0,maximumDelta/2,maximumDelta]:[-maximumDelta,-1,-.01,-.0001,0,.0001,.01,1,maximumDelta];
 const drawn=[],minGap=window.innerWidth<480?136:62;
 for(const value of [0,...ticks.filter(value=>value!==0)]){
  const x=350+deviationScale(value,maximumDelta,logStrength)*310;
  axis.append(svgNode('line',{x1:x,x2:x,y1:17,y2:33,stroke:'#eef3f7','stroke-width':1,'data-difference-tick':value}));
  if(drawn.some(previous=>Math.abs(previous-x)<minGap))continue;drawn.push(x);
  const text=svgNode('text',{x,y:57,'text-anchor':'middle',fill:'#eef3f7','font-size':16});text.textContent=value===0?'0':(value>0?'+':'−')+(Math.abs(value)===maximumDelta?Math.abs(value).toFixed(1):Number(Math.abs(value).toPrecision(2)));axis.append(text);
 }
 key.append(title,axis);return key;
}
function renderComposition(){
 const totals=$('#metrics'),expanded=totals.querySelector('.ring-details')?.open;totals.replaceChildren();
 const chart=svg('Dataset percentages minus their category mean, in percentage points. Six concentric datasets; positive differences extend clockwise and negative differences counterclockwise from each category zero.',640,640);
 chart.classList.add('genome-rings');chart.setAttribute('viewBox','-40 -40 720 720');
 const labels=svgNode('g',{'class':'ring-labels'}),legend=node('div',undefined,'ring-legend');
 for(const [key,label] of orderedCategories()){
  const item=node('div',undefined,'category-bubble'),dot=node('span',undefined,'dot'),copy=node('span',undefined,'category-copy');
  const color=compositionColor(key);dot.style.background=color;item.style.setProperty('--category-color',color);item.dataset.category=key;
  copy.append(node('span',label),node('strong',percentText(categoryMean(key))));item.append(dot,copy);legend.append(item);
 }
 const details=node('details',undefined,'ring-details');details.open=!!expanded;details.append(node('summary','Exact percentages and differences from the mean'));
 for(const [i,sample] of samples.entries()){
  const c=sample.genome_composition,r=289-i*36,group=svgNode('g',{'data-source':sample.name}),row=node('div',undefined,'genome-row');
  row.append(node('h4',`${sample.name} · ${sample.assembly}`));const exact=node('div',undefined,'composition-values');
  for(const {key,label,mean,center,half} of categorySectors()){
   const pct=categoryPercent(sample,key),delta=categoryDelta(samples,sample,key),position=delta===null?0:deviationScale(delta,maximumDelta,logStrength)*half;
   const track=svgNode('circle',{cx:320,cy:320,r,fill:'none',stroke:'#26313e','stroke-width':27,pathLength:100,'stroke-dasharray':`${half*2} ${100-half*2}`,'stroke-dashoffset':-(center-half),transform:'rotate(-90 320 320)','data-category-track':key});group.append(track);
   if(delta!==null&&delta!==0){
    const arc=Math.abs(position),start=delta<0?center+position:center;
    const segment=svgNode('circle',{cx:320,cy:320,r,fill:'none',stroke:compositionColor(key),'stroke-width':27,pathLength:100,'stroke-dasharray':`${arc} ${100-arc}`,'stroke-dashoffset':-start,transform:'rotate(-90 320 320)','data-category':key,'data-percent':pct,'data-delta':delta,'data-arc':arc,tabindex:0});
    const title=svgNode('title');title.textContent=`${sample.name} · ${label}: ${percentText(pct)}; ${signedText(delta)} from the mean (${percentText(mean)}).`;segment.append(title);segment.setAttribute('aria-label',title.textContent);
    const show=()=>{$('#ring-readout').textContent=title.textContent};segment.addEventListener('pointerenter',show);segment.addEventListener('focus',show);group.append(segment);
   }
   const angle=center/100*360-90,rad=angle*Math.PI/180,bottom=Math.sin(rad)>0;
   const zero=svgNode('line',{x1:320+(r-14)*Math.cos(rad),y1:320+(r-14)*Math.sin(rad),x2:320+(r+14)*Math.cos(rad),y2:320+(r+14)*Math.sin(rad),stroke:'#d0dae5','stroke-opacity':.65,'stroke-width':1});group.append(zero);
   const value=svgNode('text',{transform:`translate(${320+r*Math.cos(rad)} ${320+r*Math.sin(rad)}) rotate(${angle+90+(bottom?180:0)})`,'text-anchor':'middle','dominant-baseline':'central',fill:'#fff','font-size':15,'font-weight':600,stroke:'#10151d','stroke-width':4,'paint-order':'stroke','pointer-events':'none','data-delta-label':key,'data-delta':delta??'unavailable'});value.textContent=signedText(delta,'');group.append(value);
   const cell=node('div',undefined,'composition-value'),dot=node('span',undefined,'dot');dot.style.background=compositionColor(key);cell.append(dot,node('span',label),node('strong',pct===null?'Unavailable':percentText(pct)),node('span',signedText(delta)+' from mean','reference-delta'));exact.append(cell);
  }
  const ringLabel=svgNode('g',{'data-ring-label':sample.name}),y=320-r;
  ringLabel.append(svgNode('rect',{x:269,y:y-13,width:102,height:26,rx:6,fill:'#10151d'}));
  const name=svgNode('text',{x:320,y,'text-anchor':'middle','dominant-baseline':'central',fill:'#fff','font-size':23,'font-weight':600});name.textContent=sample.name;ringLabel.append(name);labels.append(ringLabel);chart.append(group);
  row.append(node('p',`100% = ${fmt(c.reference_positions)} reference positions`,'footnote'),exact);
  if(c.reference_evidence){row.append(node('p',c.reference_evidence.summary,'footnote'));if(c.reference_evidence.source_url){const link=node('a','Official benchmark confidence regions');link.href=c.reference_evidence.source_url;row.append(link)}}details.append(row);
 }
 const center=svgNode('text',{x:320,y:305,'text-anchor':'middle',fill:'#eef3f7','font-size':22});center.textContent='± from mean';
 const unit=svgNode('text',{x:320,y:333,'text-anchor':'middle',fill:'#a8b5c7','font-size':16});unit.textContent='percentage points';chart.append(center,unit,labels);percentageGuide(chart);
 totals.append(chart,differenceScale(),node('h4','Category mean','ring-legend-title'),legend,details);
 $('#dna-comparison-title').textContent='DNA datasets · differences from the mean';
 $('#ring-reference-note').textContent='Each category has the same zero-centered scale on every ring: counterclockwise is below the mean, clockwise is above. Numbers show signed percentage-point differences. Legend percentages are unweighted means across datasets with available values; missing confirmed-match evidence is N/A. Confirmed-same means use 4 datasets; other means use 6, so legend values do not sum to 100%. Category proportions compare recorded evidence, not person-to-person sequence identity. Sources use different assemblies and methods.';
 $('#composition-palette').textContent=categoryPalette?'Palette: colorblind-friendly · switch palette':'Palette: high contrast · switch palette';
 $('#log-value').textContent=logStrength===0?'Linear differences':`Log strength ${logStrength} / 8`;
}
function render(){legend();renderComposition();$('#overview-takeaway').textContent='All six datasets appear together below. Percentages describe called variant sites within each dataset, not the whole genome. Different sources and methods limit comparisons.';comparison('#types',['SNV','indel','MNV','mixed','symbolic'],(s,k)=>(s.variant_types[k]||0)/s.records*100,{percent:true});comparison('#genotypes',[...new Set(samples.flatMap(s=>Object.keys(s.genotypes)))],(s,k)=>Object.keys(s.genotypes).every(k=>k==='missing_or_partial')?null:(s.genotypes[k]||0)/s.records*100,{percent:true});const label=node('label','Choose a chromosome'),picker=node('select');label.htmlFor='chromosome';picker.id='chromosome';const chromosomes=[...new Set(samples.flatMap(s=>Object.keys(s.chromosomes)))];for(const c of chromosomes){const option=node('option',`Chromosome ${c}`);option.value=c;picker.append(option)}picker.onchange=()=>{chrom=picker.value;plot()};$('#chromosomes').replaceChildren(label,picker);plot();comparison('#satellite-stat',['Satellite overlaps'],s=>s.satellite_overlaps?Object.values(s.satellite_overlaps).reduce((a,b)=>a+b,0):null);comparison('#depth',['0','1-9','10-19','20-29','30-59','60+'],(s,k)=>Object.keys(s.depth_bins).every(k=>k==='missing')?null:(s.depth_bins[k]||0)/s.records*100,{percent:true});$('#depth-note').textContent=samples.map(s=>`${s.name}: ${fmt(s.depth_bins.missing||0)} sites with unavailable depth`).join(' · ');const consequences=[...new Set(samples.flatMap(s=>Object.keys(s.consequences)))].sort((a,b)=>samples.reduce((n,s)=>n+(s.consequences[b]||0)-(s.consequences[a]||0),0));comparison('#consequences',consequences.slice(0,6),(s,k)=>s.consequences[k]??null);$('#annotation-note').textContent=samples.map(s=>`${s.name}: ${s.engine}; ${fmt(s.annotated)} annotated records`).join(' · ')+'. Categories overlap. VEP and bcftools csq use different annotation models; counts are not equivalent or disease predictions.'}
bars('#satellite-chart',Object.entries(data.satellites.reference_bp).sort((a,b)=>b[1]-a[1]).slice(0,8).map(([c,v])=>['Chr '+c,v]),{color:colors[0],suffix:' bp'});$('#provenance').textContent=`Snapshot generated ${new Date(data.generated_at).toLocaleDateString('en-US',{year:'numeric',month:'long',day:'numeric'})}. Charts use completed QC / annotation results and an independent chromosome count check. Repeat source: UCSC hg38 rmsk; SHA-256 ${data.satellites.sha256}.`;if(data.reference_analysis_at)$('#provenance').textContent+=` Reference-match evidence analyzed ${new Date(data.reference_analysis_at).toLocaleDateString('en-US',{year:'numeric',month:'long',day:'numeric'})}; only aggregate counts are published.`;$('#present').onclick=async()=>{document.body.classList.toggle('presentation');if(document.body.classList.contains('presentation')&&document.documentElement.requestFullscreen){try{await document.documentElement.requestFullscreen()}catch{}}else if(document.fullscreenElement)await document.exitFullscreen()};const helix=svg('Decorative DNA helix',400,300);for(let i=0;i<23;i++){const y=15+i*12,x1=200+92*Math.sin(i*.43),x2=200-92*Math.sin(i*.43);helix.append(svgNode('line',{x1,x2,y1:y,y2:y,stroke:i%2?colors[1]:colors[0],'stroke-opacity':'.4','stroke-width':3}),svgNode('circle',{cx:x1,cy:y,r:5,fill:colors[0]}),svgNode('circle',{cx:x2,cy:y,r:5,fill:colors[1]}))}$('#helix').append(helix);render();
$('#composition-palette').onclick=()=>{categoryPalette=!categoryPalette;renderComposition()};
$('#composition-scale').oninput=e=>{logStrength=Number(e.target.value);renderComposition()};

window.addEventListener('resize',()=>{plot();renderComposition()});

// Navigation follows location; it does not claim that visitors understood a step.
const journeyLinks=[...document.querySelectorAll('.journey a')];
const journeySections=journeyLinks.map(a=>document.querySelector(a.getAttribute('href')));
function updateJourney(){let active=journeySections[0];for(const section of journeySections){if(section.getBoundingClientRect().top<=180)active=section;}if(window.scrollY+window.innerHeight>=document.documentElement.scrollHeight-2)active=journeySections.at(-1);for(const a of journeyLinks){if(a.hash==='#'+active.id)a.setAttribute('aria-current','step');else a.removeAttribute('aria-current');}}
window.addEventListener('scroll',updateJourney,{passive:true});updateJourney();
document.querySelector('#compare-genomes').onclick=()=>document.querySelector('#choose-genome').scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});

// Published chart evidence stays usable when the owner-only work feed is unavailable.
function workCard(title,value,detail){const card=node('article',undefined,'work-card');card.append(node('h4',title),node('strong',value),node('p',detail));return card;}
const referenceSamples=samples.filter(s=>s.genome_composition.reference_evidence);
$('#completed-analysis').append(
 workCard('Variant summaries',`${fmt(samples.reduce((n,s)=>n+s.records,0))} records`,`${samples.length} datasets summarized by variant type, chromosome, depth and genotype where recorded.`),
 workCard('Functional annotation',`${fmt(samples.reduce((n,s)=>n+s.annotated,0))} annotated records`,'Existing VEP annotations audited for Julian; local bcftools consequences for the comparison datasets. Methods are not equivalent.'),
 workCard('Reference-match evidence',`${referenceSamples.length} datasets assessed`,'Official GIAB confidence regions and a quality-filtered reference-call audit. Positions without sufficient evidence remain unverified.')
);
$('#analysis-date').textContent='Chart snapshot: '+new Date(data.generated_at).toLocaleString()+ (data.reference_analysis_at?' · Reference evidence updated: '+new Date(data.reference_analysis_at).toLocaleString():'')+'. Source-read work below does not automatically change these charts.';
const human=value=>String(value||'not recorded').replaceAll('_',' ');
let workBusy=false,workTimer;
async function refreshWork(){
 if(document.hidden||workBusy)return;workBusy=true;
 try{
  const response=await fetch('/api/v1/swarm/wgs',{cache:'no-store',signal:AbortSignal.timeout(8000)});
  if(!response.ok)throw Error(response.status===401||response.status===403?'Sign in through Swarm Control to view the current workload record.':'Current workload record unavailable. The dated chart summaries above remain available.');
  const work=await response.json();
  if(!work.available)throw Error('No source-read work record is available. This does not affect the published chart summaries.');
  $('#work-status').textContent='Read from saved workload records · checked '+new Date(work.sampled_at*1000).toLocaleTimeString()+' · refreshes every 15 seconds';
  const stages=$('#work-stages');stages.replaceChildren();
  for(const sample of work.samples||[])stages.append(workCard(sample.sample+' source files',`${fmt(sample.files_verified)} / ${fmt(sample.files_total)} verified`,`${(sample.bytes_verified/1024**3).toFixed(1)} GiB checksum verified · ${fmt(sample.files_active)} downloading · ${fmt(sample.files_failed)} awaiting retry`));
  stages.append(workCard('Read quality control',`${fmt(work.qc_verified_files||0)} verified files`,`${fmt(work.qc.reads_processed)} reads processed · ${fmt(work.qc.bases_processed)} bases processed. In-progress totals can be provisional.`),workCard('Reference alignment',`${fmt(work.alignment.completed)} / ${fmt(work.alignment.files)} completed`,(work.alignment.status==='all_watson_runs_aligned'?'All Watson runs aligned; not yet merged, called or published':human(work.alignment.status))+'. Reference index: '+human(work.index.status)+'.'));
  const workers=$('#gpu-work');workers.replaceChildren();
  for(const worker of work.gpu_workers||[])workers.append(workCard(worker.host,`${fmt(worker.verified_files)} GPU-verified files`,`${worker.device||'Device not recorded'} · ${fmt(worker.bases)} verified bases recounted; ${fmt(worker.streaming_bases||0)} streaming GPU bases in ${fmt(worker.streaming_batches||0)} batches, with ${fmt(worker.streaming_cpu_crosschecked_batches||0)} live batches CPU crosschecked. ${fmt(worker.matches_original_cpu_qc)} files match original CPU QC; ${fmt(worker.cpu_crosschecked_batches)} batches independently CPU checked; ${fmt(worker.fallback_batches)} fallback batches. Startup checks: ${fmt(worker.startup_fixtures)} base/quality + ${fmt(worker.mask_startup_fixtures)} mask fixtures. ${human(worker.status)}${worker.updated?' · record updated '+new Date(worker.updated*1000).toLocaleString():''}.`));
  const perf=work.gpu_performance;
  $('#work-performance').textContent=perf?.available?`CompAI parallelism check: ${fmt(perf.bases)} bases in ${perf.before_seconds.toFixed(3)} → ${perf.after_seconds.toFixed(3)} seconds (${Math.round((perf.before_seconds/perf.after_seconds-1)*100)}% higher throughput). ${perf.scope}`:'';
 }catch(error){$('#work-status').textContent=error.message;$('#work-stages').replaceChildren();$('#gpu-work').replaceChildren();$('#work-performance').textContent='';}
 finally{workBusy=false;clearTimeout(workTimer);if(!document.hidden)workTimer=setTimeout(refreshWork,15000);}
}
document.addEventListener('visibilitychange',()=>{clearTimeout(workTimer);if(!document.hidden)void refreshWork();});
void refreshWork();
