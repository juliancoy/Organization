"""Bounded local Vulkan QC worker using the existing shared CPU/RAM pool."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
RESOURCE_DIR=ROOT/'.runtime/launcher-jobs/.resources'


@contextmanager
def admission(host,memory_mb,stop):
    from .wgs_swarm_download import AgentClient
    from .launcher_agent import ResourceLease
    identity=hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest()
    if not host.get('local') or identity!=host['machine']:raise RuntimeError('Native GPU work must remain on the attested local machine')
    client=AgentClient(host);lease=None
    try:
        status=client.call('status')
        if status['machine']!=identity or not status['worker_policy']['accepting_jobs']:raise RuntimeError('Local worker identity or policy changed')
        # This native controller owns its flock lease independently of agent reloads.
        # Read authoritative host limits rather than treating our service cgroup
        # limit as the capacity of the whole shared worker pool.
        limits=client.call('resource.capacity',{'group':'gpu-qc-native'})
        lease=ResourceLease('gpu-qc-native',1,memory_mb,directory=RESOURCE_DIR,limits=limits,pids=64)
        while not lease.try_acquire():
            if stop.wait(2):raise InterruptedError('Native GPU work stopped')
        yield
    finally:
        if lease:lease.close()
        client.close()


def run_native(host,item,ident,output,changed,stop,verify=False):
    from .wgs_stream_qc import aggregate_qc,CHUNK_BYTES,qc_memory_mb
    from .wgs_swarm_download import atomic_json
    root=Path(host['cache']).parent/'gpu-qc'
    config={'local_root':str(root),'host_root':str(root),'verify':verify,'verify_first':True}
    memory=qc_memory_mb(min(CHUNK_BYTES,item['bytes']),gpu_parallel=True)
    metadata={key:item[key] for key in ('sample','run','filename') if key in item}
    metadata.update(host=host['host'],memory_mb=memory)
    if not (root/'service.sock').is_socket():raise RuntimeError('Local GPU service is not ready')
    def progress(value):
        value={**value,**metadata}
        atomic_json(Path(output)/(ident+'.json'),value)
        if changed:changed(value)
    with admission(host,memory,stop):
        result=aggregate_qc(Path(host['cache'])/ident,item,CHUNK_BYTES,progress=progress,stop=stop,gpu=config,gpu_parallel=True)
    result={**result,**metadata}
    atomic_json(Path(output)/(ident+'.json'),result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--hosts',type=Path,required=True);parser.add_argument('--host',default='compai.local');parser.add_argument('--work',type=Path,required=True);args=parser.parse_args()
    stop=threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stop.set())
    host=next(h for h in json.loads(args.hosts.read_text()) if h['host']==args.host)
    work=args.work.absolute()
    if any(p.is_symlink() or (p/'.git').exists() for p in [work,*work.parents]):raise ValueError('GPU batch state must be private and outside Git')
    work.mkdir(mode=0o700,parents=True,exist_ok=True);os.umask(0o077)
    guard=(work/'native-controller.lock').open('a');fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
    assets=ROOT/'scripts/gpu-tools';names=['qc_engine.c','qc_engine.comp','qc_server.py','genome_masks.comp','qc_engine_parallel.comp']
    version=hashlib.sha256(b''.join((assets/name).read_bytes() for name in names)).hexdigest()
    process=None
    try:
        with admission(host,1024,stop):
            for name in names:shutil.copyfile(assets/name,work/name)
            subprocess.run(['glslangValidator','-V','qc_engine_parallel.comp','-o','qc_engine.spv'],cwd=work,check=True)
            subprocess.run(['glslangValidator','-V','genome_masks.comp','-o','genome_masks.spv'],cwd=work,check=True)
            subprocess.run(['gcc','-O3','-march=native','qc_engine.c','-lvulkan','-o','qc_engine'],cwd=work,check=True)
            env={**os.environ,'GPU_QC_ROOT':str(work),'GPU_QC_HOST_ROOT':str(work),'GPU_QC_VERSION':version,'GPU_QC_MAX_BATCH':'16777216','GPU_QC_WORKGROUPS':'1','VK_ICD_FILENAMES':'/usr/share/vulkan/icd.d/nvidia_icd.json'}
            process=subprocess.Popen([sys.executable,'qc_server.py'],cwd=work,env=env,start_new_session=True)
            try:
                while not stop.wait(1):
                    if process.poll() is not None:raise RuntimeError('Native GPU worker stopped')
            finally:
                if process:
                    try:os.killpg(process.pid,signal.SIGTERM)
                    except ProcessLookupError:pass
                    try:process.wait(timeout=15)
                    except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()
    finally:guard.close()


if __name__=='__main__':main()
