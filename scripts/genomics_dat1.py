"""Private two-worker DAT1 callability deployment; never infer absence from no call."""
import argparse
import collections
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import signal
import time
import uuid
from scripts import genomics_distributed as distributed
from scripts.genomics import private_run,write_json

LOCUS={'gene':'SLC6A3','alias':'DAT1','variant':'3-prime UTR 40-bp VNTR / rs28363170','assembly':'GRCh38','chrom':'5','start':1393581,'end':1393985,'coordinate_system':'0-based half-open','target_repeat_count':10,
       'source':'https://doi.org/10.26508/lsa.202201677'}
WORKER=Path(__file__).with_name('genomics_dat1_worker.py').read_text()


def package(sam):
    value={'locus':LOCUS,'sam':sam};payload=gzip.compress(json.dumps(value).encode(),mtime=0)
    if len(payload)>8*1024*1024:raise ValueError('Target input exceeds private RAM payload bound')
    return {'kind':'dat1-callability','records':sum(bool(l) and not l.startswith('@') for l in sam.splitlines()),'sha256':hashlib.sha256(payload).hexdigest(),'payload':payload}


def controls():
    start=LOCUS['start']-100
    # These test evidence gating, not an unvalidated VNTR genotype model.
    def row(length,flag=0):return f'fixture\t{flag}\t5\t{start+1}\t60\t{length}M\t*\t0\t0\t'+('A'*length)+'\t'+('I'*length)+'\n'
    return [('short_read_cannot_resolve_repeat',row(150),0),('long_alignment_is_detected',row(650),1),('duplicate_is_not_independent_evidence',row(650,1024),0)]


def execute(host,batch):
    from agent_client import AgentClient
    lease=uuid.uuid4().hex;name='genomics-'+lease;client=AgentClient(host)
    acquired=False
    try:
        status=client.call('status',timeout=10)
        if not distributed.eligible(status) or status.get('machine')!=host['machine']:raise RuntimeError('Worker identity or policy changed')
        limits=client.call('resource.capacity',{'group':'genomics-dat1-private'})
        if limits.get('available_mb',0)<256 or limits.get('available_cpus',0)<1:raise distributed.CapacityWait('Waiting for shared CPU/RAM capacity')
        acquired=client.call('resource.acquire',{'id':lease,'group':'genomics-dat1-private','cpus':1,'memory_mb':256,'container':name})['acquired']
        if not acquired:raise distributed.CapacityWait('Capacity changed before reservation')
        return distributed.execute_reserved(host,batch,name,256,group='genomics-dat1-private')
    finally:
        try:
            if acquired:client.call('resource.release',{'id':lease})
        finally:client.close()


def execute_when_ready(host,batch):
    deadline=time.monotonic()+900
    while True:
        if distributed.STOP.is_set():raise InterruptedError('Private run stopped')
        try:return execute(host,batch)
        except distributed.CapacityWait:
            if time.monotonic()>=deadline:raise
            time.sleep(5)


def save_state(folder,state):
    temporary=folder/('state-'+uuid.uuid4().hex+'.tmp')
    write_json(temporary,state)
    temporary.replace(folder/'state.json')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--bam',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    os.umask(0o077);folder=private_run(args.output)
    for kind in (signal.SIGTERM,signal.SIGINT):signal.signal(kind,lambda *_:distributed.STOP.set())
    state={'status':'preparing','locus':LOCUS,'worker_code_sha256':hashlib.sha256(WORKER.encode()).hexdigest(),'jobs':{},'genotype':'not_assessed','conclusion':'inconclusive','absence_confirmed':False}
    def save():save_state(folder,state)
    save();print('Private DAT1 run:',folder,flush=True)
    try:
        hosts=[h for h in distributed.discover() if h.get('eligible')]
        hosts.sort(key=lambda h:(not h.get('local'),h['host']))
        hosts=hosts[:2]
        if len(hosts)<2:
            state.update(status='waiting_for_two_enrolled_workers');save();return
        # Existing executor supplies authenticated, encrypted, RAM-only transport,
        # immutable approved Python images, Docker isolation and output hashes.
        distributed.WORKER=WORKER
        control_results=[]
        for name,sam,expected in controls():
            batch=package(sam)
            for host in hosts:
                result=execute_when_ready(host,batch)
                if result['direct_spanning_templates']!=expected or result['absence_supported'] is not False:raise RuntimeError('Evidence-gating control failed')
                control_results.append({'control':name,'host':host['host'],'passed':True})
        write_json(folder/'controls.json',control_results)
        stat=args.bam.stat();state['input_identity']={'path':str(args.bam.resolve()),'bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns}
        header=subprocess.check_output(['samtools','view','-H',str(args.bam)],text=True)
        if not any(line.startswith('@SQ\tSN:5\tLN:181538259') for line in header.splitlines()):raise ValueError('BAM chromosome 5 does not match GRCh38')
        # All target alignments are audited; low-quality/duplicate evidence is counted
        # separately, never silently interpreted as a negative allele.
        sam=subprocess.check_output(['samtools','view',str(args.bam),'5:1390000-1397500'],text=True)
        batch=package(sam);state.update(status='running',input_sha256=batch['sha256']);save()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda host:execute_when_ready(host,batch),hosts))
        for result in results:
            write_json(folder/(result['host']+'.json'),result);state['jobs'][result['host']]={'status':'passed','input_sha256':result['sha256']}
        comparable=lambda r:{k:v for k,v in r.items() if k not in ('host','elapsed_seconds','memory_mb','input_bytes','dataset')}
        if comparable(results[0])!=comparable(results[1]):raise RuntimeError('Workers disagree; no conclusion permitted')
        if args.bam.stat().st_mtime_ns!=stat.st_mtime_ns or args.bam.stat().st_size!=stat.st_size:raise RuntimeError('Input changed during analysis')
        state.update(status='completed_callability_audit',summary=comparable(results[0]),reproducibility='two authenticated workers agree',
            blockers=['No target-specific model in the recommended adVNTR GRCh38 database. A custom model needs locus-specific validation.',
                      'A negative repeat genotype requires validated detection of both alleles and appropriate positive/negative controls. This audit is not genotyping.'],
            next_step='Validated DAT1 repeat genotyping; use locus-specific PCR/fragment sizing or suitable long-read sequencing if existing reads cannot resolve both alleles.')
        save();print('Callability audit finished. DAT1 10-repeat absence is NOT confirmed.',flush=True)
    except Exception as error:
        state.update(status='failed',error=type(error).__name__+': '+str(error)[:500]);save()
        raise


if __name__=='__main__':main()
