"""Search the verified, downloaded HuRef FASTQ for DAT1 candidate reads.

Recruitment is not repeat genotyping. Reports and recruited reads remain outside
Git; no published genotype is used as an analysis result.
"""
import argparse
import hashlib
import json
import os
import signal
from pathlib import Path
import re
import subprocess
from scripts import genomics_distributed as distributed
from scripts.genomics import private_run
from scripts.wgs_recall_swarm import leased_run


def main():
    os.umask(0o077)
    def cancel(*_):raise InterruptedError('DAT1 source-read search cancelled')
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,cancel)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True,help='Immutable local ID from genomics/dat1-readscan-image')
    parser.add_argument('--resume-no-hit-prefix',type=Path)
    args=parser.parse_args()
    if not re.fullmatch(r'sha256:[0-9a-f]{64}',args.image):parser.error('Use an immutable local Docker image ID')
    root=Path('/mnt/PILEDRIVE/genomics-workbench/wgs')
    downloads=json.loads((root/'downloads/state.json').read_text())
    row=next(v for v in downloads['jobs'].values() if v.get('filename')=='SRR7097858_1.fastq.gz')
    if row.get('status')!='verified':raise ValueError('Source FASTQ is not verified')
    receipt=row['receipt'];source=Path(receipt['path']);before=source.stat()
    if before.st_size!=receipt['bytes'] or before.st_mtime_ns!=receipt['mtime_ns']:raise ValueError('Source identity changed after verification')
    text=subprocess.check_output(['samtools','faidx',str(root/'reference/reference.fa'),'5:1393082-1394485'],text=True)
    context=''.join(text.splitlines()[1:])
    folder=private_run('/mnt/PILEDRIVE/genomics-workbench/venter-dat1');(folder/'tmp').mkdir(mode=0o700)
    worker=Path(__file__).with_name('genomics_dat1_readscan_worker.py').read_bytes()
    job={'run':'SRR7097858','source_sha256':receipt['sha256'],'source_bytes':receipt['bytes'],'reference_context':context,
         'reference_start':1393081,'reference_end':1394485,'worker_code_sha256':hashlib.sha256(worker).hexdigest()}
    if args.resume_no_hit_prefix:
        previous=json.loads(args.resume_no_hit_prefix.read_text());old_job=json.loads(args.resume_no_hit_prefix.with_name('job.json').read_text())
        if previous.get('candidate_reads')!=0 or previous.get('source_receipt_sha256')!=receipt['sha256'] or old_job['reference_context']!=context:
            raise ValueError('Resume requires the same source and target, with a recorded no-hit prefix')
        job.update(verified_no_hit_prefix_bytes=previous['uncompressed_bytes_scanned'],prefix_report=str(args.resume_no_hit_prefix.resolve()))
    (folder/'scan_worker.py').write_bytes(worker);(folder/'job.json').write_text(json.dumps(job,indent=2)+'\n')
    host=next(h for h in distributed.discover() if h.get('eligible') and h.get('local'))
    print('Private DAT1 source-read search:',folder,flush=True)
    leased_run(host,args.image,'python /work/scan_worker.py',[(folder,'/work',False),(source,'/input/reads.fastq.gz',True)],str(folder),8,4096,folder/'execution.json')
    after=source.stat()
    if before.st_size!=after.st_size or before.st_mtime_ns!=after.st_mtime_ns:raise ValueError('Source changed during scan')
    print('Source-read search completed:',folder,flush=True)


if __name__=='__main__':main()
