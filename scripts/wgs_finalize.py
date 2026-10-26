#!/usr/bin/env python3
"""Durable Watson merge and chromosome recall after every source run verifies.

Private BAMs and confidence intervals stay on enrolled SSDs. Only aggregate
summaries return to the coordinator. No chart publication occurs here.
"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shlex
import subprocess
import threading
import time
from scripts import genomics_distributed as distributed
from scripts.wgs_recall import merge_commands,pipeline
from scripts.wgs_recall_swarm import AgentClient,copy_receipt,leased_run,GROUP,distribute_reference
from scripts.wgs_swarm_download import atomic_json,stamp,file_id

PRIMARY=[str(i) for i in range(1,23)]+['X','Y','MT']


def attest(host,path):
    code="""import hashlib,json,pathlib,sys
p=pathlib.Path(sys.argv[1]);h=hashlib.sha256()
with p.open('rb') as s:
 for block in iter(lambda:s.read(8388608),b''):h.update(block)
print(json.dumps({'bytes':p.stat().st_size,'sha256':h.hexdigest(),'path':str(p)}))
"""
    result=subprocess.run(distributed.shell_command(host,['python3','-c',code,path]),capture_output=True,text=True,check=True)
    return {**json.loads(result.stdout),'host':host['host']}


def capacity(host,min_memory=1024,min_cpus=1):
    client=AgentClient(host)
    try:value=client.call('resource.capacity',{'group':GROUP})
    finally:client.close()
    if value['available_mb']<min_memory or value.get('available_cpus',0)<min_cpus:raise distributed.CapacityWait('Waiting for signed capacity')
    return value


def remote_json(host,path):
    result=subprocess.run(distributed.shell_command(host,['cat','--',str(path)]),capture_output=True,text=True,check=True)
    return json.loads(result.stdout)


def install(host,path,data):
    code="import pathlib,sys;p=pathlib.Path(sys.argv[1]);p.parent.mkdir(mode=0o700,parents=True,exist_ok=True);p.write_bytes(sys.stdin.buffer.read());p.chmod(0o600)"
    subprocess.run(distributed.shell_command(host,['python3','-c',code,str(path)]),input=data,check=True)


def validate_generation(current,aligned):
    generation=current.get('generation')
    if current.get('phase')!='verified' or not generation or not aligned or any(j.get('receipt',{}).get('generation')!=generation for j in aligned.values()):
        raise RuntimeError('Venter BAMs do not all belong to the current fully verified source generation')


def main(root,sample="Watson"):
    root=Path(root);base=root/'recall' if sample=='Watson' else root/'recall/venter';folder=base/'finalize';folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    state_path=folder/'state.json';state=json.loads(state_path.read_text()) if state_path.exists() else {'phase':'waiting_for_all_verified_alignments','collected':{},'chromosomes':{}}
    if state.get('phase')=='complete_private_research_result_pending_review':return
    atomic_json(state_path,state)
    while True:
        driver=base/'full/driver.json'
        if driver.exists() and json.loads(driver.read_text()).get('phase') in ('all_watson_runs_aligned','all_venter_shards_aligned_and_sources_verified'):break
        time.sleep(15)
    hosts=json.loads((root/'recall-hosts.json').read_text());by_name={h['host']:h for h in hosts}
    # These SSDs have the capacity for complete aligned genomes and sort scratch.
    from docker_utils.images import ImageResolver
    resolver=ImageResolver([h for h in hosts if not h['local']],lambda h:h['ssh'])
    pinned=json.loads((root/'tools.json').read_text())['image']
    if resolver.fingerprint(None,pinned)!=resolver.fingerprint(None,'lifetech/wgs-tools:bookworm-20261026'):raise RuntimeError('Bioimage tag no longer matches pinned contents')
    resolver.ensure('lifetech/wgs-tools:bookworm-20261026',[h for h in hosts if h['host'] in ('codecollective.local','julian-latitude.local','julian-inspiron-3793.local')])
    aligned=json.loads((base/'full/state.json').read_text())['jobs']
    if sample=='Venter':
        current=json.loads((base/'generation.json').read_text())
        validate_generation(current,aligned)
    catalog=json.loads((root/'catalog.json').read_text())['files'];source_runs={f['run'] for f in catalog if f['sample']==sample};expected=source_runs if sample=='Watson' else set(aligned)
    if set(aligned)!=expected or any(aligned[r]['phase']!='complete' for r in expected):raise RuntimeError('Not every manifest run has a completed alignment')
    downloads=json.loads((root/'downloads/state.json').read_text())['jobs']
    verified={j['filename'] for j in downloads.values() if j.get('sample')==sample and j.get('status')=='verified'}
    if verified!={f['filename'] for f in catalog if f['sample']==sample}:raise RuntimeError('Not every original FASTQ has passed publisher MD5')
    sizes={}
    for host in hosts:
        rows=[(run,j['receipt']['bam']) for run,j in aligned.items() if j['receipt']['host']==host['host']]
        for offset in range(0,len(rows),100):
            chunk=rows[offset:offset+100]
            query="import pathlib,json,sys;print(json.dumps({p:pathlib.Path(p).stat().st_size for p in sys.argv[1:]}))"
            observed=json.loads(subprocess.check_output(distributed.shell_command(host,['python3','-c',query,*[p for _,p in chunk]]),text=True))
            sizes.update({run:observed[path] for run,path in chunk})
    total_bam_bytes=sum(sizes.values())
    # Conservative concurrent peak: gathered input + sort scratch + merged output
    # + complete extracted chromosome collection, before derived cleanup.
    required_bytes=4*total_bam_bytes
    while True:
        choices=[]
        for name in ('julian-latitude.local','julian-inspiron-3793.local'):
            h=by_name[name];query="import json,shutil,sys;print(json.dumps({'free_bytes':shutil.disk_usage(sys.argv[1]).free}))"
            free=json.loads(subprocess.check_output(distributed.shell_command(h,['python3','-c',query,h['cache']]),text=True))['free_bytes']
            choices.append((free,h))
        if state.get('target_host'):
            target=by_name[state['target_host']];break
        eligible=[(free,h) for free,h in choices if free>=required_bytes]
        state['storage_plan']={'actual_input_bam_bytes':total_bam_bytes,'conservative_peak_additional_bytes':required_bytes,'candidates':{h['host']:free for free,h in choices},'preserve_original_fastqs':True}
        if eligible:
            _,target=max(eligible,key=lambda row:row[0]);state['target_host']=target['host'];atomic_json(state_path,state);break
        state['phase']='waiting_for_sufficient_ssd_scratch';atomic_json(state_path,state);time.sleep(30)
    work=str(Path(target['cache']).parent/'wgs-final'/sample)
    subprocess.run(distributed.shell_command(target,['mkdir','-p','--',work+'/tmp']),check=True)
    image=subprocess.check_output(distributed.shell_command(target,['docker','image','inspect','lifetech/wgs-tools:bookworm-20261026','--format','{{.Id}}']),text=True).strip()
    state['phase']='gathering_sha256_verified_bams';atomic_json(state_path,state)
    bams=[]
    for run in sorted(expected):
        if run not in state['collected']:
            receipt=aligned[run]['receipt'];source=by_name[receipt['host']]
            transfer=attest(source,receipt['bam']);transfer.update(run=run,sample=sample)
            copied=copy_receipt(source,target,transfer)
            state['collected'][run]=copied;atomic_json(state_path,state)
        bams.append(state['collected'][run]['path'])
    merged=work+'/merged.deduplicated.bam'
    if state.get('merged') is None:
        available=capacity(target,7000,2);cpus=min(target['cpus']-1,available['available_cpus']);memory=available['available_mb']//2
        state['phase']='merging_and_marking_cross_run_duplicates';atomic_json(state_path,state)
        # /inputs is peer cache, holding all SHA-verified incoming BAMs.
        linker="import os,pathlib,json,sys;p=pathlib.Path(sys.argv[1]);p.mkdir(mode=0o700,exist_ok=True);pairs=json.loads(pathlib.Path(sys.argv[2]).read_text());[(os.link(source,p/name) if not (p/name).exists() else None) for source,name in pairs]"
        links=[(path,run+'.bam') for run,path in zip(sorted(expected),bams)]
        install(target,work+'/input-links.json',json.dumps(links).encode())
        install(target,work+'/bams.list',('\n'.join('/work/inputs/'+run+'.bam' for run in sorted(expected))+'\n').encode())
        subprocess.run(distributed.shell_command(target,['python3','-c',linker,work+'/inputs',work+'/input-links.json']),check=True)
        command=merge_commands([], '/work/merged.partial.bam',cpus,memory,list_file='/work/bams.list')
        command+=' && samtools quickcheck /work/merged.partial.bam && samtools index /work/merged.partial.bam && mv /work/merged.partial.bam /work/merged.deduplicated.bam && mv /work/merged.partial.bam.bai /work/merged.deduplicated.bam.bai'
        leased_run(target,image,command,[(work,'/work',False)],work,cpus,memory,folder/'merge.json')
        state['merged']=attest(target,merged);atomic_json(state_path,state)
    lengths={f.split('\t')[0]:int(f.split('\t')[1]) for f in (root/'reference/reference.fa.fai').read_text().splitlines() if f.split('\t')[0] in PRIMARY}
    if set(lengths)!=set(PRIMARY):raise RuntimeError('Canonical GRCh38 contigs missing')
    state['phase']='distributed_chromosome_recall';atomic_json(state_path,state)
    call_hosts=hosts
    lock=threading.Lock();extraction_lock=threading.Lock();jobs=[c for c in PRIMARY if c not in state['chromosomes']]
    def lane(host,slots,reference):
        worker_image=subprocess.check_output(distributed.shell_command(host,['docker','image','inspect','lifetech/wgs-tools:bookworm-20261026','--format','{{.Id}}']),text=True).strip()
        while True:
            with lock:
                if not jobs:return
                chrom=jobs.pop(0)
            if chrom=='19' and sample=='Watson':
                result={'chrom':chrom,'length':lengths[chrom],'counts':{'confirmed_same':0,'snv':0,'insertion_anchor':0,'deletion_start':0,'other':0,'unknown':lengths[chrom]},'excluded':True,'reason':'Whole chromosome preserves intentionally withheld Watson locus'}
            else:
                part=work+'/chromosomes/'+chrom
                subprocess.run(distributed.shell_command(target,['mkdir','-p','--',part+'/tmp']),check=True)
                # Serialize extraction on merged BAM owner; calls proceed in parallel.
                with extraction_lock:
                    available=capacity(target,1024,1)
                    leased_run(target,image,'samtools view -b -o /work/input.bam /merged.bam '+chrom,[(merged,'/merged.bam',True),(merged+'.bai','/merged.bam.bai',True),(part,'/work',False)],part,1,max(1024,available['available_mb']//target['cpus']),folder/('extract-'+chrom+'.json'))
                bam_receipt=attest(target,part+'/input.bam')
                disk_query="import shutil,sys;print(shutil.disk_usage(sys.argv[1]).free)"
                with host_guards[host['host']]:
                    free=int(subprocess.check_output(distributed.shell_command(host,['python3','-c',disk_query,host['cache']]),text=True))
                    needed=2*bam_receipt['bytes']+2048*1024*1024
                    if free-reserved_disk[host['host']]<needed:
                        with lock:jobs.append(chrom)
                        time.sleep(5);continue
                    reserved_disk[host['host']]+=needed
                try:
                    copied=copy_receipt(target,host,bam_receipt)
                    call_work=str(Path(host['cache']).parent/'wgs-calls'/sample/chrom)
                    subprocess.run(distributed.shell_command(host,['mkdir','-p','--',call_work+'/tmp']),check=True)
                    # Recover a fully written summary after coordinator interruption;
                    # incomplete worker evidence remains archived for inspection.
                    recover="import json,pathlib,sys,time;p=pathlib.Path(sys.argv[1]);s=p/'evidence/summary.json';print(json.dumps(json.loads(s.read_text()) if s.exists() else None));e=p/'evidence';e.rename(p/('interrupted-evidence-'+str(time.time_ns()))) if e.exists() and not s.exists() else None"
                    recovered=json.loads(subprocess.check_output(distributed.shell_command(host,['python3','-c',recover,call_work]),text=True))
                    if recovered is not None:
                        if recovered.get('chrom')!=chrom or recovered.get('length')!=lengths[chrom]:raise RuntimeError('Recovered chromosome evidence identity mismatch')
                        recovered.update(host=host['host'],private_evidence=call_work+'/evidence')
                        with lock:state['chromosomes'][chrom]=recovered;atomic_json(state_path,state)
                        continue
                    # Flatten pure-stdlib summary dependency to avoid mounting repository.
                    recall=(Path(__file__).parent/'wgs_recall.py').read_text()
                    summary=(Path(__file__).parent/'wgs_call_summary.py').read_text().replace('from scripts.wgs_recall import classify',recall.split("def main():")[0])
                    install(host,call_work+'/summary_worker.py',summary.encode())
                    install(host,call_work+'/reference_confidence.py',(Path(__file__).parent/'wgs_reference_confidence.py').read_bytes())
                    reference=reference
                    command=pipeline([['bcftools','mpileup','-Ov','-f','/ref/reference.fa','-q','20','-Q','20','-d','10000','-a','FORMAT/DP,FORMAT/AD','/input.bam'],['python','/work/reference_confidence.py'],['bcftools','call','-m','-f','GQ','-Ov'],['python','/work/summary_worker.py','--chrom',chrom,'--length',str(lengths[chrom]),'--output','/work/evidence']])
                    while True:
                        try:
                            available=capacity(host,2048,4)
                            leased_run(host,worker_image,command,[(reference,'/ref',True),(copied['path'],'/input.bam',True),(call_work,'/work',False)],call_work,4,max(2048,available['available_mb']//slots),folder/('call-'+chrom+'.json'));break
                        except distributed.CapacityWait:time.sleep(1)
                    result=remote_json(host,call_work+'/evidence/summary.json');result.update(host=host['host'],private_evidence=call_work+'/evidence')
                finally:
                    with host_guards[host['host']]:reserved_disk[host['host']]-=needed
            with lock:state['chromosomes'][chrom]=result;atomic_json(state_path,state)
    host_guards={h['host']:threading.Lock() for h in call_hosts};reserved_disk={h['host']:0 for h in call_hosts}
    def dispatch(host):
        # Preparation is independent: slow weak-peer transfers never delay
        # already prepared callers on stronger hosts.
        from docker_utils.images import ImageResolver
        prepare=ImageResolver([h for h in hosts if not h['local']],lambda h:h['ssh'])
        prepare.ensure('lifetech/wgs-tools:bookworm-20261026',[] if host['local'] else [host])
        reference=distribute_reference(host,root/'reference',call_only=True)
        while True:
            try:
                available=capacity(host,2048,4);break
            except distributed.CapacityWait:
                with lock:
                    if not jobs:return
                time.sleep(5)
        slots=max(1,min(available['available_cpus']//4,available['available_mb']//2048))
        with lock:state.setdefault('call_slots',{})[host['host']]=slots;atomic_json(state_path,state)
        with ThreadPoolExecutor(max_workers=slots) as lanes:list(lanes.map(lambda _:lane(host,slots,reference),range(slots)))
    with ThreadPoolExecutor(max_workers=len(call_hosts)) as pool:list(pool.map(dispatch,call_hosts))
    state['phase']='awaiting_full_source_QC';atomic_json(state_path,state)
    while True:
        passed=True
        for item in catalog:
            if item['sample']!=sample:continue
            qc=root/'qc'/(file_id(item)+'.json')
            try:value=json.loads(qc.read_text())
            except (FileNotFoundError,json.JSONDecodeError):passed=False;break
            if value.get('phase')!='verified' or value.get('provisional') is not False:passed=False;break
        if passed:break
        time.sleep(10)
    counts={key:sum(c['counts'][key] for c in state['chromosomes'].values()) for key in ('confirmed_same','snv','insertion_anchor','deletion_start','other','unknown')}
    total=sum(lengths.values())
    if sum(counts.values())!=total:raise RuntimeError('Categories do not partition canonical genome')
    result={'sample':sample,'reference_sha256':json.loads((root/'reference/source.json').read_text())['sha256'],'assembly':'GRCh38','denominator':total,'counts':counts,'excluded_chromosomes':['19'] if sample=='Watson' else [],'source_runs':len(source_runs),'alignment_shards':len(expected),'minimum_DP':20,'minimum_variant_GQ':20,'minimum_reference_RCQ':20,'reference_quality_model':'RCQ from raw PL with equal genotype priors; not caller GQ','status':'complete_private_research_result_pending_review','publication':False}
    atomic_json(folder/'aggregate.json',result);state['phase']='complete_private_research_result_pending_review';atomic_json(state_path,state)

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',required=True);p.add_argument('--sample',choices=['Watson','Venter'],default='Watson');args=p.parse_args();main(args.root,args.sample)
