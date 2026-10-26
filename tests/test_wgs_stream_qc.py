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
    assert qc_memory_mb(128*1024**2,32*1024)==292
    assert qc_memory_mb(256*1024**2,32*1024)==548


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
