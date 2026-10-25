import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from message_queue import MessageQueue
import codex

class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.q=MessageQueue(Path(self.temp.name)/'messages.sqlite')
        p=patch.object(codex,'thread',side_effect=lambda ident:{'id':ident});p.start();self.addCleanup(p.stop)
    def body(self,n=1,thread='a'):return {'request_id':f'{n:08d}-1111-4111-8111-111111111111','thread_id':thread,'text':'Task '+str(n)}
    def states(self):return [r['status'] for r in self.q.view('a')['messages']]
    def test_fifo_waits_for_idle_and_does_not_block_other_threads(self):
        self.q.submit(self.body(1));self.q.submit(self.body(2));self.q.submit(self.body(3,'b'))
        with patch.object(codex,'detail',side_effect=lambda ident:{'activity':'running' if ident=='a' else 'idle','owner_connected':True}),patch.object(codex,'mutate',return_value={'accepted':True}) as send:
            self.q.tick();self.assertEqual([x.args[0]['thread_id'] for x in send.call_args_list],['b'])
        with patch.object(codex,'detail',return_value={'activity':'idle','owner_connected':True}),patch.object(codex,'mutate',return_value={'accepted':True}) as send:
            self.q.tick();self.q.tick();self.assertEqual([x.args[0]['text'] for x in send.call_args_list],['Task 1','Task 2'])
    def test_duplicate_ids_are_idempotent_but_changed_payload_rejected(self):
        self.q.submit(self.body());self.q.submit(self.body());self.assertEqual(len(self.states()),1)
        with self.assertRaises(ValueError):self.q.submit({**self.body(),'text':'different'})
    def test_queue_survives_reload_and_disconnection(self):
        self.q.submit(self.body());self.q=MessageQueue(self.q.path)
        with patch.object(codex,'detail',return_value={'activity':'idle','owner_connected':False}),patch.object(codex,'mutate') as send:self.q.tick();send.assert_not_called()
        self.assertEqual(self.states(),['queued'])
    def test_ambiguous_delivery_blocks_following_messages_until_reviewed(self):
        self.q.submit(self.body());self.q.submit(self.body(2))
        with patch.object(codex,'detail',return_value={'activity':'idle','owner_connected':True}),patch.object(codex,'mutate',side_effect=OSError('timeout')) as send:
            self.q.tick();self.q.tick();self.assertEqual(send.call_count,1)
        self.assertEqual(self.states(),['queued','uncertain'])
        self.q.cancel({'id':self.body()['request_id']});self.assertEqual(self.states(),['queued','reviewed'])
    def test_cancel_prevents_delivery_and_queue_cannot_execute_arbitrary_commands(self):
        self.q.submit(self.body());self.q.cancel({'id':self.body()['request_id']})
        with patch.object(codex,'mutate') as send:self.q.tick();send.assert_not_called()
        with self.assertRaises(ValueError):self.q.submit({**self.body(2),'action':'steer'})
        with self.assertRaises(ValueError):self.q.submit({**self.body(2),'shell':'echo x'})
    def test_second_reader_does_not_reset_active_dispatch(self):
        self.q.submit(self.body())
        with self.q.db() as db:db.execute("UPDATE messages SET status='delivering'")
        other=MessageQueue(self.q.path);self.assertEqual(other.view('a')['messages'][0]['status'],'delivering')

    def test_restart_marks_inflight_uncertain_and_blocks_retry(self):
        import message_queue
        self.q.submit(self.body())
        with self.q.db() as db:db.execute("UPDATE messages SET status='delivering'")
        with patch.object(message_queue,'_QUEUE',self.q),patch.object(message_queue,'_WORKER_FILE',None),patch('message_queue.threading.Thread'):
            message_queue.manager(start_worker=True)
            try:self.assertEqual(self.states(),['uncertain'])
            finally:message_queue._WORKER_FILE.close()
        with patch.object(codex,'mutate') as send:self.q.tick();send.assert_not_called()

    def test_cancellation_during_readiness_check_wins_before_dispatch(self):
        self.q.submit(self.body())
        def state(ident):
            self.q.cancel({'id':self.body()['request_id']})
            return {'activity':'idle','owner_connected':True}
        with patch.object(codex,'detail',side_effect=state),patch.object(codex,'mutate') as send:
            self.q.tick();send.assert_not_called()
        self.assertEqual(self.states(),['cancelled'])
