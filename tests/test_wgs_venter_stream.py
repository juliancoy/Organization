import gzip
import hashlib
import io
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from scripts.wgs_stream_qc import OrderedChunks
from scripts.wgs_paired_shards import split_opened
from scripts.wgs_venter_stream import streaming_worker_code

class VenterStreamTests(unittest.TestCase):
    def test_paired_shards_from_committed_ranges_before_source_receipts(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);readers=[];streams=[]
            for mate in (1,2):
                raw=gzip.compress(f'@a/{mate}\nACGT\n+\nIIII\n'.encode(),mtime=0)
                p=root/str(mate);(p/'chunks').mkdir(parents=True)
                for i,offset in enumerate(range(0,len(raw),10)):
                    block=raw[offset:offset+10];(p/f'chunks/{i}.bin').write_bytes(block)
                    (p/f'chunks/{i}.json').write_text(json.dumps({'bytes':len(block),'sha256':hashlib.sha256(block).hexdigest()}))
                item={'bytes':len(raw),'md5':hashlib.md5(raw).hexdigest(),'filename':f'{mate}.fq.gz','url':'https://public.test/'+str(mate)}
                reader=OrderedChunks(p,item,chunk_bytes=10);readers.append(reader)
                streams.append(gzip.GzipFile(fileobj=io.BufferedReader(reader),mode='rb'))
            try:
                result=split_opened(*streams,root/'shards',records_per_shard=10)
                self.assertEqual(result['pairs'],1)
                for reader in readers:self.assertEqual(reader.offset,reader.item['bytes']);self.assertEqual(reader.md5.hexdigest(),reader.item['md5'])
                self.assertTrue((root/'shards/000000.json').exists())
            finally:
                for stream in streams:stream.close()
                for reader in readers:reader.close()
    def test_remote_worker_compiles_without_repository_imports(self):
        source=streaming_worker_code();compile(source,'remote-worker','exec')
        self.assertNotIn('from scripts.',source)

    def test_remote_worker_runs_to_paired_md5_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);items=[];folders={}
            for mate in (1,2):
                name=str(mate)+'.gz';raw=gzip.compress(('@a/'+str(mate)+'\nACGT\n+\nIIII\n').encode(),mtime=0)
                path=root/name;(path/'chunks').mkdir(parents=True)
                (path/'chunks/0.bin').write_bytes(raw)
                (path/'chunks/0.json').write_text(json.dumps({'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}))
                items.append({'filename':name,'url':'https://public.test/'+name,'bytes':len(raw),'md5':hashlib.md5(raw).hexdigest()});folders[name]=str(path)
            (root/'job.json').write_text(json.dumps({'items':items,'folders':folders}))
            (root/'worker.py').write_text(streaming_worker_code().replace('/work/',str(root)+'/'))
            subprocess.run([sys.executable,str(root/'worker.py')],check=True)
            self.assertEqual(json.loads((root/'shards/stream-verified.json').read_text())['pairs'],1)

    def test_old_generation_BAMs_cannot_finalize_after_source_replacement(self):
        from scripts.wgs_finalize import validate_generation
        with self.assertRaisesRegex(RuntimeError,'current fully verified'):
            validate_generation({'generation':'fresh','phase':'verified'},{'run':{'receipt':{'generation':'old'}}})
        with self.assertRaises(RuntimeError):
            validate_generation({'generation':'fresh','phase':'invalid'},{'run':{'receipt':{'generation':'fresh'}}})
        validate_generation({'generation':'fresh','phase':'verified'},{'run':{'receipt':{'generation':'fresh'}}})


def test_download_source_follows_reassigned_owner_and_cache(tmp_path):
    from scripts.wgs_venter_stream import download_source
    from scripts.wgs_swarm_download import file_id
    item={'url':'https://archive/r1'}
    hosts={'old':{'host':'old','cache':'/old'},'new':{'host':'new','cache':'/default'}}
    (tmp_path/'downloads').mkdir()
    state=tmp_path/'downloads/state.json'
    state.write_text(json.dumps({'jobs':{file_id(item):{'host':'old'}},'workers':[]}))
    assert download_source(tmp_path,hosts,item)['cache']=='/old'
    state.write_text(json.dumps({'jobs':{file_id(item):{'host':'new'}},'workers':[{'host':'new','cache':'/live'}]}))
    assert download_source(tmp_path,hosts,item)=={'host':'new','cache':'/live'}
    state.write_text(json.dumps({'jobs':{file_id(item):{'host':'untrusted'}}}))
    import pytest
    with pytest.raises(RuntimeError,match='not enrolled'):download_source(tmp_path,hosts,item)
