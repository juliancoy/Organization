"""Owner-only sequential socket adapter for the persistent Vulkan QC engine."""
import fcntl,json,os,pathlib,re,socket,stat,struct,subprocess,time
ROOT=pathlib.Path(os.environ.get('GPU_QC_ROOT','/gpu-qc'));HOST_ROOT=pathlib.Path(os.environ.get('GPU_QC_HOST_ROOT',str(ROOT)))
MAX_BATCH=int(os.environ.get('GPU_QC_MAX_BATCH','16777216'));VERSION=os.environ.get('GPU_QC_VERSION','unknown')


def read_batch(path):
    candidate=pathlib.Path(path)
    try:relative=candidate.relative_to(HOST_ROOT/'batches')
    except ValueError:raise ValueError('Batch outside private batch directory')
    local=ROOT/'batches'/relative
    if '..' in relative.parts or any(p.is_symlink() for p in [local,*local.parents]):raise ValueError('Symlink or parent traversal rejected')
    fd=os.open(local,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:raise ValueError('Batch must be an owner-only regular file')
        if not 0<info.st_size<=MAX_BATCH:raise ValueError('Batch size outside admitted bounds')
        with os.fdopen(fd,'rb',closefd=False) as stream:data=stream.read(MAX_BATCH+1)
        if len(data)!=info.st_size:raise ValueError('Batch changed during read')
        return data
    finally:os.close(fd)


def validate_aggregates(row):
    n=row['bases'];hist=row['quality_histogram'];counts=row['counts']
    if row['software_fallback'] or row['device_type'] not in (1,2):raise RuntimeError('Software Vulkan device rejected')
    if row['invalid_quality']:raise ValueError('Quality outside Phred+33 range 0..93')
    if len(hist)!=94 or sum(hist)!=n or sum(counts.values())!=n:raise RuntimeError('GPU aggregate length mismatch')
    if sum(i*v for i,v in enumerate(hist))!=row['quality_sum'] or sum(hist[20:])!=row['q20'] or sum(hist[30:])!=row['q30']:raise RuntimeError('GPU quality aggregate mismatch')
    return row


def main():
    ROOT.mkdir(parents=True,exist_ok=True,mode=0o700);(ROOT/'batches').mkdir(exist_ok=True,mode=0o700);os.umask(0o077)
    lock=(ROOT/'service.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    engine=subprocess.Popen(['./qc_engine','qc_engine.spv',str(MAX_BATCH)],stdin=subprocess.PIPE,stdout=subprocess.PIPE)
    def execute(sequence,quality,verify):
        if len(sequence)!=len(quality):raise ValueError('Sequence/quality lengths differ')
        engine.stdin.write(struct.pack('<4sII',b'GPQ1',len(sequence),int(verify)));engine.stdin.write(sequence);engine.stdin.write(quality);engine.stdin.flush()
        line=engine.stdout.readline()
        if not line:raise RuntimeError('Persistent Vulkan engine stopped')
        return validate_aggregates(json.loads(line))
    try:prior=json.loads((ROOT/'daemon.json').read_text())
    except (OSError,ValueError):prior={}
    state={'status':'starting','runtime_version':VERSION,'batches':max(0,int(prior.get('batches',0))),'bases':max(0,int(prior.get('bases',0))),'startup_fixtures':0}
    def save():
        state['updated']=time.time();tmp=ROOT/'daemon.json.tmp';tmp.write_text(json.dumps(state));tmp.replace(ROOT/'daemon.json')
    try:
        for n in (1,97,1025,4097):
            seq=(b'ACGTNacgtnRY-'*((n+12)//13))[:n];qual=bytes(33+i%94 for i in range(n));row=execute(seq,qual,True)
            assert row['exact_match'] is True;state['startup_fixtures']+=1
        state.update(status='running',device=row['device'],device_type=row['device_type'],max_batch_bytes=MAX_BATCH);save()
        path=ROOT/'service.sock';path.unlink(missing_ok=True);server=socket.socket(socket.AF_UNIX);server.bind(str(path));os.chmod(path,0o600);server.listen(8)
        while True:
            connection,_=server.accept()
            with connection:
                connection.settimeout(30);request_id=None
                try:
                    _,uid,_=struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                    if uid!=os.getuid():raise ValueError('Socket client owner mismatch')
                    stream=connection.makefile('rb');line=stream.readline(8193)
                    if len(line)>8192 or not line.endswith(b'\n'):raise ValueError('Invalid request framing')
                    request=json.loads(line)
                    if not isinstance(request,dict):raise ValueError('Request must be an object')
                    request_id=request.get('request_id')
                    if request.get('version')!=1 or not isinstance(request_id,str) or not re.fullmatch('[a-fA-F0-9]{1,64}',request_id):raise ValueError('Unsupported version or request ID')
                    if request.get('op')=='status':response={**state,'version':1,'request_id':request_id}
                    else:
                        started=time.monotonic();seq=read_batch(request['sequence_path']);qual=read_batch(request['quality_path']);row=execute(seq,qual,request.get('verify') is True)
                        row['timings']['engine_ms']=row['timings']['request_ms'];row['timings']['request_ms']=(time.monotonic()-started)*1000
                        response={**row,'version':1,'request_id':request_id,'backend':'vulkan','validated_backend':True};state['batches']+=1;state['bases']+=row['bases'];save()
                except (ValueError,KeyError,TypeError,OSError) as error:response={'version':1,'request_id':request_id,'error':str(error)}
                try:connection.sendall(json.dumps(response).encode()+b'\n')
                except OSError:pass
    finally:
        engine.terminate();engine.wait(timeout=10);(ROOT/'service.sock').unlink(missing_ok=True);state['status']='offline';save()


if __name__=='__main__':main()
