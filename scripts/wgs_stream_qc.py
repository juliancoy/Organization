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
MAPPING_HOSTS={'compai.local','codecollective.local','julian-latitude.local','julian-inspiron-3793.local'}


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


def aggregate_qc(folder,item,chunk_bytes=CHUNK_BYTES,progress=None,stop=None):
    """Four-line ENA FASTQ, Phred+33; sequences and identifiers never leave the parser."""
    progress=progress or (lambda value:None);stop=stop or STOP;last=[0.];started=time.monotonic()
    counts={'reads':0,'bases':0,'gc_bases':0,'ambiguous_bases':0,'quality_sum':0,'q20_bases':0,'q30_bases':0,'min_length':None,'max_length':0}
    reader=None
    def report(phase,force=False):
        now=time.monotonic()
        if force or now-last[0]>=2:
            progress({'phase':phase,'provisional':phase!='verified','updated':time.time(),'elapsed_seconds':round(now-started,2),'compressed_bytes_read':reader.offset if reader else 0,'compressed_bytes_total':item['bytes'],'verified_chunks':reader.verified_chunks if reader else 0,**counts});last[0]=now
    reader=OrderedChunks(folder,item,chunk_bytes,waiting=lambda:report('waiting_for_complete_chunk'),stop=stop)
    q20=bytes(1 if value>=53 else 0 for value in range(256));q30=bytes(1 if value>=63 else 0 for value in range(256))
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
                sequence=sequence.upper();length=len(sequence)
                counts['reads']+=1;counts['bases']+=length;counts['gc_bases']+=sequence.count(b'G')+sequence.count(b'C');counts['ambiguous_bases']+=length-sum(sequence.count(base) for base in (b'A',b'C',b'G',b'T'))
                counts['quality_sum']+=sum(quality)-33*length;counts['q20_bases']+=sum(quality.translate(q20));counts['q30_bases']+=sum(quality.translate(q30));counts['max_length']=max(counts['max_length'],length);counts['min_length']=length if counts['min_length'] is None else min(counts['min_length'],length)
                report('streaming')
            if reader.offset!=item['bytes'] or reader.md5.hexdigest()!=item['md5']:raise ValueError('Full streaming input failed publisher MD5')
            if counts['reads']!=int(item['read_count']):raise ValueError('FASTQ read count differs from ENA run metadata')
            while reader.receipt() is None:
                if stop.is_set():raise InterruptedError('Streaming QC stopped')
                report('awaiting_publisher_receipt');stop.wait(.2)
        report('verified',True)
        return {**counts,'phase':'verified','provisional':False,'updated':time.time(),'compressed_bytes_read':item['bytes'],'compressed_bytes_total':item['bytes'],'verified_chunks':reader.verified_chunks,'input_md5':item['md5'],'expected_reads':int(item['read_count']),'gc_percent':100*counts['gc_bases']/counts['bases'] if counts['bases'] else None,'mean_phred':counts['quality_sum']/counts['bases'] if counts['bases'] else None,'scope':'Aggregate source-read QC only, not alignment, variant calls, reference matches or reconstruction of withheld sequence.'}
    finally:reader.close()


def qc_memory_mb(chunk_bytes=CHUNK_BYTES,rss_kib=None):
    """Two chunk buffers + measured coordinator runtime + bounded parser buffers."""
    rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if rss_kib is None else rss_kib
    return math.ceil((2*chunk_bytes+rss_kib*1024+4*LINE_BYTES)/(1024*1024))


def run_remote(host,item,ident,output,mapping_reserve_mb=7000,changed=None,download_state=None):
    from scripts import genomics_distributed as distributed
    from scripts.wgs_swarm_download import AgentClient,atomic_json
    name='genomics-'+uuid.uuid4().hex;lease=uuid.uuid4().hex;client=AgentClient(host);done=threading.Event();process=None;errors=[]
    memory=qc_memory_mb(min(CHUNK_BYTES,item['bytes']));report_path=output/(ident+'.json')
    try:
        status=client.call('status')
        if status.get('machine')!=host['machine'] or not distributed.eligible(status) or 'renewable-resources' not in status.get('capabilities',[]):raise RuntimeError('Worker identity, policy or renewable leases unavailable')
        with HOST_LOCK_GUARD:guard=HOST_LOCKS.setdefault(host['host'],threading.Lock())
        with guard:
            capacity=client.call('resource.capacity',{'group':GROUP})
            if capacity['available_mb']-mapping_reserve_mb<memory or capacity.get('available_cpus',0)<1:raise distributed.CapacityWait('Preserving mapping CPU/RAM headroom')
            if not client.call('resource.acquire',{'id':lease,'group':GROUP,'cpus':1,'memory_mb':memory,'container':name})['acquired']:raise distributed.CapacityWait('QC capacity changed')
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
        worker=Path(__file__).read_text()
        argv=['docker','run','--rm','--pull=never','--name',name,'--label','org.lifetech.genomics=true','--label','org.lifetech.public-wgs-qc=true','--network','none','--read-only','--log-driver','none','--cpus','1','--memory',str(memory)+'m','--memory-swap',str(memory)+'m','--pids-limit','32','--cap-drop','ALL','--security-opt','no-new-privileges','--ulimit','core=0:0','--user',host.get('container_user','1000:1000'),'--mount','type=bind,src='+host['cache']+',dst=/input,readonly','-i',host['image'],'python','-c',worker,'--worker']
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
        if process is not None:
            try:subprocess.run(distributed.shell_command(host,['docker','rm','-f',name]),capture_output=True,timeout=20)
            except (OSError,subprocess.TimeoutExpired):pass
        try:client.call('resource.release',{'id':lease})
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
                    result=run_remote(host,item,ident,args.output,7000 if host['host'] in MAPPING_HOSTS else 0,changed,args.download_state)
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


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--worker',action='store_true');parser.add_argument('--sample',action='append');parser.add_argument('--manifest',type=Path);parser.add_argument('--hosts',type=Path);parser.add_argument('--download-state',type=Path);parser.add_argument('--output',type=Path)
    args=parser.parse_args();os.umask(0o077)
    if args.worker:
        import sys
        job=json.load(sys.stdin)
        result=aggregate_qc(job['folder'],job['item'],job.get('chunk_bytes',CHUNK_BYTES),progress=lambda value:print(json.dumps(value),flush=True))
        print(json.dumps(result),flush=True);return
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:STOP.set())
    if any(value is None for value in (args.manifest,args.hosts,args.download_state,args.output)):parser.error('Coordinator requires manifest, hosts, download-state and output')
    coordinator(args)


if __name__=='__main__':main()
