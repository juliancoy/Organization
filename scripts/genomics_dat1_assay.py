"""Run public-reference DAT1 caller controls in an isolated, leased worker.

This entry point never loads private sample alignments. A passing software test
is not validation of DAT1 genotyping from 150-bp WGS reads.
"""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import uuid
from scripts.genomics import private_run,write_json
from scripts.genomics_dat1 import save_state
from scripts import genomics_distributed as distributed

IMAGE='sha256:f5eded2437aea78b32750b8b79cc325b43f5125ff3842bae32d0bba553f72aa4'


def check_report(report):
    """Fail closed on an incomplete or internally inconsistent control report."""
    required={'long_9_9','long_9_10','long_10_10','short_9_9','short_9_10','short_10_10'}
    cases=report.get('controls',[])
    if len(cases)!=6 or {c['case'] for c in cases}!=required:
        raise ValueError('Incomplete DAT1 control panel')
    if not report.get('reference_reconstructed') or report.get('model_repeat_units')!=10:
        raise ValueError('Reference model validation failed')
    if report.get('absence_confirmed') is not False or report.get('private_sample_genotyped') is not False:
        raise ValueError('Public controls must not make a private genotype claim')
    for case in cases:
        expected=None if case['case'].startswith('short') else [int(n) for n in case['case'].split('_')[1:]]
        if case.get('expected')!=expected:
            raise ValueError('Unexpected control target')
        passed=case.get('exit_code')==0 and case.get('output_valid') is True and case.get('genotype')==expected
        if case.get('passed')!=passed:raise ValueError('Inconsistent control result')
    software_passed=all(c['passed'] for c in cases)
    if software_passed != (report.get('status')=='software_controls_passed_short_reads_unresolved'):
        raise ValueError('Inconsistent validation status')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',default=IMAGE,help='Immutable local image ID produced by genomics/dat1-image')
    args=parser.parse_args()
    if not re.fullmatch(r'sha256:[0-9a-f]{64}',args.image):parser.error('Use an immutable local Docker image ID')
    os.umask(0o077)
    folder=private_run('/mnt/PILEDRIVE/genomics-workbench/targeted-vntr-assay')
    source=Path(__file__).with_name('genomics_dat1_assay_worker.py').read_text()
    reference='/mnt/PILEDRIVE/JulianDNA/ref/Homo_sapiens.GRCh38.dna.primary_assembly.fa'
    text=subprocess.check_output(['samtools','faidx',reference,'5:1392582-1395585'],text=True)
    context=''.join(line.strip() for line in text.splitlines() if not line.startswith('>'))
    payload=gzip.compress(json.dumps({'reference_context':context,'context_start':1392581,'start':1393581,'end':1393985}).encode(),mtime=0)
    hosts=distributed.discover();host=next(h for h in hosts if h.get('eligible') and h.get('local'))
    from agent_client import AgentClient
    client=AgentClient(host);lease=uuid.uuid4().hex;name='genomics-'+lease
    acquired=False;done=threading.Event();lease_errors=[];renewer=None
    state={'status':'validating','image':args.image,'worker':host['host'],'code_sha256':hashlib.sha256(source.encode()).hexdigest(),'reference_window_sha256':hashlib.sha256(context.encode()).hexdigest(),'private_sample_genotyped':False,'absence_confirmed':False}
    save_state(folder,state);print('Public-reference assay validation, private diagnostics:',folder,flush=True)
    def interrupt(*_):raise InterruptedError('Assay validation cancelled')
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,interrupt)
    def renew():
        other=AgentClient(host)
        try:
            while not done.wait(45):
                try:other.call('resource.renew',{'id':lease},timeout=10)
                except Exception as error:
                    lease_errors.append(type(error).__name__)
                    subprocess.run(['docker','kill',name],capture_output=True,timeout=15)
                    return
        finally:other.close()
    try:
        status=client.call('status')
        if not distributed.eligible(status) or status.get('machine')!=host['machine']:raise RuntimeError('Worker identity/policy changed')
        if 'renewable-resources' not in status.get('capabilities',[]):raise RuntimeError('Renewable leases unavailable')
        capacity=client.call('resource.capacity',{'group':'genomics-dat1-assay'})
        if capacity.get('available_mb',0)<2048 or capacity.get('available_cpus',0)<1:raise distributed.CapacityWait('Waiting for 1 CPU and 2 GiB assay capacity')
        acquired=client.call('resource.acquire',{'id':lease,'group':'genomics-dat1-assay','cpus':1,'memory_mb':2048,'container':name})['acquired']
        if not acquired:raise distributed.CapacityWait('Capacity reservation changed')
        client.call('resource.renew',{'id':lease})
        if 'docker-workers' not in status.get('capabilities',[]):
            renewer=threading.Thread(target=renew,daemon=True);renewer.start()
        cmd=['docker','run','--pull=never','--name',name,'--label','org.lifetech.genomics=true','--label','org.lifetech.worker=true','--label','org.lifetech.job='+name.removeprefix('genomics-'),'--label','org.lifetech.group='+'genomics-dat1-assay','-i','--network','none','--read-only','--log-driver','none','--memory','2048m','--memory-swap','2048m','--cpus','1','--pids-limit','64','--cap-drop','ALL','--security-opt','no-new-privileges','--ulimit','core=0:0','--tmpfs','/work:rw,nosuid,nodev,size=512m,mode=1777','--user','1000:1000','-e','HOME=/work','-e','TMPDIR=/work','-e','OPENBLAS_NUM_THREADS=1','-e','OMP_NUM_THREADS=1','-e','PYTHONDONTWRITEBYTECODE=1',args.image,'python','-c',source]
        result=subprocess.run(cmd,input=payload,capture_output=True,timeout=1200)
        (folder/'worker.stderr').write_bytes(result.stderr)
        inspected=subprocess.run(['docker','inspect','--format','{{json .State}}',name],capture_output=True,text=True,timeout=15)
        detail=json.loads(inspected.stdout)
        state['container_state']={k:detail.get(k) for k in ('Status','ExitCode','OOMKilled','Error')}
        if result.returncode or lease_errors:raise RuntimeError('Assay validation worker failed; private diagnostics retained')
        report=json.loads(result.stdout)
        write_json(folder/'assay.json',report)
        if report.get('status')!='model_validation_failed':check_report(report)
        state.update(status=report['status'],result=str(folder/'assay.json'))
    except BaseException as error:
        state.update(status='failed',error=type(error).__name__+': '+str(error));raise
    finally:
        done.set()
        if renewer:renewer.join(timeout=30)
        try:
            removed=subprocess.run(['docker','rm','-f',name],capture_output=True,text=True,timeout=20)
            # A missing container is also expected if admission failed before run.
            cleaned=removed.returncode==0 or 'No such container:' in removed.stderr or 'No such object:' in removed.stderr
            state['container_cleanup_confirmed']=cleaned
            if not cleaned:
                state.update(status='failed',cleanup_error='Container cleanup unconfirmed; lease retained for expiry cleanup')
                raise RuntimeError(state['cleanup_error'])
            if acquired:client.call('resource.release',{'id':lease})
        finally:
            client.close();save_state(folder,state)
    print('Validation result:',state['status'],flush=True)


if __name__=='__main__':main()
