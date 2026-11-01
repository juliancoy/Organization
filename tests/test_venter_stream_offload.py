import gzip
import hashlib
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import threading
import pytest
from scripts.venter_stream_offload import pump, streaming_recipe
from scripts.venter_peer_qc import BAM_QC


def bam():
    core=struct.pack('<iiIIiiii',-1,-1,2,4<<16,4,-1,-1,0)
    record=core+b'x\0'+b'\x12\x48'+bytes([30,31,32,33])
    return gzip.compress(b'BAM\x01'+struct.pack('<ii',0,0)+struct.pack('<i',len(record))+record)


def test_streamed_bam_parser_needs_no_seekable_cache():
    raw=bam();command=streaming_recipe(BAM_QC)
    result=subprocess.run([sys.executable,*command[1:]],input=raw,capture_output=True,check=True)
    assert json.loads(result.stdout)['counts']=={'records':1,'bases':4,'unmapped':1,'primary':1}
    assert b'/input' not in command[-1].encode()


def test_truncated_bam_cannot_return_success():
    result=subprocess.run([sys.executable,*streaming_recipe(BAM_QC)[1:]],input=bam()[:-5],capture_output=True)
    assert result.returncode!=0


@pytest.mark.parametrize('kind',['valid','wrong_md5','short','long'])
def test_complete_stream_identity_required(kind):
    raw=bam();output=io.BytesIO()
    payload=raw[:-3] if kind=='short' else raw+b'extra' if kind=='long' else raw
    md5='0'*32 if kind=='wrong_md5' else hashlib.md5(raw).hexdigest()
    if kind=='valid':
        assert pump(io.BytesIO(payload),output,len(raw),md5)==len(raw)
        assert output.getvalue()==raw
    else:
        with pytest.raises(ValueError):pump(io.BytesIO(payload),output,len(raw),md5)


def test_two_streams_progress_concurrently_without_whole_file_buffering():
    from concurrent.futures import ThreadPoolExecutor
    barrier=threading.Barrier(2)
    raw=bam();md5=hashlib.md5(raw).hexdigest()
    class Source(io.BytesIO):
        started=False
        def readinto(self,buffer):
            assert len(buffer)==1024*1024
            if not self.started:self.started=True;barrier.wait(timeout=3)
            return super().readinto(buffer)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:pump(Source(raw),io.BytesIO(),len(raw),md5),range(2)))
    assert results==[len(raw),len(raw)]


def test_saved_receipts_bind_run_source_code_and_result():
    from scripts.venter_stream_offload import validate_stream_receipt
    job={'id':'job','spec':{'source_md5':'a'*32,'bytes':123}}
    manifest={'id':'run','parent_run':'parent','module_sha256':'new','accepted_modules':['old','new']}
    receipt={'schema':'venter-stream-result.v1','run':'run','parent_run':'parent','job':'job','node':'compai.local',
             'input_md5':'a'*32,'input_bytes':123,'output_sha256':'result','module_sha256':'old'}
    validate_stream_receipt(receipt,job,manifest,'compai.local','result')
    for key in receipt:
        with pytest.raises(ValueError):validate_stream_receipt({**receipt,key:'tampered'},job,manifest,'compai.local','result')


@pytest.mark.parametrize('valid',[True,False])
def test_executor_publishes_signed_receipt_only_for_complete_verified_stream(tmp_path,monkeypatch,valid):
    import contextlib
    from types import SimpleNamespace
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from scripts import venter_stream_offload as execution
    from scripts import venter_peer_qc as worker
    raw=bam();key=Ed25519PrivateKey.generate();ident='b'*64
    job={'id':ident,'spec':{'bytes':len(raw),'source_md5':hashlib.md5(raw).hexdigest(),'kind':'bam-read-qc'}}
    if not valid:job['spec']['source_md5']='0'*32
    class Response(io.BytesIO):
        status=200
        headers={'Content-Length':str(len(raw))}
    admissions=[]
    class Lease:
        def __init__(self,*args,**kwargs):admissions.append(args);self.file=io.BytesIO()
        def try_acquire(self):return True
        def close(self):self.file.close()
    obj=execution.Offload.__new__(execution.Offload)
    obj.folder=tmp_path;obj.worker=worker
    obj.lease=SimpleNamespace(ResourceLease=Lease,worker_policy=lambda _: {'accepting_jobs':True})
    obj.peer=SimpleNamespace(name='compai.local',node={'image':'sha256:approved'},config={'resource_directory':str(tmp_path)},
                             key=key,keys={'compai.local':key.public_key()},open_peer=lambda *args,**kwargs:Response(raw))
    obj.manifest={'id':'run','parent_run':'parent','source':'source','module_sha256':'approved-code','expires':float('inf')}
    obj.retries=worker.RetryLedger(tmp_path);updates=[];obj.update=lambda *args,**kwargs:updates.append(kwargs)
    actual_popen=subprocess.Popen;calls=[]
    def parser(argv,**kwargs):
        calls.append(argv)
        assert argv[0]=='docker' and '--network' in argv and '-i' in argv
        return actual_popen([sys.executable,'-c',argv[-1]],**kwargs)
    monkeypatch.setattr(execution.subprocess,'Popen',parser)
    monkeypatch.setattr(execution.subprocess,'run',lambda argv,**kwargs: subprocess.CompletedProcess(argv,1 if 'inspect' in argv else 0,stdout='',stderr=''))
    receipt=tmp_path/ident/'receipt.json'
    if valid:
        obj.execute(job)
        value=worker.verified(worker.load(receipt),key.public_key())
        assert value['input_bytes']==len(raw) and value['input_md5']==job['spec']['source_md5']
        assert value['output_sha256']==worker.digest(tmp_path/ident/'result.json')
        assert updates[-1]['status']=='completed'
        obj.execute(job) # Same signed result reused; no second transfer/process.
        assert len(calls)==1
    else:
        with pytest.raises(ValueError,match='MD5'):obj.execute(job)
        assert not receipt.exists() and not (tmp_path/ident/'result.json').exists()
    assert admissions[0][1:3]==(2,2048)
