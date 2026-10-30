"""Single-copy resumable public-archive transfer for space-constrained peers.

Uses the signed WGS runner's container, lease, identity and receipt contract.
An incomplete or checksum-failed file is never published as a verified FASTQ.
"""
import datetime
import concurrent.futures
import threading
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
from urllib.parse import urlsplit
from urllib.request import Request,urlopen
from urllib.error import HTTPError


class ArchivePayloadError(OSError):
    """A transient archive error body; the uncommitted range must retry."""
    pass


def save(path,value):
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value));temporary.chmod(0o600);temporary.replace(path)


def check_response(response,start,total,allowed_hosts,end=None):
    if urlsplit(response.geturl()).hostname not in allowed_hosts:raise ValueError('Unexpected archive redirect')
    end=total-1 if end is None else end
    expected=f'bytes {start}-{end}/{total}'
    if response.status!=206 or response.headers.get('Content-Range')!=expected:
        raise ValueError('Archive did not honor the exact resume range')
    if response.headers.get('Content-Encoding','identity')!='identity':raise ValueError('Unexpected content encoding')
    if int(response.headers.get('Content-Length',-1))!=end-start+1:raise ValueError('Archive range length mismatch')


def transfer(current,cache_root,opener=urlopen,sleep=time.sleep):
    item=current['item'];ident=current['id'];allowed=current['allowed_hosts']
    if urlsplit(item['url']).scheme not in ('https','http') or urlsplit(item['url']).hostname not in allowed:
        raise ValueError('Unexpected public archive URL')
    folder=cache_root/ident;folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    lock=(folder/'download.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    final=folder/item['filename'];partial=folder/(item['filename']+'.part');receipt_path=folder/'receipt.json'
    progress_path=folder/'progress.json';started=time.monotonic();last_save=0
    if final.exists():
        if not receipt_path.exists():raise ValueError('Existing final file has no verified receipt')
        receipt=json.loads(receipt_path.read_text())
        if receipt.get('url')!=item['url'] or receipt.get('md5')!=item['md5'] or final.stat().st_size!=item['bytes'] or receipt.get('mtime_ns')!=final.stat().st_mtime_ns:
            raise ValueError('Existing final file identity mismatch')
        return receipt
    position=partial.stat().st_size if partial.exists() else 0
    if position>item['bytes']:raise ValueError('Partial file exceeds publisher size')
    if shutil.disk_usage(folder).free<item['bytes']-position+1024**3:raise ValueError('Insufficient space for remaining single-copy transfer')
    def progress(phase,error=None):
        value={'phase':phase,'bytes_completed':position,'bytes_received':position,'bytes_total':item['bytes'],
               'updated':time.time(),'storage_mode':'single-copy-resumable','elapsed_seconds':round(time.monotonic()-started,1)}
        if error:value['last_error']=error
        save(progress_path,value)
    progress('downloading')
    attempt=0
    while position<item['bytes']:
        try:
            request=Request(item['url'],headers={'Range':f"bytes={position}-{item['bytes']-1}",'Accept-Encoding':'identity','User-Agent':'LifeTech-Swarm-Public-WGS/1'})
            with opener(request,timeout=60) as response,partial.open('ab',buffering=0) as output:
                check_response(response,position,item['bytes'],allowed)
                tail=b''
                while position<item['bytes']:
                    data=response.read(min(1024*1024,item['bytes']-position))
                    if not data:raise OSError('Archive connection ended before the requested range')
                    if b'<Error><Code>' in tail+data:raise ArchivePayloadError('Archive error payload detected')
                    if position==0 and not data.startswith(b'\x1f\x8b'):raise ValueError('Archive source is not gzip')
                    tail=data[-32:];output.write(data);position+=len(data)
                    if time.monotonic()-last_save>=5:
                        os.fsync(output.fileno());progress('downloading');last_save=time.monotonic()
                os.fsync(output.fileno())
        except (OSError,HTTPError) as error:
            attempt+=1
            if isinstance(error,HTTPError) and error.code not in (408,429,500,502,503,504):raise
            if attempt>=20:raise
            progress('retrying',type(error).__name__);sleep(min(2**attempt,120))
    progress('verifying')
    md5=hashlib.md5();sha256=hashlib.sha256()
    with partial.open('rb') as stream:
        for block in iter(lambda:stream.read(8*1024*1024),b''):md5.update(block);sha256.update(block)
    if partial.stat().st_size!=item['bytes'] or md5.hexdigest().lower()!=item['md5'].lower():
        progress('failed','Publisher checksum mismatch; partial retained for investigation')
        raise ValueError('Publisher checksum verification failed')
    partial.replace(final);final.chmod(0o600)
    receipt={**item,'md5':md5.hexdigest(),'sha256':sha256.hexdigest(),'mtime_ns':final.stat().st_mtime_ns,
             'verified_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'elapsed_seconds':round(time.monotonic()-started,3),
             'cache_relative_path':str(final.relative_to(cache_root)),'storage_mode':'single-copy-resumable'}
    save(receipt_path,receipt);progress('verified');lock.close();return receipt


def parallel_transfer(current,cache_root,connections,opener=urlopen,sleep=time.sleep,chunk_bytes=128*1024*1024):
    """Write independent, hashed HTTP ranges into one sparse partial file."""
    item=current['item'];ident=current['id'];allowed=current['allowed_hosts']
    folder=cache_root/ident;folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    final=folder/item['filename'];partial=folder/(item['filename']+'.part')
    if final.exists():return transfer(current,cache_root,opener,sleep)
    lock=(folder/'download.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    checkpoint=folder/'ranges.json';progress_path=folder/'progress.json';guard=threading.RLock();abort=threading.Event()
    total=item['bytes'];count=(total+chunk_bytes-1)//chunk_bytes;active={};started=time.monotonic()
    manifest={'url':item['url'],'bytes':total,'md5':item['md5'],'chunk_bytes':chunk_bytes,'done':{}}
    if checkpoint.exists():
        previous=json.loads(checkpoint.read_text())
        if any(previous.get(k)!=manifest[k] for k in ['url','bytes','md5','chunk_bytes']):raise ValueError('Resume range identity mismatch')
        manifest=previous
    fd=os.open(partial,os.O_RDWR|os.O_CREAT,0o600)
    original_size=os.fstat(fd).st_size
    def size(index):return min(chunk_bytes,total-index*chunk_bytes)
    def progress(phase):
        with guard:
            completed=sum(size(int(index)) for index in manifest['done'])
            save(progress_path,{'phase':phase,'bytes_completed':completed,'bytes_received':completed+sum(active.values()),
                'bytes_total':total,'completed_chunks':len(manifest['done']),'total_chunks':count,'connections':connections,
                'updated':time.time(),'storage_mode':'single-copy-parallel-ranges','elapsed_seconds':round(time.monotonic()-started,1)})
    heartbeat=[0.0]
    def digest_range(index):
        digest=hashlib.sha256();offset=index*chunk_bytes;remaining=size(index)
        while remaining:
            block=os.pread(fd,min(1024*1024,remaining),offset)
            if not block:raise ValueError('Completed range is missing from partial file')
            digest.update(block);offset+=len(block);remaining-=len(block)
            if time.monotonic()-heartbeat[0]>5:
                progress('validating_saved_ranges');heartbeat[0]=time.monotonic()
        return digest.hexdigest()
    try:
        if original_size>total:raise ValueError('Partial file exceeds publisher size')
        if not checkpoint.exists():
            # Import only whole ranges from our previous sequential prefix.
            for index in range(original_size//chunk_bytes):manifest['done'][str(index)]=digest_range(index)
        else:
            for key,digest in list(manifest['done'].items()):
                index=int(key)
                if index<0 or index>=count or digest_range(index)!=digest:raise ValueError('Cached range failed integrity validation')
        completed=sum(size(int(index)) for index in manifest['done'])
        if shutil.disk_usage(folder).free<total-completed+1024**3:raise ValueError('Insufficient remaining single-copy storage')
        os.ftruncate(fd,total);save(checkpoint,manifest)
        def fetch(index):
            start=index*chunk_bytes;end=start+size(index)-1
            for attempt in range(20):
                if abort.is_set():return
                digest=hashlib.sha256();received=0;tail=b''
                try:
                    request=Request(item['url'],headers={'Range':f'bytes={start}-{end}','Accept-Encoding':'identity','User-Agent':'LifeTech-Swarm-Public-WGS/1'})
                    with opener(request,timeout=60) as response:
                        check_response(response,start,total,allowed,end)
                        while received<size(index):
                            if abort.is_set():return
                            data=response.read(min(1024*1024,size(index)-received))
                            if not data:raise OSError('Archive range ended early')
                            if b'<Error><Code>' in tail+data:raise ArchivePayloadError('Archive error payload detected')
                            if start==0 and received==0 and not data.startswith(b'\x1f\x8b'):raise ValueError('Archive source is not gzip')
                            tail=data[-32:];written=os.pwrite(fd,data,start+received)
                            if written!=len(data):raise OSError('Short partial-file write')
                            digest.update(data);received+=len(data)
                            with guard:active[index]=received
                    os.fsync(fd)
                    with guard:
                        manifest['done'][str(index)]=digest.hexdigest();active.pop(index,None);save(checkpoint,manifest)
                    return
                except (OSError,HTTPError) as error:
                    if isinstance(error,HTTPError) and error.code not in (408,429,500,502,503,504):raise
                    if attempt==19:raise
                    sleep(min(2**(attempt+1),120))
        progress('downloading')
        with concurrent.futures.ThreadPoolExecutor(max_workers=connections) as pool:
            pending={pool.submit(fetch,i) for i in range(count) if str(i) not in manifest['done']}
            try:
                while pending:
                    finished,pending=concurrent.futures.wait(pending,timeout=5,return_when=concurrent.futures.FIRST_EXCEPTION)
                    for future in finished:future.result()
                    progress('downloading')
            except BaseException:
                abort.set()
                for future in pending:future.cancel()
                raise
        if len(manifest['done'])!=count:raise ValueError('Not all source ranges completed')
        progress('verifying');md5=hashlib.md5();sha256=hashlib.sha256()
        with partial.open('rb') as stream:
            for block in iter(lambda:stream.read(8*1024*1024),b''):md5.update(block);sha256.update(block)
        if md5.hexdigest().lower()!=item['md5'].lower():raise ValueError('Publisher checksum verification failed')
        os.close(fd);fd=None;partial.replace(final);final.chmod(0o600)
        receipt={**item,'sha256':sha256.hexdigest(),'md5':md5.hexdigest(),'mtime_ns':final.stat().st_mtime_ns,
                 'verified_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'elapsed_seconds':round(time.monotonic()-started,3),
                 'cache_relative_path':str(final.relative_to(cache_root)),'storage_mode':'single-copy-parallel-ranges'}
        save(folder/'receipt.json',receipt);progress('verified');return receipt
    finally:
        if fd is not None:os.close(fd)
        lock.close()


def retryable_download_error(error):
    if isinstance(error,HTTPError):return error.code in (408,429,500,502,503,504)
    return isinstance(error,OSError) and not isinstance(error,PermissionError)


def main():
    os.umask(0o077);job=json.load(sys.stdin);root=Path('/cache')
    for current in job['jobs']:
        try:print(json.dumps(parallel_transfer(current,root,min(8,max(1,job.get('connections',1))))),flush=True)
        except Exception as error:
            folder=root/current['id'];folder.mkdir(mode=0o700,parents=True,exist_ok=True)
            old=json.loads((folder/'progress.json').read_text()) if (folder/'progress.json').exists() else {}
            save(folder/'progress.json',{**old,'phase':'failed','updated':time.time(),'last_error':type(error).__name__+': '+str(error)[:300]})
            print(json.dumps({'id':current['id'],'error':type(error).__name__+': '+str(error)[:300],'retryable':retryable_download_error(error)}),flush=True);raise


if __name__=='__main__':main()
