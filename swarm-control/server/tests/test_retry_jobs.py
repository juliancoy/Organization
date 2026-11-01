from pathlib import Path
import tempfile
import unittest
from retry_jobs import RetryQueue,blocked
from task_events import EventStore

class RetryQueueTests(unittest.TestCase):
    def test_claim_backoff_budget_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store=EventStore(Path(directory)/'events.db');queue=RetryQueue(store)
            queue.offer({'id':'run'},'suite')
            self.assertIsNone(queue.claim())
            ticket=queue.claim(now=10**12);self.assertEqual(ticket['attempts'],1)
            # An unexpired admission claim cannot be taken by a second scheduler.
            self.assertIsNone(RetryQueue(store).claim(now=10**12+1))
            queue.dispatched(ticket['id'],'child');queue.finish(ticket['id'],False)
            ticket=queue.claim(now=10**12);self.assertEqual(ticket['attempts'],2)
            queue.dispatched(ticket['id'],'child2');queue.finish(ticket['id'],False)
            self.assertEqual(queue.rows()[0]['state'],'exhausted');self.assertIsNone(queue.claim(now=10**12))
    def test_duplicate_offer_and_permanent_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            queue=RetryQueue(EventStore(Path(directory)/'db'))
            queue.offer({'id':'run'},'suite',blocked=True);queue.offer({'id':'run'},'suite')
            self.assertEqual(len(queue.rows()),1);self.assertIsNone(queue.claim(now=10**12))
            self.assertTrue(blocked({'exit_code':137}));self.assertFalse(blocked({'exit_code':1}))

    def test_retry_pins_parent_commits_and_prefers_another_worker(self):
        from live_distributed import LiveDistributedTests
        from github_sources import source_digest
        from types import SimpleNamespace
        from unittest.mock import Mock
        service=LiveDistributedTests.__new__(LiveDistributedTests)
        runner=SimpleNamespace(run={},save=Mock())
        service.create_run=Mock(return_value=runner)
        repos={'.':{'url':'https://github.com/example/repo.git','commit':'a'*40}}
        parent={'id':'parent','repositories':repos,'source_sha256':source_digest(repos),'hosts':[{'host':'failed-peer','jobs':{'monitor--one':{'suite':'monitor','file':'tests/test_agent_sessions.py'}}}]}
        result=service.retry_pinned(parent,{'task':'monitor--one','id':'parent:monitor--one','attempts':1})
        self.assertEqual(result.run['pinned_repositories'],repos)
        self.assertEqual(result.run['retry_attempt'],2)
        self.assertEqual(result.run['avoid_workers'],['failed-peer'])
        self.assertEqual(service.create_run.call_args.kwargs['selection']['suites'],[])
        self.assertIn('test_agent_sessions.py',service.create_run.call_args.kwargs['selection']['paths'][0])
        parent['source_sha256']='invalid';service.create_run.reset_mock()
        with self.assertRaises(ValueError):service.retry_pinned(parent,{'task':'monitor--one','id':'t','attempts':1})
        service.create_run.assert_not_called()
