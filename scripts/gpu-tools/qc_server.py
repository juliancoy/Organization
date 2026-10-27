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
    if len(hist)!=94 or set(counts)!={'A','C','G','T','N','other'} or any(type(v)is not int or v<0 for v in [*hist,*counts.values()]) or sum(hist)!=n or sum(counts.values())!=n:raise RuntimeError('GPU aggregate length mismatch')
    if sum(i*v for i,v in enumerate(hist))!=row['quality_sum'] or sum(hist[20:])!=row['q20'] or sum(hist[30:])!=row['q30']:raise RuntimeError('GPU quality aggregate mismatch')
    return row


def mask_oracle(words):
    counts=[0]*69
    for word in words:
        states=[]
        for person in range(6):
            bits=(word>>(4*person))&15;reference=bool(bits&1);span=bool(bits&2);unknown=bool(bits&4);start=bool(bits&8)
            value=0
            if not word&0x80000000 and not unknown and not(reference and span):
                if span and start:value=2
                elif reference and not span and not start:value=1
            states.append(value);counts[(0 if value==1 else 6 if value==2 else 12)+person]+=1;counts[18+person]+=span
        pair=0
        for left in range(6):
            for right in range(left+1,6):
                if states[left] and states[right]:
                    counts[24+pair*3]+=1;counts[25+pair*3+(states[left]!=states[right])]+=1
                pair+=1
    return counts


def validate_mask(row):
    n=row['positions'];counts=row['mask_counts']
    if row['software_fallback'] or row['device_type'] not in (1,2) or len(counts)!=69:raise RuntimeError('Invalid hardware mask output')
    if any(type(x)is not int or not 0<=x<=n for x in counts):raise RuntimeError('Invalid mask counter range')
    if any(counts[i]+counts[i+6]+counts[i+12]!=n for i in range(6)):raise RuntimeError('Mask state partition mismatch')
    if any(counts[i]!=counts[i+1]+counts[i+2] for i in range(24,69,3)):raise RuntimeError('Mask pair partition mismatch')
    return row


def main():
    ROOT.mkdir(parents=True,exist_ok=True,mode=0o700);(ROOT/'batches').mkdir(exist_ok=True,mode=0o700);os.umask(0o077)
    lock=(ROOT/'service.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    command=['./qc_engine','qc_engine.spv',str(MAX_BATCH),'genome_masks.spv']
    if os.environ.get('GPU_QC_WORKGROUPS')=='1':command.append('workgroup')
    engine=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE)
    def execute(sequence,quality,verify):
        if len(sequence)!=len(quality):raise ValueError('Sequence/quality lengths differ')
        engine.stdin.write(struct.pack('<4sII',b'GPQ1',len(sequence),int(verify)));engine.stdin.write(sequence);engine.stdin.write(quality);engine.stdin.flush()
        line=engine.stdout.readline()
        if not line:raise RuntimeError('Persistent Vulkan engine stopped')
        return validate_aggregates(json.loads(line))
    def execute_mask(data,verify=False):
        if not data or len(data)%4:raise ValueError('Mask tile must contain little-endian uint32 words')
        count=len(data)//4
        if verify and count>16384:raise ValueError('CPU mask fixture verification limited to 16384 positions')
        engine.stdin.write(struct.pack('<4sII',b'GPM1',count,0));engine.stdin.write(data);engine.stdin.flush();line=engine.stdout.readline()
        if not line:raise RuntimeError('Persistent mask engine stopped')
        row=validate_mask(json.loads(line));row.update(cpu_crosschecked=verify,exact_match=None)
        if verify:
            expected=mask_oracle(struct.unpack('<'+'I'*count,data))
            if row['mask_counts']!=expected:raise RuntimeError('GPU/CPU mask mismatch')
            row['exact_match']=True
        return row
    try:prior=json.loads((ROOT/'daemon.json').read_text())
    except (OSError,ValueError):prior={}
    state={'status':'starting','runtime_version':VERSION,'batches':max(0,int(prior.get('batches',0))),'bases':max(0,int(prior.get('bases',0))),'mask_batches':max(0,int(prior.get('mask_batches',0))),'mask_positions':max(0,int(prior.get('mask_positions',0))),'startup_fixtures':0}
    def save():
        state['updated']=time.time();tmp=ROOT/'daemon.json.tmp';tmp.write_text(json.dumps(state));tmp.replace(ROOT/'daemon.json')
    try:
        for n in (1,97,1025,4097):
            seq=(b'ACGTNacgtnRY-'*((n+12)//13))[:n];qual=bytes(33+i%94 for i in range(n));row=execute(seq,qual,True)
            assert row['exact_match'] is True;state['startup_fixtures']+=1
        fixture=[sum(((value+k)%16)<<(k*4) for k in range(6)) for value in range(16)]
        fixture+=[x|0x80000000 for x in fixture]
        for n in (17,1025):
            words=(fixture*((n+31)//32))[:n];execute_mask(struct.pack('<'+'I'*n,*words),True)
        state['mask_startup_fixtures']=2
        state.update(status='running',compute_mode='cooperative' if os.environ.get('GPU_QC_WORKGROUPS')=='1' else 'legacy',device=row['device'],device_type=row['device_type'],max_batch_bytes=MAX_BATCH);save()
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
                    if type(request.get('version'))is not int or request.get('version')!=1 or not isinstance(request_id,str) or not re.fullmatch('[a-fA-F0-9]{1,64}',request_id):raise ValueError('Unsupported version or request ID')
                    if request.get('op')=='status':response={**state,'version':1,'request_id':request_id}
                    elif request.get('op')=='mask':
                        started=time.monotonic();row=execute_mask(read_batch(request['mask_path']),request.get('verify') is True)
                        row['timings']['engine_ms']=row['timings']['request_ms'];row['timings']['request_ms']=(time.monotonic()-started)*1000
                        response={**row,'version':1,'request_id':request_id,'backend':'vulkan','validated_backend':True};state['mask_batches']=state.get('mask_batches',0)+1;state['mask_positions']=state.get('mask_positions',0)+row['positions'];save()
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
