"""Stream aggregate FASTQ QC from verified public download chunks, before file completion."""
import argparse
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import resource
import socket
from collections import Counter
import signal
import subprocess
import threading
import time
import uuid

CHUNK_BYTES=128*1024*1024
LINE_BYTES=1024*1024
STOP=threading.Event()
GROUP='genomics-wgs-stream-qc'
HOST_LOCKS={}
HOST_LOCK_GUARD=threading.Lock()


class OrderedChunks(io.RawIOBase):
    """Read only complete SHA-checked ranges; final-file fallback preserves byte offset."""
    def __init__(self,folder,item,chunk_bytes=CHUNK_BYTES,waiting=None,stop=None):
        self.folder=Path(folder);self.item=item;self.chunk_bytes=chunk_bytes;self.waiting=waiting or (lambda:None);self.stop=stop or STOP
        self.offset=0;self.data=None;self.final=None;self.md5=hashlib.md5();self.verified_chunks=0
    def readable(self):return True
    def receipt(self):
        p=self.folder/'receipt.json'
        try:r=json.loads(p.read_text())
        except (FileNotFoundError,json.JSONDecodeError):return None
        if any(r.get(k)!=self.item[k] for k in ('url','bytes','md5','filename')):raise ValueError('Archive receipt differs from source manifest')
        final=self.folder/self.item['filename']
        if not final.is_file() or final.stat().st_size!=self.item['bytes'] or final.stat().st_mtime_ns!=r.get('mtime_ns'):raise ValueError('Archive final file changed after verification')
        return r
    def _next(self):
        if self.offset>=self.item['bytes']:return False
        index=self.offset//self.chunk_bytes;start=index*self.chunk_bytes
        while not self.stop.is_set():
            receipt=self.receipt()
            if receipt:
                self.final=(self.folder/self.item['filename']).open('rb');self.final.seek(self.offset);return True
            chunk=self.folder/'chunks'/(str(index)+'.bin');metadata=chunk.with_suffix('.json')
            if chunk.exists() and metadata.exists():
                try:
                    info=json.loads(metadata.read_text());data=chunk.read_bytes()
                except FileNotFoundError:continue  # Assembler may replace chunks with a verified final file.
                expected=min(self.chunk_bytes,self.item['bytes']-start)
                if len(data)!=expected or info.get('bytes')!=expected or hashlib.sha256(data).hexdigest()!=info.get('sha256'):
                    raise ValueError('Complete archive chunk failed verification')
                self.data=io.BytesIO(data);self.data.seek(self.offset-start);self.verified_chunks+=1;return True
            self.waiting();self.stop.wait(.2)
        raise InterruptedError('Streaming QC stopped')
    def readinto(self,buffer):
        if not len(buffer):return 0
        if self.offset>=self.item['bytes']:return 0
        while True:
            stream=self.final or self.data
            if stream is None:
                if not self._next():return 0
                stream=self.final or self.data
            count=stream.readinto(buffer)
            if count:
                self.offset+=count;self.md5.update(memoryview(buffer)[:count]);return count
            if self.final:raise ValueError('Verified archive ended before expected byte count')
            self.data.close();self.data=None
    def close(self):
        if self.final:self.final.close()
        if self.data:self.data.close()
        super().close()


BATCH_BASES=4*1024*1024
BASE_KEYS=('A','C','G','T','N','other')


def cpu_batch_counts(sequence,quality):
    """Count one bounded batch; shared fallback keeps transactions exact."""
    bases=Counter(sequence.upper());hist=Counter(quality)
    counts={base:bases[ord(base)] for base in BASE_KEYS[:-1]}
    counts['other']=len(sequence)-sum(counts.values())
    histogram=[hist[value+33] for value in range(94)]
    return {'counts':counts,'quality_histogram':histogram,
            'quality_sum':sum(i*n for i,n in enumerate(histogram)),
            'q20':sum(histogram[20:]),'q30':sum(histogram[30:])}


def validate_gpu_counts(value,length,request_id=None):
    """Reject partial, mismatched or software responses before committing anything."""
    if not isinstance(value,dict) or value.get('version')!=1 or value.get('backend')!='vulkan' or value.get('software_fallback') is not False or value.get('device_type') not in (1,2):raise ValueError('Unverified GPU backend')
    if request_id is not None and value.get('request_id')!=request_id:raise ValueError('GPU request identity differs')
    if value.get('exact_match') is not True and value.get('validated_backend') is not True:raise ValueError('GPU backend fixtures not verified')
    if value.get('exact_match') is False:raise ValueError('GPU CPU crosscheck failed')
    counts=value.get('counts',{});hist=value.get('quality_histogram',[])
    if set(counts)!=set(BASE_KEYS) or any(type(n) is not int or n<0 for n in counts.values()) or sum(counts.values())!=length:raise ValueError('GPU base totals differ')
    if len(hist)!=94 or any(type(n) is not int or n<0 for n in hist) or sum(hist)!=length or value.get('bases')!=length:raise ValueError('GPU quality totals differ')
    if value.get('quality_sum')!=sum(i*n for i,n in enumerate(hist)) or value.get('q20')!=sum(hist[20:]) or value.get('q30')!=sum(hist[30:]):raise ValueError('GPU quality summaries differ')
    return value


def request_gpu_batch(sequence,quality,config):
    """Private files and one bounded RPC; a failed transaction is never committed."""
    ident=uuid.uuid4().hex;local=Path(config['local_root']);batch=local/'batches';batch.mkdir(mode=0o700,parents=True,exist_ok=True)
    paths=[batch/(ident+'.seq'),batch/(ident+'.qual')]
    try:
        for path,data in zip(paths,(sequence,quality)):
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as stream:stream.write(data)
        request={'version':1,'request_id':ident,'sequence_path':config['host_root']+'/batches/'+paths[0].name,'quality_path':config['host_root']+'/batches/'+paths[1].name}
        if config.get('verify'):request['verify']=True
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
            client.settimeout(config.get('timeout',10));client.connect(str(local/'service.sock'));client.sendall(json.dumps(request).encode()+b'\n')
            with client.makefile('rb') as stream:line=stream.readline(65537)
        if not line.endswith(b'\n') or len(line)>65536:raise ValueError('Invalid GPU response framing')
        value=validate_gpu_counts(json.loads(line),len(sequence),ident)
        if config.get('verify') and (value.get('exact_match') is not True or value.get('cpu_crosschecked') is not True):raise ValueError('Requested GPU CPU crosscheck was not performed')
        return value
    finally:
        for path in paths:path.unlink(missing_ok=True)


class BatchCounts:
    """Commit each batch exactly once through GPU or CPU fallback."""
    def __init__(self,counts,batch_bases=BATCH_BASES,gpu_request=None,gpu_parallel=False):
        if batch_bases<1:raise ValueError('Batch size must be positive')
        self.counts=counts;self.limit=batch_bases;self.gpu_request=gpu_request;self.sequence=bytearray();self.quality=bytearray();self.pending=None;self.executor=None
        if gpu_parallel and gpu_request:
            from concurrent.futures import ThreadPoolExecutor
            self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix="qc-gpu")
        counts.update(base_counts={k:0 for k in BASE_KEYS},quality_histogram=[0]*94,gpu_batches=0,gpu_bases=0,cpu_batches=0,cpu_bases=0,gpu_fallback_batches=0,gpu_cpu_crosschecked_batches=0,qc_backend='cpu',histogram_source='cpu',gpu_devices=[])
    def add(self,sequence,quality):
        # Split exceptionally long records too; parser owns the record boundary.
        position=0
        while position<len(sequence):
            size=min(self.limit-len(self.sequence),len(sequence)-position)
            self.sequence.extend(sequence[position:position+size]);self.quality.extend(quality[position:position+size]);position+=size
            if len(self.sequence)==self.limit:self.flush()
    def evaluate(self,sequence,quality):
        gpu=False;fallback=False;value=None;length=len(sequence)
        if self.gpu_request:
            try:value=validate_gpu_counts(self.gpu_request(sequence,quality),length);gpu=True
            except (OSError,ValueError,RuntimeError,KeyError,TypeError,TimeoutError):fallback=True
        if not gpu:value=cpu_batch_counts(sequence,quality)
        return value,gpu,fallback,length

    def commit(self,result):
        value,gpu,fallback,length=result
        # Only this parser thread commits, once, after validation or CPU fallback.
        c=self.counts;c['gpu_fallback_batches']+=int(fallback)
        for key in BASE_KEYS:c['base_counts'][key]+=value['counts'][key]
        for i,n in enumerate(value['quality_histogram']):c['quality_histogram'][i]+=n
        c['gc_bases']+=value['counts']['G']+value['counts']['C'];c['ambiguous_bases']+=value['counts']['N']+value['counts']['other']
        c['quality_sum']+=value['quality_sum'];c['q20_bases']+=value['q20'];c['q30_bases']+=value['q30']
        c['gpu_batches' if gpu else 'cpu_batches']+=1;c['gpu_bases' if gpu else 'cpu_bases']+=length
        if gpu and value.get('cpu_crosschecked',value.get('exact_match') is True):c['gpu_cpu_crosschecked_batches']+=1
        if gpu and value.get('device') not in c['gpu_devices']:c['gpu_devices'].append(str(value.get('device','unknown'))[:200])
        c['qc_backend']='mixed' if c['gpu_bases'] and c['cpu_bases'] else 'vulkan' if c['gpu_bases'] else 'cpu';c['histogram_source']=c['qc_backend']

    def drain(self):
        if self.pending:
            future,_=self.pending;self.commit(future.result());self.pending=None

    def flush(self):
        if not self.sequence:return
        self.drain()
        sequence,quality=bytes(self.sequence),bytes(self.quality)
        self.sequence.clear();self.quality.clear()
        if self.executor:self.pending=(self.executor.submit(self.evaluate,sequence,quality),len(sequence))
        else:self.commit(self.evaluate(sequence,quality))

    def finish(self):
        self.flush();self.drain()

    def pending_bases(self):return len(self.sequence)+(self.pending[1] if self.pending else 0)

    def close(self):
        if self.executor:self.executor.shutdown(wait=True,cancel_futures=True)


def aggregate_qc(folder,item,chunk_bytes=CHUNK_BYTES,progress=None,stop=None,gpu=None,batch_bases=BATCH_BASES,gpu_request=None,gpu_parallel=False):
    """Four-line ENA FASTQ, Phred+33; sequences and identifiers never leave the parser."""
    progress=progress or (lambda value:None);stop=stop or STOP;last=[0.];started=time.monotonic()
    counts={'reads':0,'bases':0,'gc_bases':0,'ambiguous_bases':0,'quality_sum':0,'q20_bases':0,'q30_bases':0,'min_length':None,'max_length':0}
    checked_gpu=[False]
    def gpu_call(sequence,quality):
        config={**gpu,'verify':bool(gpu.get('verify') or gpu.get('verify_first') and not checked_gpu[0])}
        result=request_gpu_batch(sequence,quality,config);checked_gpu[0]=True;return result
    batch=BatchCounts(counts,batch_bases,gpu_request or (gpu_call if gpu else None),gpu_parallel)
    reader=None
    def report(phase,force=False):
        now=time.monotonic()
        if force or now-last[0]>=2:
            progress({'phase':phase,'provisional':phase!='verified','updated':time.time(),'elapsed_seconds':round(now-started,2),'compressed_bytes_read':reader.offset if reader else 0,'compressed_bytes_total':item['bytes'],'verified_chunks':reader.verified_chunks if reader else 0,'pending_batch_bases':batch.pending_bases(),**counts});last[0]=now
    reader=OrderedChunks(folder,item,chunk_bytes,waiting=lambda:report('waiting_for_complete_chunk'),stop=stop)
    report('starting',True)
    try:
        with io.BufferedReader(reader,buffer_size=1024*1024) as raw,gzip.GzipFile(fileobj=raw,mode='rb') as stream:
            while True:
                if stop.is_set():raise InterruptedError('Streaming QC stopped')
                header=stream.readline(LINE_BYTES+1)
                if not header:break
                sequence=stream.readline(LINE_BYTES+1).rstrip(b'\r\n');plus=stream.readline(LINE_BYTES+1);quality=stream.readline(LINE_BYTES+1).rstrip(b'\r\n')
                if len(header)>LINE_BYTES or len(sequence)>LINE_BYTES or len(plus)>LINE_BYTES or len(quality)>LINE_BYTES or not header.startswith(b'@') or not plus.startswith(b'+') or len(sequence)!=len(quality) or not sequence:
                    raise ValueError('Malformed or unsupported archive FASTQ record')
                if min(quality)<33 or max(quality)>126:raise ValueError('Invalid Phred+33 quality encoding')
                length=len(sequence)
                counts['reads']+=1;counts['bases']+=length;counts['max_length']=max(counts['max_length'],length);counts['min_length']=length if counts['min_length'] is None else min(counts['min_length'],length)
                batch.add(sequence,quality)
                report('streaming')
            batch.finish()
            if reader.offset!=item['bytes'] or reader.md5.hexdigest()!=item['md5']:raise ValueError('Full streaming input failed publisher MD5')
            if counts['reads']!=int(item['read_count']):raise ValueError('FASTQ read count differs from ENA run metadata')
            while reader.receipt() is None:
                if stop.is_set():raise InterruptedError('Streaming QC stopped')
                report('awaiting_publisher_receipt');stop.wait(.2)
        report('verified',True)
        return {**counts,'phase':'verified','provisional':False,'updated':time.time(),'compressed_bytes_read':item['bytes'],'compressed_bytes_total':item['bytes'],'verified_chunks':reader.verified_chunks,'input_md5':item['md5'],'expected_reads':int(item['read_count']),'gc_percent':100*counts['gc_bases']/counts['bases'] if counts['bases'] else None,'mean_phred':counts['quality_sum']/counts['bases'] if counts['bases'] else None,'scope':'Aggregate source-read QC only, not alignment, variant calls, reference matches or reconstruction of withheld sequence.'}
    finally:reader.close();batch.close()


def qc_memory_mb(chunk_bytes=CHUNK_BYTES,rss_kib=None,batch_bases=BATCH_BASES,gpu_parallel=False):
    """Two chunks, measured runtime, parser buffers, current/pending batches and CPU uppercase workspace."""
    rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if rss_kib is None else rss_kib
    return math.ceil((2*chunk_bytes+rss_kib*1024+4*LINE_BYTES+(5 if gpu_parallel else 3)*batch_bases)/(1024*1024))


def run_remote(host,item,ident,output,mapping_reserve_mb=0,changed=None,download_state=None,gpu_verify=False):
    if host.get('local') and host.get('gpu_qc_native'):
        from scripts.gpu_qc_native import run_native
        return run_native(host,item,ident,output,changed,STOP,gpu_verify)
    from scripts import genomics_distributed as distributed
    from scripts.wgs_swarm_download import AgentClient,atomic_json,release_after_cleanup
    name='genomics-'+uuid.uuid4().hex;lease=uuid.uuid4().hex;client=AgentClient(host);done=threading.Event();process=None;errors=[]
    gpu_enabled=host.get('gpu_qc') is True or host['host']=='julian-minipc.local'
    memory=qc_memory_mb(min(CHUNK_BYTES,item['bytes']),gpu_parallel=gpu_enabled);report_path=output/(ident+'.json')
    try:
        status=client.call('status')
        if status.get('machine')!=host['machine'] or not distributed.eligible(status) or 'renewable-resources' not in status.get('capabilities',[]):raise RuntimeError('Worker identity, policy or renewable leases unavailable')
        with HOST_LOCK_GUARD:guard=HOST_LOCKS.setdefault(host['host'],threading.Lock())
        with guard:
            capacity=client.call('resource.capacity',{'group':GROUP})
            if mapping_reserve_mb and capacity['available_mb']-mapping_reserve_mb<memory:raise distributed.CapacityWait('Requested mapping headroom is unavailable')
            request={'id':lease,'group':GROUP,'cpus':1,'memory_mb':memory,'container':name}
            acquired=client.call('resource.acquire',request)['acquired']
        deadline=time.monotonic()+120
        while not acquired:
            if STOP.wait(1):raise InterruptedError('QC stopped while waiting for capacity')
            if time.monotonic()>deadline:raise distributed.CapacityWait('Waiting for shared QC capacity')
            status=client.call('status')
            if not distributed.eligible(status) or status.get('machine')!=host['machine']:raise distributed.CapacityWait('Worker is updating or declining new work')
            acquired=client.call('resource.acquire',request)['acquired']
        client.call('resource.renew',{'id':lease})
        def renew():
            other=AgentClient(host)
            try:
                while not done.wait(30):
                    try:other.call('resource.renew',{'id':lease},timeout=10)
                    except Exception:
                        errors.append('Resource renewal failed');subprocess.run(distributed.shell_command(host,['docker','rm','-f',name]),capture_output=True,timeout=20);return
            finally:other.close()
        threading.Thread(target=renew,daemon=True).start()
        def cancel():
            while not done.wait(1):
                failed=False
                if download_state:
                    try:
                        source_row=json.loads(Path(download_state).read_text()).get('jobs',{}).get(ident,{})
                        failed=source_row.get('status')=='failed' or source_row.get('host') not in (None,host['host'])
                        if failed:errors.append('Source download failed or cache assignment changed')
                    except (OSError,ValueError):pass
                if STOP.is_set() or failed:
                    subprocess.run(distributed.shell_command(host,['docker','rm','-f',name]),capture_output=True,timeout=20);return
        threading.Thread(target=cancel,daemon=True).start()
        payload={'item':item,'folder':'/input/'+ident,'chunk_bytes':CHUNK_BYTES}
        gpu_mount=[]
        if gpu_enabled:
            gpu_root=str(Path(host['cache']).parent/'gpu-qc')
            subprocess.run(distributed.shell_command(host,['mkdir','-p','--',gpu_root+'/batches']),check=True,timeout=20)
            payload['gpu']={'local_root':'/gpu-qc','host_root':gpu_root,'verify':gpu_verify,'verify_first':True}
            payload['gpu_parallel']=True
            gpu_mount=['--mount','type=bind,src='+gpu_root+',dst=/gpu-qc']
        worker=Path(__file__).read_text()
        argv=['docker','run','--rm','--pull=never','--name',name,'--label','org.lifetech.genomics=true','--label','org.lifetech.public-wgs-qc=true','--network','none','--read-only','--log-driver','none','--cpus','1','--memory',str(memory)+'m','--memory-swap',str(memory)+'m','--pids-limit','32','--cap-drop','ALL','--security-opt','no-new-privileges','--ulimit','core=0:0','--user',host.get('container_user','1000:1000'),'--mount','type=bind,src='+host['cache']+',dst=/input,readonly',*gpu_mount,'-i',host['image'],'python','-c',worker,'--worker']
        process=subprocess.Popen(distributed.shell_command(host,argv),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1)
        process.stdin.write(json.dumps(payload));process.stdin.close()
        last={}
        for line in process.stdout:
            last=json.loads(line);value={**last,'sample':item['sample'],'run':item['run'],'filename':item['filename'],'host':host['host'],'memory_mb':memory};atomic_json(report_path,value)
            if changed:changed(value)
        code=process.wait();error=process.stderr.read()
        if errors and 'Source download failed or cache assignment changed' in errors:raise InterruptedError('Source input moved or is not currently available')
        if code or errors:raise RuntimeError('Streaming worker failed ('+str(code)+'): '+(error[-300:] or str(errors)))
        if last.get('phase')!='verified' or last.get('provisional') is not False:raise ValueError('QC did not produce a verified final result')
        return last
    finally:
        done.set()
        try:release_after_cleanup(host,name,client,lease)
        finally:client.close()


def integrity_hold_active(host, ident, item, hold):
    """Rearm a quarantined input only after its byte identity changes or MD5 verifies."""
    from scripts import genomics_distributed as distributed
    probe="""import json,pathlib,sys
p=pathlib.Path(sys.argv[1]); out={}
for name in ['chunks/0.json','receipt.json']:
 try:out[name]=json.loads((p/name).read_text())
 except (OSError,ValueError):pass
print(json.dumps(out))
"""
    result=subprocess.run(distributed.shell_command(host,['python3','-c',probe,str(Path(host['cache'])/ident)]),capture_output=True,text=True,timeout=15)
    if result.returncode:return True
    try:data=json.loads(result.stdout)
    except ValueError:return True
    receipt=data.get('receipt.json',{})
    if receipt.get('md5')==item['md5'] and receipt.get('bytes')==item['bytes'] and receipt.get('url')==item['url']:return False
    sha=data.get('chunks/0.json',{}).get('sha256')
    return not sha or sha==hold['chunk0_sha256']


def coordinator(args):
    from concurrent.futures import ThreadPoolExecutor
    from scripts.wgs_swarm_download import atomic_json,file_id
    from scripts.genomics_distributed import CapacityWait
    catalog=json.loads(args.manifest.read_text());items={file_id(item):item for item in catalog['files'] if not args.sample or item['sample'] in args.sample};hosts={host['host']:host for host in json.loads(args.hosts.read_text())}
    args.output=args.output.absolute()
    if any((p/'.git').exists() or p.is_symlink() for p in [args.output,*args.output.parents]):raise ValueError('Private QC state must be outside Git and symlinks')
    args.output.mkdir(parents=True,exist_ok=True,mode=0o700);state={'status':'running','jobs':{}};lock=threading.RLock()
    for ident,item in items.items():
        path=args.output/(ident+'.json')
        if path.exists():
            previous=json.loads(path.read_text())
            if previous.get('phase')=='verified' and previous.get('input_md5')==item['md5']:state['jobs'][ident]={**previous,'status':'verified'}
    def save():state['updated']=time.time();atomic_json(args.output/'state.json',state)
    with lock:save()
    def worker(host,lane):
        hold_checks={}
        while not STOP.is_set():
            downloads=json.loads(args.download_state.read_text());eligible=[]
            cache=next((row['cache'] for row in downloads.get('workers',[]) if row['host']==host['host']),host['cache']);host={**host,'cache':cache}
            for ident,row in downloads.get('jobs',{}).items():
                if ident not in items or row['host']!=host['host'] or (row['sample']=='Venter')!=(lane=='venter'):continue
                with lock:previous=state['jobs'].get(ident,{})
                if previous.get('status')=='verified':continue
                try:holds=json.loads((args.output/'integrity-holds.json').read_text())
                except (OSError,ValueError):holds={}
                hold=holds.get(ident)
                if hold:
                    checked,active=hold_checks.get(ident,(0,True))
                    if time.monotonic()-checked>=30:
                        active=integrity_hold_active(host,ident,items[ident],hold);hold_checks[ident]=(time.monotonic(),active)
                    if active:
                        with lock:state['jobs'][ident]={'status':'waiting_input_integrity','host':host['host'],'sample':row['sample'],'run':row['run'],'provisional':True,'chunk0_sha256':hold['chunk0_sha256'],'reason':hold.get('reason','Input integrity investigation')};save()
                        continue
                if previous.get('host') not in (None,row['host']):
                    with lock:state['jobs'][ident]={'status':'waiting_for_input','host':row['host'],'sample':row['sample'],'run':row['run'],'provisional':True};save()
                if row['status'] in ('downloading','verified'):eligible.append((row['status']!='verified',ident))
            for _,ident in sorted(eligible):
                if STOP.is_set():break
                item=items[ident]
                def changed(value):
                    with lock:state['jobs'][ident]={**value,'status':value['phase']};save()
                with lock:state['jobs'][ident]={'status':'waiting_capacity','host':host['host'],'sample':item['sample'],'run':item['run'],'provisional':True};save()
                try:
                    result=run_remote(host,item,ident,args.output,0,changed,args.download_state)
                    with lock:state['jobs'][ident]={**state['jobs'][ident],**result,'status':'verified'};save()
                    print('QC verified '+item['filename']+' on '+host['host'],flush=True)
                except CapacityWait:break
                except InterruptedError:
                    with lock:state['jobs'][ident]={'status':'stopped' if STOP.is_set() else 'waiting_for_input','host':host['host'],'sample':item['sample'],'run':item['run'],'provisional':True};save()
                except Exception as error:
                    with lock:state['jobs'][ident]={'status':'failed','host':host['host'],'sample':item['sample'],'run':item['run'],'error':type(error).__name__+': '+str(error)[:300],'provisional':True};save()
            with lock:
                if len(state['jobs'])==len(items) and all(row['status']=='verified' for row in state['jobs'].values()):state['status']='passed';save();STOP.set()
            STOP.wait(2)
    try:
        with ThreadPoolExecutor(max_workers=len(hosts)*2) as pool:list(pool.map(lambda pair:worker(*pair),[(host,lane) for host in hosts.values() for lane in ('watson','venter')]))
    finally:
        with lock:
            if state['status']!='passed':
                state['status']='stopped'
                for row in state['jobs'].values():
                    if row.get('status') not in ('verified','failed'):row['status']='stopped'
                save()


def gpu_backfill(args):
    """Continuously enrich already verified miniPC archives without replacing prior QC."""
    import fcntl
    from scripts.wgs_swarm_download import atomic_json,file_id
    from scripts.genomics_distributed import CapacityWait
    args.output=args.output.absolute();args.output.mkdir(mode=0o700,parents=True,exist_ok=True)
    if any((p/'.git').exists() or p.is_symlink() for p in [args.output,*args.output.parents]):raise ValueError('Backfill state must remain private outside Git')
    lock=(args.output/'backfill.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    catalog={file_id(item):item for item in json.loads(args.manifest.read_text())['files'] if item['sample']=='Watson'}
    host=next(h for h in json.loads(args.hosts.read_text()) if h['host']==args.gpu_host)
    state_path=args.output/'state.json'
    try:state=json.loads(state_path.read_text())
    except (FileNotFoundError,ValueError):state={'jobs':{}}
    state.update(status='running',scope='GPU histogram backfill of publisher-verified public source reads; separate from primary QC and scientific publication',provisional=True)
    for ident,row in state['jobs'].items():
        if ident in catalog:row.update(sample='Watson',run=catalog[ident]['run'],filename=catalog[ident]['filename'],host=host['host'])
        if row.get('phase')=='verified' and row.get('gpu_bases')==row.get('bases') and row.get('matches_original_cpu_qc'):
            row.update(status='verified',validation='native_cpu_every_bin' if row.get('gpu_cpu_crosschecked_batches')==row.get('gpu_batches') else 'prior_aggregate_and_startup_fixtures')
    def save():state['updated']=time.time();atomic_json(state_path,state)
    save()
    try:
        while not STOP.is_set():
            downloads=json.loads(args.download_state.read_text());host={**host,'cache':next((w['cache'] for w in downloads.get('workers',[]) if w['host']==host['host']),host['cache'])}
            eligible=[]
            for ident,source in downloads.get('jobs',{}).items():
                if ident not in catalog or source.get('host')!=host['host'] or source.get('status')!='verified':continue
                previous=state['jobs'].get(ident,{})
                if previous.get('retry_after',0)>time.time():continue
                if previous.get('status')=='verified' and previous.get('input_md5')==catalog[ident]['md5']:continue
                try:baseline=json.loads((args.qc_baseline/(ident+'.json')).read_text())
                except (FileNotFoundError,ValueError):continue
                if baseline.get('phase')=='verified' and baseline.get('input_md5')==catalog[ident]['md5']:eligible.append((catalog[ident]['bytes'],ident,baseline))
            for _,ident,baseline in sorted(eligible):
                if STOP.is_set():break
                item=catalog[ident];metadata={'sample':item['sample'],'run':item['run'],'filename':item['filename'],'host':host['host']}
                def changed(row):state['jobs'][ident]={**row,**metadata,'status':row['phase']};save()
                try:
                    result=run_remote(host,item,ident,args.output,0,changed,args.download_state)
                    validate_backfill(result,baseline)
                    if result['gpu_bases']!=result['bases']:
                        state['jobs'][ident]={**result,**metadata,'status':'waiting_gpu','provisional':True,'reason':'CPU fallback preserved counts; awaiting full GPU histogram enrichment','retry_after':time.time()+60};save();break
                    result.update(**metadata,status='verified',matches_prior_qc=True,matches_original_cpu_qc=baseline.get('qc_backend','cpu')=='cpu',gpu_batch_cpu_crosscheck_requested=False,validation='prior_aggregate_and_startup_fixtures')
                    state['jobs'][ident]=result;save()
                except CapacityWait:
                    state['jobs'][ident]={**metadata,'status':'waiting_capacity','provisional':True};save();break
                except InterruptedError:
                    state['jobs'][ident]={**metadata,'status':'stopped' if STOP.is_set() else 'waiting_input','provisional':True};save();break
                except Exception as error:
                    state['jobs'][ident]={**metadata,'status':'waiting_or_failed','provisional':True,'retry_after':time.time()+60,'error':type(error).__name__+': '+str(error)[:240]};save();break
            state['status']='running' if eligible else 'watching_for_verified_sources';save();STOP.wait(5)
    finally:
        state['status']='stopped';save();lock.close()


def validate_backfill(result,baseline):
    """A new histogram never overrides disagreement with original source QC."""
    keys=('reads','bases','gc_bases','ambiguous_bases','quality_sum','q20_bases','q30_bases','min_length','max_length','input_md5')
    if result.get('phase')!='verified' or result.get('provisional') is not False or any(result.get(k)!=baseline.get(k) for k in keys):raise ValueError('GPU histogram backfill differs from prior verified QC')
    if len(result.get('quality_histogram',[]))!=94 or sum(result['quality_histogram'])!=result['bases']:raise ValueError('Backfill histogram does not cover all source bases')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--worker',action='store_true');parser.add_argument('--gpu-backfill',action='store_true');parser.add_argument('--gpu-host',default='julian-minipc.local');parser.add_argument('--qc-baseline',type=Path);parser.add_argument('--sample',action='append');parser.add_argument('--manifest',type=Path);parser.add_argument('--hosts',type=Path);parser.add_argument('--download-state',type=Path);parser.add_argument('--output',type=Path)
    args=parser.parse_args();os.umask(0o077)
    if args.worker:
        import sys
        job=json.load(sys.stdin)
        result=aggregate_qc(job['folder'],job['item'],job.get('chunk_bytes',CHUNK_BYTES),progress=lambda value:print(json.dumps(value),flush=True),gpu=job.get('gpu'),batch_bases=job.get('batch_bases',BATCH_BASES),gpu_parallel=job.get('gpu_parallel',False))
        print(json.dumps(result),flush=True);return
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:STOP.set())
    if any(value is None for value in (args.manifest,args.hosts,args.download_state,args.output)):parser.error('Coordinator requires manifest, hosts, download-state and output')
    if args.gpu_backfill:
        if args.qc_baseline is None:parser.error('GPU backfill requires --qc-baseline')
        gpu_backfill(args);return
    coordinator(args)


if __name__=='__main__':main()
