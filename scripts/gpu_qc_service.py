"""Durable signed-lease coordinator for a private persistent Vulkan QC worker."""
import argparse,base64,hashlib,json,signal,subprocess,threading,time,uuid
from pathlib import Path
from . import genomics_distributed as distributed
from .gpu_qc_benchmark import SETUP
from .wgs_swarm_download import AgentClient,atomic_json,release_after_cleanup
STOP=threading.Event()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--host',required=True);parser.add_argument('--hosts',type=Path);parser.add_argument('--state',type=Path,required=True);args=parser.parse_args()
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:STOP.set())
    args.state.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    assets=Path(__file__).parent/'gpu-tools';names=['qc_engine.c','qc_engine.comp','qc_server.py'];version=hashlib.sha256(b''.join((assets/n).read_bytes() for n in names)).hexdigest()
    state={'status':'starting','host':args.host,'runtime_version':version,'batches':0,'bases':0}
    def save(**updates):state.update(updates,updated=time.time());atomic_json(args.state,state)
    hosts=json.loads(args.hosts.read_text()) if args.hosts else distributed.discover();host=next(h for h in hosts if h['host']==args.host and h.get('eligible'))
    setup=SETUP.replace("gpu-dna-qc","gpu-qc")
    payload={'machine':host['machine'],'assets':{n:base64.b64encode((assets/n).read_bytes()).decode() for n in names}}
    result=subprocess.run(distributed.shell_command(host,['python3','-c',setup]),input=json.dumps(payload),capture_output=True,text=True,check=True,timeout=25);runtime=json.loads(result.stdout)
    root=runtime['work'];name='genomics-'+uuid.uuid4().hex;lease=uuid.uuid4().hex;client=AgentClient(host);done=threading.Event();process=None;acquired=False;renew_errors=[]
    try:
        status=client.call('status')
        if not distributed.eligible(status) or status['machine']!=host['machine']:raise RuntimeError('Worker policy or identity changed')
        while not STOP.is_set():
            capacity=client.call('resource.capacity',{'group':'gpu-qc-service'});memory=distributed.memory_allocation(capacity,max(1,host['cpus']))
            acquired=client.call('resource.acquire',{'id':lease,'group':'gpu-qc-service','cpus':1,'memory_mb':memory,'container':name})['acquired']
            if acquired:break
            save(status='waiting_capacity');STOP.wait(5)
        if not acquired:return
        max_batch=min(16*1024**2,memory*1024**2//16)
        def renew():
            other=AgentClient(host)
            try:
                while not done.wait(30):
                    try:other.call('resource.renew',{'id':lease})
                    except Exception as error:renew_errors.append(str(error));STOP.set();return
            finally:other.close()
        threading.Thread(target=renew,daemon=True).start()
        command='glslangValidator -V qc_engine.comp -o qc_engine.spv && gcc -O3 -march=native qc_engine.c -lvulkan -o qc_engine && exec python3 qc_server.py'
        argv=['docker','run','--rm','--pull=never','--name',name,'--label','org.lifetech.gpu-qc-service=true','--network','none','--read-only','--cpus','1','--memory',str(memory)+'m','--memory-swap',str(memory)+'m','--pids-limit','32','--cap-drop','ALL','--security-opt','no-new-privileges','--user',str(runtime['uid'])+':'+str(runtime['gid']),'--group-add',str(runtime['render_gid']),'--device',runtime['render'],'-e','TMPDIR=/work/tmp','-e','XDG_CACHE_HOME=/work/tmp','-e','GPU_QC_ROOT=/gpu-qc','-e','GPU_QC_HOST_ROOT='+root,'-e','GPU_QC_VERSION='+version,'-e','GPU_QC_MAX_BATCH='+str(max_batch),'--mount','type=bind,src=/usr,dst=/usr,readonly','--mount','type=bind,src='+root+',dst=/work','--mount','type=bind,src='+root+',dst=/gpu-qc','-w','/work',host['image'],'bash','-o','pipefail','-c',command]
        save(status='starting',memory_mb=memory,cpus=1,container=name,socket=root+'/service.sock',max_batch_bytes=max_batch)
        process=subprocess.Popen(distributed.shell_command(host,argv));next_scan=0
        while process.poll() is None and not STOP.wait(1):
            if time.monotonic()<next_scan:continue
            next_scan=time.monotonic()+5
            try:
                command=['python3','-c','import pathlib,sys; p=pathlib.Path(sys.argv[1]); print(p.read_text() if p.exists() else "{}")',root+'/daemon.json']
                row=json.loads(subprocess.check_output(distributed.shell_command(host,command),text=True,timeout=10))
                if row.get('runtime_version')==version:save(**{k:row[k] for k in ('status','batches','bases','device','startup_fixtures') if k in row})
            except (subprocess.SubprocessError,ValueError):pass
        if process.poll() is not None or renew_errors:raise RuntimeError('Persistent GPU QC worker stopped: '+str(process.returncode))
    finally:
        done.set()
        try:
            if acquired:release_after_cleanup(host,name,client,lease)
            else:client.call('resource.release',{'id':lease})
        finally:client.close();save(status='offline')


if __name__=='__main__':main()
