"""Run one hardware-only Vulkan DNA count crosscheck under a signed worker lease."""
import argparse,base64,hashlib,json,shlex,signal,subprocess,threading,time,uuid
from pathlib import Path
from . import genomics_distributed as distributed
from .wgs_swarm_download import AgentClient,atomic_json,release_after_cleanup


def validate_result(result):
    if result.get('software_fallback') is not False or result.get('device_type') not in (1,2):
        raise ValueError('A physical integrated or discrete GPU is required')
    counts=result.get('counts',{})
    if set(counts)!={'A','C','G','T','N','other'} or any(type(v)is not int or v<0 for v in counts.values()):
        raise ValueError('Invalid aggregate counts')
    if result.get('exact_match') is not True or sum(counts.values())!=result.get('bases'):
        raise ValueError('GPU/CPU count validation failed')
    return result


SETUP=r'''import base64,hashlib,json,os,pathlib,sys
job=json.load(sys.stdin)
if hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes()).hexdigest()!=job['machine']:raise RuntimeError('Worker identity changed')
p=pathlib.Path.home()/'.local/state/lifetech-launcher/gpu-dna-qc'
if any(x.is_symlink() or (x/'.git').exists() for x in [p,*p.parents]):raise RuntimeError('Unsafe work directory')
p.mkdir(parents=True,exist_ok=True,mode=0o700);(p/'tmp').mkdir(exist_ok=True)
if p.stat().st_uid!=os.getuid():raise RuntimeError('Work directory ownership changed')
p.chmod(0o700)
for name,value in job['assets'].items():(p/name).write_bytes(base64.b64decode(value))
renders=sorted(pathlib.Path('/dev/dri').glob('renderD*'))
if not renders:raise RuntimeError('No render device')
render=renders[0]
print(json.dumps({'work':str(p),'uid':os.getuid(),'gid':os.getgid(),'render':str(render),'render_gid':render.stat().st_gid}))
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--host',required=True);parser.add_argument('--hosts',type=Path)
    parser.add_argument('--state',type=Path,default=Path('/mnt/PILEDRIVE/genomics-workbench/wgs/downloads/state.json'))
    parser.add_argument('--run');parser.add_argument('--batch-bytes',type=int,default=16*1024*1024);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    if not 0<args.batch_bytes<=1024**3:parser.error('Batch must contain 1..1073741824 bases')
    hosts=json.loads(args.hosts.read_text()) if args.hosts else distributed.discover();host=next(h for h in hosts if h['host']==args.host and h.get('eligible'))
    jobs=json.loads(args.state.read_text())['jobs'].values();candidates=[j['receipt'] for j in jobs if j['status']=='verified' and j['host']==args.host and (not args.run or j['run']==args.run)]
    if not candidates:raise RuntimeError('No publisher-verified FASTQ on selected worker')
    source=max(candidates,key=lambda x:x['bytes']);assets=Path(__file__).parent/'gpu-tools';names=['base_counts.c','base_counts.comp','extract_batch.py']
    payload={'machine':host['machine'],'assets':{n:base64.b64encode((assets/n).read_bytes()).decode() for n in names}}
    stage=subprocess.run(distributed.shell_command(host,['python3','-c',SETUP]),input=json.dumps(payload),capture_output=True,text=True,timeout=25,check=True);runtime=json.loads(stage.stdout)
    name='genomics-'+uuid.uuid4().hex;lease=uuid.uuid4().hex;client=AgentClient(host);done=threading.Event();process=None;renew_errors=[];acquired=False
    def interrupted(*_):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,interrupted)
    try:
        status=client.call('status')
        if not distributed.eligible(status) or status['machine']!=host['machine']:raise RuntimeError('Worker identity or admission policy changed')
        capacity=client.call('resource.capacity',{'group':'gpu-qc'});memory=distributed.memory_allocation(capacity,max(1,host['cpus']))
        if args.batch_bytes>memory*1024**2//8:raise RuntimeError('Batch exceeds a safe share of current signed memory capacity')
        acquired=client.call('resource.acquire',{'id':lease,'group':'gpu-qc','cpus':1,'memory_mb':memory,'container':name})['acquired']
        if not acquired:raise RuntimeError('Worker admission capacity changed')
        def renew():
            other=AgentClient(host)
            try:
                while not done.wait(30):
                    try:other.call('resource.renew',{'id':lease})
                    except Exception as error:
                        renew_errors.append(str(error))
                        if process:process.terminate()
                        return
            finally:other.close()
        threading.Thread(target=renew,daemon=True).start()
        command="trap 'rm -f bases.bin' EXIT; python3 extract_batch.py /input.fastq.gz "+shlex.quote(source['sha256'])+' '+str(args.batch_bytes)+' && glslangValidator -V base_counts.comp -o count.spv && gcc -O3 -march=native base_counts.c -lvulkan -o count && ./count bases.bin count.spv && cat batch.json'
        argv=['docker','run','--rm','--pull=never','--name',name,'--label','org.lifetech.gpu-qc=true','--network','none','--read-only','--cpus','1','--memory',str(memory)+'m','--memory-swap',str(memory)+'m','--pids-limit','32','--cap-drop','ALL','--security-opt','no-new-privileges','--user',str(runtime['uid'])+':'+str(runtime['gid']),'--group-add',str(runtime['render_gid']),'--device',runtime['render'],'-e','TMPDIR=/work/tmp','-e','XDG_CACHE_HOME=/work/tmp','--mount','type=bind,src=/usr,dst=/usr,readonly','--mount','type=bind,src='+runtime['work']+',dst=/work','--mount','type=bind,src='+source['path']+',dst=/input.fastq.gz,readonly','-w','/work',host['image'],'bash','-o','pipefail','-c',command]
        process=subprocess.Popen(distributed.shell_command(host,argv),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True);stdout,stderr=process.communicate(timeout=180)
        if process.returncode or renew_errors:raise RuntimeError('GPU crosscheck failed: '+stderr[-1000:])
        rows=[json.loads(line) for line in stdout.splitlines() if line.startswith('{')];result=validate_result(next(r for r in rows if 'exact_match'in r));batch=next(r for r in rows if 'prefix_batch'in r)
        receipt={'host':host['host'],'sample':source['sample'],'run':source['run'],'publisher_md5':source['md5'],'input':batch,'result':result,'memory_mb':memory,'cpus':1,'render_gid':runtime['render_gid'],'image':host['image'],'source_sha256':{n:hashlib.sha256((assets/n).read_bytes()).hexdigest() for n in names},'finished':time.time(),'pipeline_acceleration_enabled':False,'timing_scope':'Count stage only; gzip decoding and source validation excluded'}
        args.output.parent.mkdir(parents=True,exist_ok=True,mode=0o700);atomic_json(args.output,receipt);print(json.dumps(receipt))
    finally:
        done.set()
        try:
            if acquired:release_after_cleanup(host,name,client,lease)
        finally:client.close()


if __name__=='__main__':main()
