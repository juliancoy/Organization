import gzip
import hashlib
import io
import json
import re
import pytest
from scripts.wgs_single_copy_worker import transfer,parallel_transfer


class Response(io.BytesIO):
    status=206
    def __init__(self,data,start,total,end=None):
        super().__init__(data)
        end=total-1 if end is None else end
        self.headers={'Content-Range':f'bytes {start}-{end}/{total}','Content-Length':str(end-start+1)}
    def geturl(self):return 'https://example.test/reads.fastq.gz'


def fixture(tmp_path):
    data=gzip.compress(b'@synthetic\nACGT\n+\nIIII\n'*1000)
    item={'url':'https://example.test/reads.fastq.gz','filename':'reads.fastq.gz','bytes':len(data),'md5':hashlib.md5(data).hexdigest()}
    current={'id':'fixture','item':item,'allowed_hosts':['example.test']}
    seen=[]
    def opener(request,timeout):
        match=re.match(r'bytes=(\d+)-(\d+)',request.get_header('Range'));start,end=map(int,match.groups());seen.append(start)
        return Response(data[start:end+1],start,len(data),end)
    return data,current,opener,seen


def test_resume_uses_only_remaining_range_and_publishes_after_checksum(tmp_path):
    data,current,opener,seen=fixture(tmp_path);folder=tmp_path/'fixture';folder.mkdir()
    (folder/'reads.fastq.gz.part').write_bytes(data[:37])
    receipt=transfer(current,tmp_path,opener=opener)
    assert seen==[37]
    assert (folder/'reads.fastq.gz').read_bytes()==data
    assert not (folder/'reads.fastq.gz.part').exists()
    assert receipt['sha256']==hashlib.sha256(data).hexdigest()
    assert json.loads((folder/'progress.json').read_text())['phase']=='verified'


def test_bad_checksum_never_publishes_or_deletes_partial(tmp_path):
    data,current,opener,_=fixture(tmp_path);current['item']['md5']='0'*32
    with pytest.raises(ValueError,match='checksum'):transfer(current,tmp_path,opener=opener)
    folder=tmp_path/'fixture'
    assert not (folder/'reads.fastq.gz').exists() and not (folder/'receipt.json').exists()
    assert (folder/'reads.fastq.gz.part').read_bytes()==data


def test_ignored_resume_range_is_rejected(tmp_path):
    data,current,opener,_=fixture(tmp_path)
    def ignored(request,timeout):
        response=opener(request,timeout);response.status=200;return response
    with pytest.raises(ValueError,match='resume range'):transfer(current,tmp_path,opener=ignored)
    assert not (tmp_path/'fixture/receipt.json').exists()


def test_parallel_ranges_import_prefix_without_a_second_file(tmp_path):
    data,current,opener,seen=fixture(tmp_path);folder=tmp_path/'fixture';folder.mkdir()
    (folder/'reads.fastq.gz.part').write_bytes(data[:37])
    receipt=parallel_transfer(current,tmp_path,3,opener=opener,chunk_bytes=32)
    assert 0 not in seen and set(seen)==set(range(32,len(data),32))
    assert (folder/'reads.fastq.gz').read_bytes()==data
    assert receipt['md5']==hashlib.md5(data).hexdigest()
    assert not list(folder.glob('*.assembling'))
    assert json.loads((folder/'progress.json').read_text())['phase']=='verified'


def test_parallel_checksum_mismatch_does_not_publish(tmp_path):
    data,current,opener,_=fixture(tmp_path);current['item']['md5']='0'*32
    with pytest.raises(ValueError,match='checksum'):
        parallel_transfer(current,tmp_path,3,opener=opener,chunk_bytes=32)
    assert not (tmp_path/'fixture/reads.fastq.gz').exists()
    assert (tmp_path/'fixture/reads.fastq.gz.part').exists()


def test_saved_range_bitmap_is_verified_before_resume(tmp_path):
    data,current,opener,seen=fixture(tmp_path);folder=tmp_path/'fixture';folder.mkdir()
    partial=folder/'reads.fastq.gz.part';partial.write_bytes(data)
    checkpoint={**{k:current['item'][k] for k in ['url','bytes','md5']},'chunk_bytes':32,'done':{'0':hashlib.sha256(data[:32]).hexdigest()}}
    (folder/'ranges.json').write_text(json.dumps(checkpoint))
    with partial.open('r+b') as stream:stream.write(b'corrupted-prefix')
    with pytest.raises(ValueError,match='integrity'):
        parallel_transfer(current,tmp_path,3,opener=opener,chunk_bytes=32)
    assert not seen and not (folder/'receipt.json').exists()


def test_archive_error_body_retries_uncommitted_range(tmp_path):
    data,current,opener,seen=fixture(tmp_path);failed=[False]
    def transient(request,timeout):
        if not failed[0]:
            failed[0]=True
            response=opener(request,timeout)
            response.seek(0);response.write(b'<Error><Code>SlowDown</Code>');response.seek(0)
            return response
        return opener(request,timeout)
    receipt=parallel_transfer(current,tmp_path,1,opener=transient,sleep=lambda _:None,chunk_bytes=32)
    assert failed[0] and receipt['md5']==hashlib.md5(data).hexdigest()
    assert (tmp_path/'fixture/reads.fastq.gz').read_bytes()==data
