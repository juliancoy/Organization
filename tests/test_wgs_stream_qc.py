import gzip
import hashlib
import json
import threading
import time
from pathlib import Path

import pytest
from scripts.wgs_stream_qc import OrderedChunks,aggregate_qc,qc_memory_mb


def source(tmp_path):
    data=gzip.compress(b'@one\nACGN\n+\nIIII\n@two\nAT\n+\n!!\n',mtime=0)
    item={'url':'https://ftp.sra.ebi.ac.uk/test.fastq.gz','filename':'test.fastq.gz','bytes':len(data),'md5':hashlib.md5(data).hexdigest(),'read_count':2}
    return data,item


def receipt(folder,item,data):
    final=folder/item['filename'];final.write_bytes(data)
    (folder/'receipt.json').write_text(json.dumps({**item,'mtime_ns':final.stat().st_mtime_ns}))


def chunks(folder,data,size):
    (folder/'chunks').mkdir(exist_ok=True)
    for i,start in enumerate(range(0,len(data),size)):
        payload=data[start:start+size];(folder/'chunks'/f'{i}.bin').write_bytes(payload)
        (folder/'chunks'/f'{i}.json').write_text(json.dumps({'sha256':hashlib.sha256(payload).hexdigest(),'bytes':len(payload)}))


def test_full_fastq_counts_and_publisher_verification(tmp_path):
    data,item=source(tmp_path);receipt(tmp_path,item,data);progress=[]
    result=aggregate_qc(tmp_path,item,progress=progress.append)
    assert result['reads']==2 and result['bases']==6 and result['gc_bases']==2 and result['ambiguous_bases']==1
    assert result['q20_bases']==4 and result['q30_bases']==4
    assert result['quality_sum']==160 and result['min_length']==2 and result['max_length']==4
    assert result['provisional'] is False and progress[-1]['phase']=='verified'


def test_chunk_stream_ignores_partials_and_continues_after_final_replacement(tmp_path):
    data,item=source(tmp_path);size=11;chunks(tmp_path,data[:size],size)
    reader=OrderedChunks(tmp_path,item,size);assert reader.readinto(bytearray())==0
    assert reader.offset==0
    buffer=bytearray(size);assert reader.readinto(buffer)==size
    assert bytes(buffer)==data[:size]
    for p in (tmp_path/'chunks').iterdir():p.unlink()
    (tmp_path/'chunks'/'1.part').write_bytes(b'NOT COMPLETE')
    receipt(tmp_path,item,data)
    rest=reader.read();assert data==bytes(buffer)+rest
    assert reader.md5.hexdigest()==item['md5']


def test_stream_stays_provisional_until_full_receipt(tmp_path):
    data,item=source(tmp_path);chunks(tmp_path,data,11);progress=[];result=[]
    thread=threading.Thread(target=lambda:result.append(aggregate_qc(tmp_path,item,11,progress.append)));thread.start()
    deadline=time.monotonic()+2
    while time.monotonic()<deadline and not any(row['phase']=='awaiting_publisher_receipt' for row in progress):time.sleep(.02)
    assert thread.is_alive() and not result
    receipt(tmp_path,item,data);thread.join(2)
    assert result[0]['phase']=='verified'


def test_corrupt_complete_chunk_and_wrong_count_are_rejected(tmp_path):
    data,item=source(tmp_path);chunks(tmp_path,data,11)
    (tmp_path/'chunks'/'0.bin').write_bytes(b'bad')
    with pytest.raises(ValueError,match='chunk failed'):
        OrderedChunks(tmp_path,item,11).read()
    receipt(tmp_path,item,data)
    with pytest.raises(ValueError,match='read count differs'):
        aggregate_qc(tmp_path,{**item,'read_count':3})


def test_memory_budget_tracks_chunk_geometry_and_runtime():
    assert qc_memory_mb(128*1024**2,32*1024)==304
    assert qc_memory_mb(256*1024**2,32*1024)==560
    assert qc_memory_mb(128*1024**2,32*1024,gpu_parallel=True)==312


def test_partial_bytes_are_never_consumed_and_stop_unblocks_waiting(tmp_path):
    data,item=source(tmp_path);(tmp_path/'chunks').mkdir();(tmp_path/'chunks'/'0.part').write_bytes(data)
    stop=threading.Event();reader=OrderedChunks(tmp_path,item,11,stop=stop);errors=[]
    def run():
        try:reader.read(1)
        except Exception as e:errors.append(e)
    thread=threading.Thread(target=run);thread.start();time.sleep(.05)
    assert reader.offset==0
    stop.set();thread.join(1)
    assert isinstance(errors[0],InterruptedError)


def test_integrity_hold_rearms_only_changed_chunk_or_matching_publisher_receipt(monkeypatch):
    from types import SimpleNamespace
    from scripts import wgs_stream_qc as qc
    from scripts import genomics_distributed as distributed
    monkeypatch.setattr(distributed,'shell_command',lambda host,argv:argv)
    data={}
    monkeypatch.setattr(qc.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout=json.dumps(data)))
    item={'url':'https://archive/file','bytes':10,'md5':'publisher'}
    host={'cache':'/cache'};hold={'chunk0_sha256':'bad'}
    assert qc.integrity_hold_active(host,'id',item,hold)
    data['chunks/0.json']={'sha256':'bad'}
    assert qc.integrity_hold_active(host,'id',item,hold)
    data['receipt.json']={'url':item['url'],'bytes':10,'md5':'wrong'}
    assert qc.integrity_hold_active(host,'id',item,hold)
    data['chunks/0.json']={'sha256':'fresh'}
    assert not qc.integrity_hold_active(host,'id',item,hold)
    data['chunks/0.json']={'sha256':'bad'}
    data['receipt.json']={**item}
    assert not qc.integrity_hold_active(host,'id',item,hold)


def gpu_response(sequence,quality):
    from scripts.wgs_stream_qc import cpu_batch_counts
    return {**cpu_batch_counts(sequence,quality),'version':1,'bases':len(sequence),'backend':'vulkan','device':'fixture Intel GPU','device_type':1,'software_fallback':False,'validated_backend':True,'exact_match':None}


def assert_same_counts(left,right):
    for key in ['reads','bases','gc_bases','ambiguous_bases','quality_sum','q20_bases','q30_bases','base_counts','quality_histogram','min_length','max_length']:
        assert left[key]==right[key],key


def test_gpu_batches_cross_records_and_chunks_without_cpu_recount(tmp_path,monkeypatch):
    from scripts import wgs_stream_qc as qc
    data,item=source(tmp_path);receipt(tmp_path,item,data)
    expected=aggregate_qc(tmp_path,item,batch_bases=3)
    responses=[gpu_response(b'ACG',b'III'),gpu_response(b'NAT',b'I!!')];seen=[]
    def gpu(seq,qual):
        seen.append((bytes(seq),bytes(qual)));return responses[len(seen)-1]
    monkeypatch.setattr(qc,'cpu_batch_counts',lambda *a:(_ for _ in ()).throw(AssertionError('CPU recount on GPU route')))
    result=aggregate_qc(tmp_path,item,batch_bases=3,gpu_request=gpu)
    assert_same_counts(result,expected)
    assert seen==[(b'ACG',b'III'),(b'NAT',b'I!!')]
    assert result['gpu_batches']==2 and result['gpu_bases']==6 and result['cpu_bases']==0
    assert result['qc_backend']==result['histogram_source']=='vulkan'
    assert sum(result['quality_histogram'])==6


def test_gpu_rejection_and_interrupted_rpc_fallback_commit_each_batch_once(tmp_path):
    data,item=source(tmp_path);receipt(tmp_path,item,data)
    expected=aggregate_qc(tmp_path,item,batch_bases=2);calls=[]
    def gpu(seq,qual):
        calls.append(bytes(seq))
        if len(calls)==1:return gpu_response(seq,qual)
        if len(calls)==2:return {**gpu_response(seq,qual),'quality_sum':9999}
        raise TimeoutError('response lost after submission')
    result=aggregate_qc(tmp_path,item,batch_bases=2,gpu_request=gpu)
    assert_same_counts(result,expected)
    assert result['gpu_batches']==1 and result['cpu_batches']==2 and result['gpu_fallback_batches']==2
    assert result['gpu_bases']==2 and result['cpu_bases']==4 and result['qc_backend']=='mixed'


def test_missing_gpu_socket_falls_back_and_removes_private_batch_files(tmp_path):
    data,item=source(tmp_path);receipt(tmp_path,item,data)
    config={'local_root':str(tmp_path/'gpu'),'host_root':'/owner/gpu'}
    expected=aggregate_qc(tmp_path,item,batch_bases=2)
    result=aggregate_qc(tmp_path,item,batch_bases=2,gpu=config)
    assert_same_counts(result,expected)
    assert result['gpu_fallback_batches']==3 and result['cpu_bases']==6
    assert not list((tmp_path/'gpu/batches').iterdir())


def test_gpu_still_rejects_malformed_fastq_and_invalid_qualities(tmp_path):
    for text in [b'@a\nAC\n+\nI\n',b'@a\nAC\n+\nI \n']:
        data=gzip.compress(text,mtime=0);item={'url':'https://archive/test','filename':'a.gz','bytes':len(data),'md5':hashlib.md5(data).hexdigest(),'read_count':1};receipt(tmp_path,item,data)
        with pytest.raises(ValueError):aggregate_qc(tmp_path,item,batch_bases=1,gpu_request=gpu_response)


def test_gpu_counts_cover_all_quality_bins_and_noncanonical_lowercase_bases(tmp_path):
    sequence=(b'acgtnRY?'*12)[:94];quality=bytes(range(33,127));text=b'@a\n'+sequence+b'\n+\n'+quality+b'\n';data=gzip.compress(text,mtime=0);item={'url':'https://archive/quality','filename':'a.gz','bytes':len(data),'md5':hashlib.md5(data).hexdigest(),'read_count':1};receipt(tmp_path,item,data)
    expected=aggregate_qc(tmp_path,item,batch_bases=7);result=aggregate_qc(tmp_path,item,batch_bases=7,gpu_request=gpu_response)
    assert_same_counts(result,expected)
    assert result['quality_histogram']==[1]*94 and result['q20_bases']==74 and result['q30_bases']==64
    assert result['gpu_batches']==14 and result['base_counts']['other']>0


def test_gpu_batch_counts_with_ordered_chunk_boundaries_and_receipt_gate(tmp_path):
    data,item=source(tmp_path);chunks(tmp_path,data,11);phases=[]
    def progress(row):
        phases.append(row['phase'])
        if row['phase']=='awaiting_publisher_receipt':receipt(tmp_path,item,data)
    result=aggregate_qc(tmp_path,item,chunk_bytes=11,progress=progress,batch_bases=2,gpu_request=gpu_response)
    assert result['verified_chunks']>1 and 'awaiting_publisher_receipt' in phases
    assert result['gpu_batches']==3 and result['bases']==result['gpu_bases']==6
    assert result['input_md5']==item['md5'] and result['provisional'] is False


def test_gpu_socket_request_identity_is_checked_before_fallback(tmp_path):
    import socket
    from scripts.wgs_stream_qc import request_gpu_batch
    local=tmp_path/'gpu';local.mkdir();server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);server.bind(str(local/'service.sock'));server.listen(1);requests=[]
    def serve():
        with server.accept()[0] as connection:
            with connection.makefile('rb') as stream:requests.append(json.loads(stream.readline()))
            response={**gpu_response(b'AC',b'II'),'request_id':'wrong'}
            connection.sendall(json.dumps(response).encode()+b'\n')
        server.close()
    thread=threading.Thread(target=serve);thread.start()
    with pytest.raises(ValueError,match='identity'):request_gpu_batch(b'AC',b'II',{'local_root':str(local),'host_root':'/owner/gpu'})
    thread.join(1)
    assert requests[0]['sequence_path'].startswith('/owner/gpu/batches/')
    assert not list((local/'batches').iterdir())


def test_requested_per_batch_cpu_crosscheck_cannot_be_replaced_by_startup_fixtures(tmp_path):
    import socket
    from scripts.wgs_stream_qc import request_gpu_batch
    local=tmp_path/'gpu';local.mkdir();server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);server.bind(str(local/'service.sock'));server.listen(1)
    def serve():
        with server.accept()[0] as connection:
            with connection.makefile('rb') as stream:request=json.loads(stream.readline())
            connection.sendall(json.dumps({**gpu_response(b'AC',b'II'),'request_id':request['request_id'],'cpu_crosschecked':False}).encode()+b'\n')
        server.close()
    thread=threading.Thread(target=serve);thread.start()
    with pytest.raises(ValueError,match='crosscheck'):request_gpu_batch(b'AC',b'II',{'local_root':str(local),'host_root':'/owner/gpu','verify':True})
    thread.join(1)


def test_backfill_rejects_prior_qc_disagreement_and_incomplete_histogram(tmp_path):
    from scripts.wgs_stream_qc import validate_backfill
    data,item=source(tmp_path);receipt(tmp_path,item,data);baseline=aggregate_qc(tmp_path,item)
    result=aggregate_qc(tmp_path,item,gpu_request=gpu_response,batch_bases=2)
    validate_backfill(result,baseline)
    with pytest.raises(ValueError,match='prior verified'):validate_backfill({**result,'gc_bases':999},baseline)
    with pytest.raises(ValueError,match='histogram'):validate_backfill({**result,'quality_histogram':[0]*94},baseline)


def test_parallel_gpu_batch_allows_parsing_ahead_but_commits_once():
    from scripts.wgs_stream_qc import BatchCounts
    entered=threading.Event();release=threading.Event()
    def gpu(seq,qual):
        entered.set();assert release.wait(3);return gpu_response(seq,qual)
    counts={'gc_bases':0,'ambiguous_bases':0,'quality_sum':0,'q20_bases':0,'q30_bases':0}
    batch=BatchCounts(counts,batch_bases=3,gpu_request=gpu,gpu_parallel=True)
    try:
        batch.add(b'ACG',b'III');assert entered.wait(2)
        batch.add(b'NA',b'I!')
        assert batch.pending_bases()==5 and counts['gpu_bases']==0
        release.set();batch.finish()
        assert counts['gpu_bases']==5 and counts['gpu_batches']==2 and batch.pending_bases()==0
        assert sum(counts['base_counts'].values())==sum(counts['quality_histogram'])==5
    finally:release.set();batch.close()


def test_parallel_gpu_errors_fall_back_without_double_counting(tmp_path):
    data,item=source(tmp_path);receipt(tmp_path,item,data);expected=aggregate_qc(tmp_path,item,batch_bases=2);calls=[]
    def gpu(seq,qual):
        calls.append(bytes(seq))
        if len(calls)==1:return gpu_response(seq,qual)
        if len(calls)==2:return {**gpu_response(seq,qual),'quality_sum':9999}
        raise TimeoutError('GPU response lost')
    result=aggregate_qc(tmp_path,item,batch_bases=2,gpu_request=gpu,gpu_parallel=True)
    assert_same_counts(result,expected)
    assert result['gpu_batches']==1 and result['cpu_batches']==2 and result['gpu_fallback_batches']==2
    assert result['gpu_bases']+result['cpu_bases']==result['bases']==6


def test_parallel_gpu_drains_tail_before_publisher_verification(tmp_path):
    data,item=source(tmp_path);chunks(tmp_path,data,11);phases=[]
    def progress(row):
        phases.append(row['phase'])
        if row['phase']=='awaiting_publisher_receipt':receipt(tmp_path,item,data)
    result=aggregate_qc(tmp_path,item,chunk_bytes=11,progress=progress,batch_bases=4,gpu_request=gpu_response,gpu_parallel=True)
    assert result['gpu_bases']==result['bases']==6 and result['gpu_batches']==2
    assert 'awaiting_publisher_receipt' in phases and result['phase']=='verified'


def test_first_live_batch_is_crosschecked_then_gpu_only_batches_continue(tmp_path,monkeypatch):
    from scripts import wgs_stream_qc as qc
    data,item=source(tmp_path);receipt(tmp_path,item,data);seen=[]
    def request(sequence,quality,config):
        seen.append(config['verify'])
        return {**gpu_response(sequence,quality),'cpu_crosschecked':config['verify'],'exact_match':True if config['verify'] else None,'validated_backend':True}
    monkeypatch.setattr(qc,'request_gpu_batch',request)
    result=aggregate_qc(tmp_path,item,batch_bases=3,gpu={'verify_first':True},gpu_parallel=True)
    assert seen==[True,False]
    assert result['gpu_batches']==2 and result['gpu_cpu_crosschecked_batches']==1
    assert result['cpu_bases']==result['gpu_fallback_batches']==0


def test_qc_registers_with_shared_scheduler_when_ram_is_temporarily_reserved(tmp_path,monkeypatch):
    from unittest.mock import Mock
    from scripts import wgs_stream_qc as qc,genomics_distributed as distributed,wgs_swarm_download as downloads
    client=Mock();acquisitions=[]
    def call(operation,args=None):
        if operation=='status':return {'ready':True,'machine':'machine','protocol':'lifetech-agent.v1','worker_policy':{'accepting_jobs':True},'capabilities':['renewable-resources']}
        if operation=='resource.capacity':return {'available_mb':0,'available_cpus':0}
        if operation=='resource.acquire':acquisitions.append(args);return {'acquired':len(acquisitions)>1}
        return {}
    client.call.side_effect=call
    monkeypatch.setattr(downloads,'AgentClient',lambda _:client)
    monkeypatch.setattr(downloads,'release_after_cleanup',lambda *a:None)
    monkeypatch.setattr(distributed,'shell_command',lambda *a:['synthetic'])
    monkeypatch.setattr(qc,'STOP',Mock(wait=lambda _:False))
    def popen(*a,**k):raise RuntimeError('Reached admitted worker launch')
    monkeypatch.setattr(qc.subprocess,'Popen',popen)
    with pytest.raises(RuntimeError,match='Reached admitted worker launch'):
        qc.run_remote({'host':'cpu-host','machine':'machine','cache':str(tmp_path),'image':'test'}, {'bytes':100},'source',tmp_path)
    assert len(acquisitions)==2 and acquisitions[0]==acquisitions[1]
    assert acquisitions[0]['memory_mb']>0
