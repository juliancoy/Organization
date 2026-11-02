"""Checkpointed RAM-scratch mate preparation and streaming genome finalization.

Mates are guaranteed to belong to the same source batch. Preparing each batch
independently therefore permits a sorted merge followed by GLOBAL duplicate
marking, without a genome-sized name collation or coordinate re-sort.
"""
import json
import hashlib
import math
import re
from pathlib import Path
import shlex
import shutil

from scripts.wgs_swarm_download import atomic_json, stamp
from scripts.genome_runtime import leased_run
from scripts.wgs_paired_shards import sha
from scripts.wgs_finalize import attest

SCHEMA='julian-mate-prepared.v1'


def plan(memory_mb,input_bytes,largest_batch_bytes,free_bytes):
    if memory_mb<8192:raise RuntimeError('RAM finalization awaits at least 8 GiB of available worker memory')
    budget=min(24576,int(memory_mb)-2048)
    ram=min(4096,budget//4)
    # sort -@1 uses two sorting threads; account for per-thread -m, collate,
    # fixmate/HTS buffers and the entire bounded tmpfs in the same reservation.
    sort_mb=max(512,(budget-ram-3072)//2)
    required=math.ceil(input_bytes*1.25)+2*largest_batch_bytes+10*1024**3
    if free_bytes<required:raise RuntimeError('Streaming finalization awaits final-output space plus one batch and reserve; all verified batches retained')
    return {'schema':'julian-stream-finalization.v1','memory_mb':budget,'ram_scratch_mb':ram,
            'sort_memory_per_thread_mb':sort_mb,'sort_threads':2,'cpus':5,
            'input_bytes':input_bytes,'required_free_bytes':required,'observed_free_bytes':free_bytes,
            'scratch_tier':'container_tmpfs','swap_allowed':False,
            'scope':'RAM-bounded per-batch mate preparation, sorted stream merge, global duplicate marking; final output and checkpoints use disk'}


def prepare_command(ident,config):
    source='/run/bams/'+ident+'.bam';out='/work/'+ident+'.prepared.partial.bam'
    return ('export TMPDIR=/ram; samtools collate -u -O '+shlex.quote(source)+' /ram/collate'
        +' | samtools fixmate -m -u - -'
        +' | samtools sort -@ 1 -m '+str(config['sort_memory_per_thread_mb'])+'M -T /ram/sort -o '+shlex.quote(out)+' -'
        +' && samtools quickcheck '+shlex.quote(out)
        +' && samtools flagstat -O json '+shlex.quote(out)+' > /work/'+ident+'.flagstat.json')


def final_command():
    return ('export TMPDIR=/ram; samtools merge -u -b /work/bams.list -'
        +' | samtools markdup -@ 2 -s -T /ram/markdup - /work/Julian-HuRef2.partial.bam'
        +' && samtools quickcheck /work/Julian-HuRef2.partial.bam'
        +' && samtools index -@ 2 /work/Julian-HuRef2.partial.bam'
        +' && samtools flagstat -O json /work/Julian-HuRef2.partial.bam > /work/flagstat.json'
        +' && samtools stats -r /ref/reference.fa /work/Julian-HuRef2.partial.bam > /work/stats.txt')


def check_counts(flag,pairs,original=None):
    primary=sum(flag.get(k,{}).get('primary',0) for k in ('QC-passed reads','QC-failed reads'))
    if primary!=2*pairs:raise ValueError('Mate preparation lost or duplicated primary reads')
    if original:
        for field in ('total','secondary','supplementary'):
            before=sum(original.get(k,{}).get(field,0) for k in ('QC-passed reads','QC-failed reads'))
            after=sum(flag.get(k,{}).get(field,0) for k in ('QC-passed reads','QC-failed reads'))
            if before!=after:raise ValueError('Mate preparation changed record accounting')


def source_aliases(root,job):
    """Only known generated files for this run; never traverse or remove reads."""
    root=Path(root).resolve();ident=job['id'];digest=job['receipt']['sha256']
    return [root/'alignments'/ident/'alignment.bam',root/'collected/incoming'/digest/'alignment.bam']


def adopt_prepared(root,job,checkpoint):
    """Idempotent recovery if interrupted between rename, cleanup, and save."""
    root=Path(root).resolve();source=root/'bams'/(job['id']+'.bam')
    if checkpoint.get('schema')!=SCHEMA or checkpoint.get('job')!=job['id'] or checkpoint.get('pairs')!=job['pairs']:
        raise ValueError('Prepared batch checkpoint identity mismatch')
    new=checkpoint['prepared'];old=checkpoint['original']
    if source.stat().st_size!=new['bytes'] or sha(source)!=new['sha256']:
        raise ValueError('Prepared batch checksum mismatch')
    job['receipt']={**job['receipt'],**new,'path':str(source)}
    job['mate_preparation']={'schema':SCHEMA,'original':old,'prepared':new,'verified':True}
    for path in source_aliases(root,{**job,'receipt':old}):
        if path.is_symlink() or not path.exists():continue
        st=path.stat()
        # Unlink only aliases to the old verified inode. A changed path is kept.
        if (st.st_dev,st.st_ino)==(old['device'],old['inode']):path.unlink()


def prepare_batch(root,job,local,image,config,save,run=leased_run):
    if not re.fullmatch(r'[0-9]{6}',job['id']):raise ValueError('Invalid source batch ID')
    root=Path(root).resolve();final=root/'final';folder=final/'mate-prepared';folder.mkdir(parents=True,mode=0o700,exist_ok=True)
    source=root/'bams'/(job['id']+'.bam');checkpoint=folder/(job['id']+'.json')
    if checkpoint.exists():
        record=json.loads(checkpoint.read_text())
        # A checkpoint is written before replacement so either outcome can resume.
        if sha(source)==record['prepared']['sha256']:
            adopt_prepared(root,job,record);save();return
    else:record=None
    expected=job['receipt']
    if source.stat().st_size!=expected['bytes'] or sha(source)!=expected['sha256']:
        raise ValueError('Source batch identity changed before mate preparation')
    st=source.stat();original={**expected,'device':st.st_dev,'inode':st.st_ino}
    out=final/(job['id']+'.prepared.partial.bam')
    run(local,image,prepare_command(job['id'],config),[(root,'/run',True),(final,'/work',False)],str(final),
        config['cpus'],config['memory_mb'],folder/(job['id']+'.stage.json'),wait_for_capacity=True,tmpfs={'/ram':config['ram_scratch_mb']})
    flag=json.loads((final/(job['id']+'.flagstat.json')).read_text());check_counts(flag,job['pairs'],job.get('flagstat'))
    prepared={'bytes':out.stat().st_size,'sha256':sha(out),'host':local['host']}
    record={'schema':SCHEMA,'job':job['id'],'pairs':job['pairs'],'original':original,'prepared':prepared,'verified':stamp(),'image':image,'worker':local['host'],'recipe_sha256':hashlib.sha256(prepare_command(job['id'],config).encode()).hexdigest()}
    atomic_json(checkpoint,record)
    out.replace(source)
    adopt_prepared(root,job,record);save()
    (final/(job['id']+'.flagstat.json')).unlink(missing_ok=True)


def finalize(root,jobs,local,manifest,reference,save,available_memory,stop=None):
    root=Path(root).resolve();final=root/'final';final.mkdir(mode=0o700,exist_ok=True)
    pending=final/'completion-pending.json'
    if pending.exists():
        record=json.loads(pending.read_text());bam=final/'Julian-HuRef2.bam';partial=final/'Julian-HuRef2.partial.bam'
        candidate=next((p for p in (partial,bam) if p.is_file() and p.stat().st_size==record['bam']['bytes'] and sha(p)==record['bam']['sha256']),None)
        if candidate is None:raise ValueError('Interrupted final output checksum mismatch')
        if partial.exists() and candidate==partial:partial.replace(bam)
        index=final/'Julian-HuRef2.partial.bam.bai'
        if index.exists():index.replace(final/'Julian-HuRef2.bam.bai')
        index=final/'Julian-HuRef2.bam.bai'
        if not index.is_file():raise ValueError('Verified final BAM index missing')
        if record.get('index') and (index.stat().st_size!=record['index']['bytes'] or sha(index)!=record['index']['sha256']):raise ValueError('Interrupted final index checksum mismatch')
        record['bam']['path']=str(bam);atomic_json(final/'complete.json',record);return
    sizes=[Path(j['receipt']['path']).stat().st_size for j in jobs]
    config=plan(available_memory,sum(sizes),max(sizes,default=0),shutil.disk_usage(root).free)
    atomic_json(final/'stream-plan.json',config)
    for index,job in enumerate(jobs):
        if stop is not None and stop.is_set():raise InterruptedError('Finalization stopped at a verified batch checkpoint')
        atomic_json(root/'finalization.json',{'phase':'running','stage':'RAM mate preparation','completed_batches':index,
            'total_batches':len(jobs),'memory_mb':config['memory_mb'],'ram_scratch_mb':config['ram_scratch_mb'],'updated':stamp()})
        prepare_batch(root,job,local,manifest['image'],config,save)
    # Prepared BAMs stay coordinate sorted. Mark duplicates only after combining
    # ALL batches so PCR duplicates across source shards remain detectable.
    sizes=[Path(j['receipt']['path']).stat().st_size for j in jobs]
    plan(available_memory,sum(sizes),max(sizes,default=0),shutil.disk_usage(root).free)
    (final/'bams.list').write_text(''.join('/run/bams/'+j['id']+'.bam\n' for j in jobs))
    if stop is not None and stop.is_set():raise InterruptedError('Finalization stopped before global merge; prepared batches retained')
    run_args=[local,manifest['image'],final_command(),[(root,'/run',True),(final,'/work',False),(reference,'/ref',True)],str(final),config['cpus'],config['memory_mb'],root/'finalization.json']
    leased_run(*run_args,wait_for_capacity=True,tmpfs={'/ram':config['ram_scratch_mb']})
    check_counts(json.loads((final/'flagstat.json').read_text()),sum(j['pairs'] for j in jobs))
    record={'bam':attest(local,str(final/'Julian-HuRef2.partial.bam')),'primary_pair_accounting_verified':True,
        'duplicate_marking':'across_all_batches','finalization_plan':config,'finished':stamp(),
        'warning':'Reference mismatches include biological divergence; not a pure sequencing-error estimate'}
    index=final/'Julian-HuRef2.partial.bam.bai'
    record['index']={'bytes':index.stat().st_size,'sha256':sha(index)}
    atomic_json(pending,record)
    (final/'Julian-HuRef2.partial.bam').replace(final/'Julian-HuRef2.bam')
    (final/'Julian-HuRef2.partial.bam.bai').replace(final/'Julian-HuRef2.bam.bai')
    record['bam']['path']=str(final/'Julian-HuRef2.bam');atomic_json(final/'complete.json',record)
