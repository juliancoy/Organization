import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import fleet_backup
from launcher_agent import Agent
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


class FleetBackupTests(unittest.TestCase):
    def fixture(self,root):
        store=root/'store';snapshot=store/'20261024T180859Z';snapshot.mkdir(parents=True)
        secret=b'private production fixture; never visible in remote ciphertext'
        (snapshot/'secrets.json').write_bytes(secret)
        blob=b'media fixture';digest=hashlib.sha256(blob).hexdigest();(store/'objects').mkdir();(store/'objects'/digest).write_bytes(blob)
        manifest={'status':'complete','completedAt':'2026-10-24','gaps':[],'files':{'secrets.json':hashlib.sha256(secret).hexdigest()},'objects':{'media':[{'sha256':digest,'size':len(blob)}]}}
        (snapshot/'manifest.json').write_text(json.dumps(manifest))
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048);keyfile=root/'private.pem'
        keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        return store,snapshot,key,keyfile
    def test_complete_encrypted_round_trip_includes_external_object_store(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);store,snapshot,key,keyfile=self.fixture(root)
            folder=fleet_backup.package(snapshot,store,root/'cache',keyfile,part_size=128)
            self.assertFalse(any(b'private production fixture' in p.read_bytes() for p in folder.glob('*.enc')))
            output=root/'restore';result=fleet_backup.verify(folder,key,target=output)
            self.assertEqual(result['files'],3);self.assertEqual((output/snapshot.name/'secrets.json').read_bytes(),(snapshot/'secrets.json').read_bytes())
            part=next(folder.glob('*.enc'));part.write_bytes(part.read_bytes()[:-1]+b'x')
            with self.assertRaises(RuntimeError):fleet_backup.verify(folder,key,fleet_backup.contents(snapshot,store))
    def test_incomplete_snapshot_is_not_replicated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);store,snapshot,key,keyfile=self.fixture(root)
            m=json.loads((snapshot/'manifest.json').read_text());m['gaps']=['missing credentials'];(snapshot/'manifest.json').write_text(json.dumps(m))
            with self.assertRaisesRegex(RuntimeError,'complete verified'):fleet_backup.package(snapshot,store,root/'cache',keyfile)
    def test_agent_requires_encryption_and_verified_parts_before_commit_and_resumes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);store,snapshot,key,keyfile=self.fixture(root);folder=fleet_backup.package(snapshot,store,root/'cache',keyfile,part_size=256)
            agent=object.__new__(Agent);agent.config=root/'peers.json';agent.root=root/'jobs';agent.root.mkdir();agent.lock=threading.RLock();agent.uploads={};agent.busy=lambda:None
            manifest=json.loads((folder/'manifest.json').read_text());args={'snapshot':snapshot.name}
            invalid={**manifest,'encryption':'none'}
            with self.assertRaises(ValueError):agent.backup('backup.begin',{'manifest':invalid})
            begin=agent.backup('backup.begin',{'manifest':manifest});self.assertEqual(len(begin['missing']),len(manifest['parts']))
            with self.assertRaises(RuntimeError):agent.backup('backup.commit',args)
            for p in manifest['parts']:
                raw=(folder/(p['sha256']+'.enc')).read_bytes();token=agent.upload('upload.begin',{'size':len(raw),'sha256':p['sha256']})['upload']
                agent.upload('upload.chunk',{'upload':token,'offset':0,'data':base64.b64encode(raw).decode()});agent.upload('upload.finish',{'upload':token})
                agent.backup('backup.put',{**args,'upload':token,'sha256':p['sha256']})
            self.assertTrue(agent.backup('backup.commit',args)['verified']);self.assertEqual(agent.backup('backup.begin',{'manifest':manifest})['missing'],[])
            self.assertEqual(len(agent.backup('backup.status',{})['copies']),1)
            p=manifest['parts'][0];(root/'backups'/snapshot.name/(p['sha256']+'.enc')).write_bytes(b'corrupt')
            self.assertIn(p['sha256'],agent.backup('backup.begin',{'manifest':manifest})['missing'])
            with self.assertRaises(RuntimeError):agent.backup('backup.commit',args)

if __name__=='__main__':unittest.main()
