import hashlib
import json
from pathlib import Path

import pytest
from scripts.wgs_swarm_download import validate_manifest,assign_files,file_id,DOWNLOAD_WORKER


def item(url='https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR000/SRR000001/SRR000001.fastq.gz',size=100):
    return {'url':url,'bytes':size,'md5':'a'*32,'filename':'SRR000001.fastq.gz','sample':'Watson','run':'SRR000001'}


def test_manifest_rejects_nonarchive_and_traversal():
    assert validate_manifest({'files':[item()]})[0]['bytes']==100
    for change in ({'url':'https://evil.example/reads.gz'},{'filename':'../reads.gz'},{'bytes':-1},{'md5':'unknown'}):
        with pytest.raises(ValueError):validate_manifest({'files':[{**item(),**change}]})


def test_disk_assignment_reserves_chunk_and_assembly_bytes():
    first=item(size=100);second=item(url=item()['url'].replace('000001','000002'),size=60)
    hosts=[{'host':'small','free_bytes':150},{'host':'large','free_bytes':250}]
    mapping=assign_files([first,second],hosts)
    assert mapping[file_id(first)]=='large'
    assert mapping[file_id(second)]=='small'
    with pytest.raises(RuntimeError):assign_files([item(size=200)],hosts)


def test_resume_preserves_existing_file_host_without_double_counting_cache():
    source=item(size=100);ident=file_id(source)
    mapping=assign_files([source],[{'host':'peer','free_bytes':30,'cache_sizes':{ident:200}}],{ident:{'host':'peer','status':'downloading'}})
    assert mapping[ident]=='peer'


def test_worker_downloads_exact_ranges_checks_publisher_md5_and_resumes(tmp_path,monkeypatch):
    import io
    import sys
    import urllib.request
    payload=b'public compressed archive bytes for range transport test'*3
    source={**item(size=len(payload)),'md5':hashlib.md5(payload).hexdigest()}
    job={'item':source,'id':file_id(source),'chunk_bytes':23,'connections':3,'allowed_hosts':['ftp.sra.ebi.ac.uk']}
    calls=[]
    class Response(io.BytesIO):
        status=206
        def __init__(self,start,end):
            super().__init__(payload[start:end+1]);self.headers={'Content-Range':f'bytes {start}-{end}/{len(payload)}'}
        def geturl(self):return source['url']
    def request(req,timeout):
        start,end=map(int,req.headers['Range'].removeprefix('bytes=').split('-'));calls.append((start,end));return Response(start,end)
    monkeypatch.setenv('WGS_CACHE_ROOT',str(tmp_path));monkeypatch.setattr(urllib.request,'urlopen',request)
    output=io.StringIO();monkeypatch.setattr(sys,'stdin',io.StringIO(json.dumps(job)));monkeypatch.setattr(sys,'stdout',output)
    namespace={};exec(compile(DOWNLOAD_WORKER,'worker','exec'),namespace)
    receipt=json.loads(output.getvalue())
    assert receipt['md5']==source['md5']
    assert (tmp_path/receipt['cache_relative_path']).read_bytes()==payload
    assert len(calls)==(len(payload)+22)//23
    assert not (tmp_path/job['id']/'chunks').exists()
    monkeypatch.setattr(sys,'stdin',io.StringIO(json.dumps(job)));monkeypatch.setattr(sys,'stdout',io.StringIO())
    exec(compile(DOWNLOAD_WORKER,'worker','exec'),{})
    assert len(calls)==(len(payload)+22)//23


@pytest.mark.parametrize("corrupt",[False,True])
def test_worker_resumes_existing_partial_at_exact_byte_offset(tmp_path,monkeypatch,corrupt):
    import io,sys,urllib.request
    payload=b'A'*(1024*1024+7)+b'remaining fresh publisher data'
    source={**item(size=len(payload)),'md5':hashlib.md5(payload).hexdigest()};ident=file_id(source)
    folder=tmp_path/ident/'chunks';folder.mkdir(parents=True);(folder/'0.part').write_bytes((payload[:100]+b'X'+payload[101:1024*1024+7]) if corrupt else payload[:1024*1024+7])
    job={'item':source,'id':ident,'chunk_bytes':2*1024*1024,'connections':3,'allowed_hosts':['ftp.sra.ebi.ac.uk']};calls=[]
    class Response(io.BytesIO):
        status=206
        def __init__(self,start,end):super().__init__(payload[start:end+1]);self.headers={'Content-Range':f'bytes {start}-{end}/{len(payload)}'}
        def geturl(self):return source['url']
    def request(req,timeout):
        start,end=map(int,req.headers['Range'].removeprefix('bytes=').split('-'));calls.append((start,end));return Response(start,end)
    monkeypatch.setenv('WGS_CACHE_ROOT',str(tmp_path));monkeypatch.setattr(urllib.request,'urlopen',request);monkeypatch.setattr(sys,'stdin',io.StringIO(json.dumps(job)));monkeypatch.setattr(sys,'stdout',io.StringIO())
    exec(compile(DOWNLOAD_WORKER,'worker','exec'),{})
    assert (7,len(payload)-1) in calls
    assert ((0,len(payload)-1) in calls)==corrupt
    assert bool(list((folder.parent/'integrity-quarantine').glob('0.part.overlap-mismatch-*')))==corrupt
    assert (tmp_path/ident/source['filename']).read_bytes()==payload


def test_one_worker_downloads_multiple_small_files_concurrently(tmp_path,monkeypatch):
    import io,sys,threading,urllib.request
    jobs=[];payloads={}
    for index in range(4):
        source=item(url=f'https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR000/SRR00000{index}/SRR00000{index}.fastq.gz',size=20)
        payloads[source['url']]=bytes([65+index])*20;source['md5']=hashlib.md5(payloads[source['url']]).hexdigest();jobs.append({'item':source,'id':file_id(source),'chunk_bytes':128,'allowed_hosts':['ftp.sra.ebi.ac.uk']})
    import time
    (tmp_path/'.network-control.json').write_text(json.dumps({'connections':4,'updated':time.time()}))
    barrier=threading.Barrier(4)
    class Response(io.BytesIO):
        status=206
        def __init__(self,url):super().__init__(payloads[url]);self.url=url;self.headers={'Content-Range':'bytes 0-19/20'}
        def geturl(self):return self.url
    def request(req,timeout):barrier.wait(timeout=2);return Response(req.full_url)
    monkeypatch.setenv('WGS_CACHE_ROOT',str(tmp_path));monkeypatch.setattr(urllib.request,'urlopen',request);monkeypatch.setattr(sys,'stdin',io.StringIO(json.dumps({'jobs':jobs,'connections':4})));out=io.StringIO();monkeypatch.setattr(sys,'stdout',out)
    exec(compile(DOWNLOAD_WORKER,'worker','exec'),{})
    receipts=[json.loads(line) for line in out.getvalue().splitlines()]
    assert len(receipts)==4 and all('md5' in receipt for receipt in receipts)


def test_failed_ssh_still_cleans_remote_container_before_resource_release(monkeypatch):
    from scripts import wgs_swarm_download as module
    events=[]
    class Client:
        def call(self,op,args):events.append(op)
    monkeypatch.setattr(module,'cleanup_owned_container',lambda host,name:events.append('remote Docker cleanup') or True)
    module.release_after_cleanup({'host':'peer'},'genomics-'+'a'*32,Client(),'lease')
    assert events==['remote Docker cleanup','resource.release']


def test_unconfirmed_remote_cleanup_does_not_release_live_container_budget(monkeypatch):
    from scripts import wgs_swarm_download as module
    from unittest.mock import Mock
    client=Mock();monkeypatch.setattr(module,'cleanup_owned_container',lambda *args:False)
    with pytest.raises(RuntimeError,match='lease retained'):module.release_after_cleanup({},'name',client,'lease')
    client.call.assert_not_called()


def test_shared_flow_budget_can_reduce_below_machine_count_and_rotate_fairly():
    from scripts.wgs_swarm_download import flow_assignments,FlowBudget
    hosts=[{'host':str(i),'cpus':8} for i in range(6)]
    policy=FlowBudget(hosts,now=100)
    assert policy.budget==6
    assert policy.update(105,1,1000)==3
    assert policy.not_before==150
    assert policy.update(110,2,1000)==1
    first=flow_assignments(hosts,1,rotation=0);second=flow_assignments(hosts,1,rotation=1)
    assert sum(first.values())==sum(second.values())==1
    assert first['0']==1 and second['1']==1
    assert policy.update(180,2,1000)==2
    assert policy.update(215,2,900)==2, 'No upward ramp without measured throughput gain'
    assert policy.update(216,2,1100)==3


def test_streaming_lagging_mate_gets_shared_wan_token_before_far_ahead_files():
    from scripts.wgs_swarm_download import flow_assignments
    hosts=[{'host':'Watson','cpus':8},{'host':'R1','cpus':8},{'host':'R2','cpus':8}]
    assert flow_assignments(hosts,1,priorities={'R2'})=={'Watson':0,'R1':0,'R2':1}


def test_bad_preserved_single_range_is_quarantined_and_freshly_retried(tmp_path,monkeypatch):
    import io,sys,urllib.request
    payload=b'correct publisher payload';bad=b'X'+payload[1:]
    source={**item(size=len(payload)),'md5':hashlib.md5(payload).hexdigest()};ident=file_id(source);chunks=tmp_path/ident/'chunks';chunks.mkdir(parents=True)
    (chunks/'0.bin').write_bytes(bad);(chunks/'0.json').write_text(json.dumps({'sha256':hashlib.sha256(bad).hexdigest(),'bytes':len(bad)}))
    class Response(io.BytesIO):
        status=206
        def __init__(self):super().__init__(payload);self.headers={'Content-Range':f'bytes 0-{len(payload)-1}/{len(payload)}'}
        def geturl(self):return source['url']
    monkeypatch.setenv('WGS_CACHE_ROOT',str(tmp_path));monkeypatch.setattr(urllib.request,'urlopen',lambda *a,**k:Response());monkeypatch.setattr(sys,'stdin',io.StringIO(json.dumps({'item':source,'id':ident,'chunk_bytes':128,'connections':1,'allowed_hosts':['ftp.sra.ebi.ac.uk']})));output=io.StringIO();monkeypatch.setattr(sys,'stdout',output)
    exec(compile(DOWNLOAD_WORKER,'worker','exec'),{})
    receipt=json.loads(output.getvalue());assert receipt['md5']==source['md5']
    assert (tmp_path/ident/source['filename']).read_bytes()==payload
    quarantined=list((tmp_path/ident).glob('checksum-mismatch-*/chunks/0.bin'));assert len(quarantined)==1 and quarantined[0].read_bytes()==bad


def test_seeded_paired_stream_requests_earliest_missing_chunk_before_small_files(tmp_path,monkeypatch):
    import io,sys,urllib.request
    payloads={};jobs=[]
    for index,(size,layout) in enumerate([(10,'SINGLE'),(43,'PAIRED')]):
        source={**item(url=f'https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR000/SRR00000{index}/SRR00000{index}.fastq.gz',size=size),'layout':layout}
        data=bytes([65+index])*size;source['md5']=hashlib.md5(data).hexdigest();payloads[source['url']]=data;jobs.append({'item':source,'id':file_id(source),'chunk_bytes':16,'allowed_hosts':['ftp.sra.ebi.ac.uk']})
    paired=jobs[1];chunks=tmp_path/paired['id']/'chunks';chunks.mkdir(parents=True)
    for i in range(2):
        data=payloads[paired['item']['url']][i*16:(i+1)*16];(chunks/f'{i}.bin').write_bytes(data);(chunks/f'{i}.json').write_text(json.dumps({'sha256':hashlib.sha256(data).hexdigest(),'bytes':16}))
    calls=[]
    class Response(io.BytesIO):
        status=206
        def __init__(self,url,start,end):super().__init__(payloads[url][start:end+1]);self.url=url;self.headers={'Content-Range':f'bytes {start}-{end}/{len(payloads[url])}'}
        def geturl(self):return self.url
    def request(req,timeout):
        start,end=map(int,req.headers['Range'].removeprefix('bytes=').split('-'));calls.append((req.full_url,start));return Response(req.full_url,start,end)
    monkeypatch.setenv('WGS_CACHE_ROOT',str(tmp_path));monkeypatch.setattr(urllib.request,'urlopen',request);monkeypatch.setattr(sys,'stdin',io.StringIO(json.dumps({'jobs':jobs,'connections':1})));monkeypatch.setattr(sys,'stdout',io.StringIO())
    exec(compile(DOWNLOAD_WORKER,'worker','exec'),{})
    assert calls[0]==(paired['item']['url'],32)
    assert len(calls)==2, 'Seeded prefix must never be downloaded again'


def test_control_timeout_does_not_stop_other_peer_heartbeats(monkeypatch):
    import subprocess
    from types import SimpleNamespace
    from scripts import wgs_swarm_download as module
    monkeypatch.setattr(module.distributed,'shell_command',lambda host,argv:[host['host']])
    calls=[]
    def run(command,**kwargs):
        calls.append((command[0],kwargs))
        if command[0]=='unreachable':raise subprocess.TimeoutExpired(command,kwargs['timeout'])
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(module.subprocess,'run',run)
    assert module.write_network_control({'host':'unreachable','cache':'/cache'},6) is False
    assert module.write_network_control({'host':'healthy','cache':'/cache'},6) is True
    assert all(call[1]['timeout']<20 for call in calls)
    assert json.loads(calls[1][1]['input'])['connections']==6


def test_readiness_waits_for_updates_but_never_substitutes_machine_identity(monkeypatch):
    from unittest.mock import Mock
    from scripts import wgs_swarm_download as module
    client=Mock();monkeypatch.setattr(module,'AgentClient',lambda _:client)
    status={'machine':'expected','ready':False,'protocol':'lifetech-agent.v1','worker_policy':{'accepting_jobs':True}}
    client.call.return_value=status
    assert module.download_worker_ready({'machine':'expected'}) is False
    client.call.return_value={**status,'ready':True}
    assert module.download_worker_ready({'machine':'expected'}) is True
    client.call.return_value={**status,'machine':'other','ready':True}
    with pytest.raises(RuntimeError,match='machine identity differs'):module.download_worker_ready({'machine':'expected'})
    assert client.close.call_count==3
