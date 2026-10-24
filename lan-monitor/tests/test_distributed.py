import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from distributed import DistributedTests, plan, targets, no_remote_shell

class DistributedTestsTests(unittest.TestCase):
    def test_stop_is_idempotent_and_targets_only_current_run(self):
        with tempfile.TemporaryDirectory() as folder:
            service = DistributedTests(state=folder)
            self.assertFalse(service.stop())
            service.running = True
            service.run = {'id': '20261023T120000Z-1234abcd', 'status': 'running',
                           'hosts': plan([{'host': 'a.local', 'user': 'julian'}])}
            with patch('distributed.threading.Thread') as thread:
                self.assertTrue(service.stop())
                self.assertFalse(service.stop())
                thread.return_value.start.assert_called_once()
            self.assertEqual(service.view()['run']['status'], 'stopping')
            service.run['hosts'][0]['agent_endpoint']='http://10.42.0.2:8790'
            with patch('agent_client.AgentClient') as client:
                client.return_value.call.return_value={'acknowledged':True}
                service.stop_hosts(service.run)
                client.return_value.call.assert_called_once_with('job.stop',{'id':service.run['id']},timeout=40)
                self.assertTrue(service.run['hosts'][0]['stop_acknowledged'])

    def test_inventory_uses_enrollment_and_never_falls_back_to_ssh(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'peers.json'
            path.write_text(json.dumps({'name':'a.local','peers':{'a.local':'10.42.0.1','b.local':'10.42.0.2'}}))
            with patch('agent_client.CONFIG',path):workers=targets()
            self.assertEqual([h['host'] for h in workers],['a.local','b.local'])
            self.assertTrue(workers[0]['local'])
            with self.assertRaises(RuntimeError):no_remote_shell(workers[1])

    def test_every_suite_is_assigned_and_common_checks_run_on_every_host(self):
        hosts = plan([{'host': name + '.local', 'user': 'julian'} for name in ['a', 'b', 'c']])
        for host in hosts:
            self.assertIn('monitor', host['suites']); self.assertIn('map', host['suites'])
        for suite in ['web', 'org-api', 'chat', 'workspace', 'identity', 'identity-python']:
            self.assertEqual(sum(suite in host['suites'] for host in hosts), 1)

    def test_github_urls_and_pinned_source_manifest(self):
        from github_sources import github_url,source_digest
        self.assertEqual(github_url('git@github.com:owner/Organization.git'),'https://github.com/owner/Organization.git')
        self.assertEqual(github_url('https://github.com/owner/PIdP'),'https://github.com/owner/PIdP.git')
        for url in ('https://token@github.com/owner/repo','file:///tmp/repo','https://elsewhere.example/repo'):
            with self.assertRaises(ValueError):github_url(url)
        refs={'.':{'url':'https://github.com/owner/Organization.git','commit':'a'*40,'stage':'pushed'}}
        before=source_digest(refs)
        refs['.']['stage']='verified'
        self.assertEqual(before,source_digest(refs))
        refs['.']['commit']='b'*40
        self.assertNotEqual(before,source_digest(refs))

    def test_history_shows_ten_independent_records_without_replacing_older_results(self):
        with tempfile.TemporaryDirectory() as folder:
            service=DistributedTests(state=folder)
            ids=[]
            for index in range(12):
                ident='20261024T120000Z-'+format(index,'08x');ids.append(ident)
                service.run={'id':ident,'started':'2026-10-24T12:00:00Z','status':'passed',
                             'hosts':[{'jobs':{'monitor':{'status':'passed','logs':['run '+str(index)]}}}]}
                service.save()
            view=service.view()
            self.assertEqual(len(view['history']),10)
            self.assertEqual(view['history'][0]['id'],ids[-1])
            self.assertEqual(view['history'][0]['passed'],1)
            old=service.view(ids[0])['run']
            self.assertEqual(old['hosts'][0]['jobs']['monitor']['logs'],['run 0'])
            self.assertEqual(service.run['id'],ids[-1])
            self.assertEqual(len(list(Path(folder).glob('*.json'))),12)

    def test_restart_marks_jobs_interrupted_and_history_ids_cannot_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '20261023T120000Z-1234abcd.json'
            path.write_text(json.dumps({'id': path.stem, 'status': 'running', 'hosts': [{'status': 'running', 'jobs': {'a': {'status': 'running'}}}]}))
            service = DistributedTests(state=folder)
            self.assertEqual(service.view()['run']['hosts'][0]['jobs']['a']['status'], 'interrupted')
            with self.assertRaises(ValueError): service.view('../../etc/passwd')

if __name__ == '__main__': unittest.main()
