import asyncio
import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,AsyncMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from launcher_agent import worker_policy,Agent
from launcher_peers import JobGateway

class PolicyTests(unittest.TestCase):
    def test_battery_auto_ac_and_user_override(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);power=p/'power';power.mkdir();bat=power/'BAT0';bat.mkdir()
            (bat/'type').write_text('Battery');(bat/'status').write_text('Discharging');(bat/'capacity').write_text('42')
            cfg=p/'peers.json'
            self.assertFalse(worker_policy(cfg,power)['accepting_jobs'])
            ac=power/'AC';ac.mkdir();(ac/'type').write_text('Mains');(ac/'online').write_text('1')
            self.assertTrue(worker_policy(cfg,power)['accepting_jobs'])
            (p/'worker-policy.json').write_text('{"mode":"paused"}')
            self.assertFalse(worker_policy(cfg,power)['accepting_jobs'])
            (p/'worker-policy.json').write_text('{"mode":"always"}');(ac/'online').write_text('0')
            self.assertTrue(worker_policy(cfg,power)['accepting_jobs'])
    def test_paused_agent_rejects_new_work_before_docker(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);(p/'worker-policy.json').write_text('{"mode":"paused"}')
            agent=Agent.__new__(Agent);agent.config=p/'peers.json'
            for op,args in [('job.start',{}),('source.stage',{}),('browser.start',{}),('image.pull',{}),('job.feed',{'tasks':['x']})]:
                with self.assertRaisesRegex(RuntimeError,'paused'):agent.dispatch(op,args)
            self.assertFalse(agent.dispatch('worker.policy',{})['accepting_jobs'])

class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_fragmented_roundtrip_is_binary_exact(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'peers.json';p.write_text(json.dumps({'internet_nodes':{'worker':'remote'}}))
            agent=Mock(config=p);g=JobGateway(agent);ident='a'*32
            future=asyncio.get_running_loop().create_future();g.pending[('remote',ident)]=future
            data=b'\x00\xff'*20000
            for seq,off in enumerate(range(0,len(data),12000)):
                g.receive('remote',{'id':ident,'reply':True,'seq':seq,'end':off+12000>=len(data),'data':base64.b64encode(data[off:off+12000]).decode()})
            self.assertEqual(await future,data);self.assertFalse(g.parts)
    async def test_unenrolled_and_unsolicited_messages_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'peers.json';p.write_text(json.dumps({'internet_nodes':{'worker':'remote'}}))
            g=JobGateway(Mock(config=p));m={'id':'a'*32,'reply':True,'seq':0,'end':True,'data':'AA=='}
            with self.assertRaisesRegex(ValueError,'Unenrolled'):g.receive('stranger',m)
            with self.assertRaisesRegex(ValueError,'Unsolicited'):g.receive('remote',m)
    async def test_invalid_sequence_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'peers.json';p.write_text(json.dumps({'internet_nodes':{'worker':'remote'}}))
            g=JobGateway(Mock(config=p))
            with self.assertRaises(ValueError):g.receive('remote',{'id':'a'*32,'reply':False,'seq':9,'end':False,'data':'AA=='})
    async def test_disconnect_does_not_silently_accept_request(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'peers.json';p.write_text(json.dumps({'internet_nodes':{'worker':'remote'}}))
            g=JobGateway(Mock(config=p))
            with self.assertRaisesRegex(RuntimeError,'not connected'):await g.remote('worker',b'encrypted')
            self.assertFalse(g.pending)

if __name__=='__main__':unittest.main()
