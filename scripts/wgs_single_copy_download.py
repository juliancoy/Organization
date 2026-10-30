"""Run a single-copy public WGS download on one explicitly selected swarm peer."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import subprocess


def failure_exit_code(error):
    """Retry transport/lease failures; keep integrity and identity failures stopped."""
    if isinstance(error, PermissionError):
        return 78
    if isinstance(error, (OSError, subprocess.SubprocessError, distributed.CapacityWait)):
        return 75
    if isinstance(error, RuntimeError) and str(error) in (
        "Download lease renewal failed",
        "Owned WGS container cleanup unconfirmed; lease retained for server expiry",
    ):
        return 75
    return 78
from scripts import genomics_distributed as distributed
from scripts import wgs_swarm_download as download


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--hosts',type=Path,required=True)
    parser.add_argument('--host',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();os.umask(0o077)
    args.output.mkdir(parents=True,exist_ok=True,mode=0o700)
    if any((p/'.git').exists() for p in [args.output,*args.output.parents]):raise ValueError('Download state must be outside Git')
    import fcntl
    lock=(args.output/'run.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:download.STOP.set())
    files=download.validate_manifest(json.loads(args.manifest.read_text()))
    if len(files)!=1:raise ValueError('Single-copy job requires exactly one manifest file')
    host=next(h for h in json.loads(args.hosts.read_text()) if h['host']==args.host and h.get('eligible'))
    host=download.preflight(host,Path('/unused-remote-cache'),scan_sizes=False,timeout=120)
    if host.get('local'):raise ValueError('This entry point requires an explicitly enrolled remote peer')
    download.DOWNLOAD_WORKER=Path(__file__).with_name('wgs_single_copy_worker.py').read_text()
    ident=download.file_id(files[0]);state={'status':'queued','started':download.stamp(),'host':host['host'],'cache':host['cache'],
        'manifest_sha256':hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        'storage_mode':'single-copy-resumable','worker_sha256':hashlib.sha256(download.DOWNLOAD_WORKER.encode()).hexdigest(),
        'jobs':{ident:{**files[0],'host':host['host'],'status':'queued'}}}
    def save():state['updated']=download.stamp();download.atomic_json(args.output/'state.json',state)
    def changed(key,status,extra=None):
        if key is None:
            row=extra['progress_batch'].get(ident)
            if row and state['jobs'][ident]['status']!='verified':state['jobs'][ident].update(status=row['phase'],progress=row)
        else:state['jobs'][key].update(status=status,**(extra or {}))
        save()
    save();print('Single-copy download state:',args.output/'state.json',flush=True)
    try:
        state['status']='running';save()
        while not download.STOP.is_set():
            try:download.run_batch(host,files,changed,8,memory_floor_mb=1024);break
            except distributed.CapacityWait:
                state['status']='waiting_capacity';save();download.STOP.wait(5)
        state['status']='stopped' if download.STOP.is_set() else 'passed' if state['jobs'][ident]['status']=='verified' else 'failed'
        state['finished']=download.stamp();save()
        if state['status']=='failed':raise RuntimeError('Download did not produce a verified receipt')
    except Exception as error:
        state.update(status='failed',error=type(error).__name__+': '+str(error)[:500]);save();raise


if __name__=='__main__':
    try:main()
    except Exception as error:
        print(type(error).__name__+': '+str(error),file=sys.stderr,flush=True)
        raise SystemExit(failure_exit_code(error))
