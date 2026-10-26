import gzip
import json
from pathlib import Path
import tempfile
import unittest
from scripts.wgs_paired_shards import split

class PairTests(unittest.TestCase):
    def write(self,path,names):
        with gzip.open(path,'wb') as stream:
            for name in names:stream.write(b'@'+name+b'\nAC\n+\nII\n')
    def test_atomic_matched_pairs_and_last_partial_shard(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder);self.write(p/'a.gz',[b'a/1',b'b/1',b'c/1']);self.write(p/'b.gz',[b'a/2',b'b/2',b'c/2'])
            self.assertEqual(split(p/'a.gz',p/'b.gz',p/'out',2)['pairs'],3)
            self.assertEqual(json.loads((p/'out/000001.json').read_text())['pairs'],1)
    def test_mismatched_pairs_never_publish_shard(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder);self.write(p/'a.gz',[b'a/1']);self.write(p/'b.gz',[b'b/2'])
            with self.assertRaises(ValueError):split(p/'a.gz',p/'b.gz',p/'out',2)
            self.assertFalse((p/'out/000000.json').exists())
