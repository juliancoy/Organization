import concurrent.futures
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import codex_devices as devices

class DevicePairingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'devices.json'
        p=patch.object(devices,'FILE',self.path);p.start();self.addCleanup(p.stop)
    def test_pair_once_device_survives_reload_and_can_be_revoked(self):
        pair=devices.issue();session=devices.session(pair)
        self.assertTrue(devices.valid(session))
        with self.assertRaises(ValueError):devices.session(pair)
        stored=self.path.read_text();self.assertNotIn(pair,stored);self.assertNotIn(session,stored)
        self.assertEqual(self.path.stat().st_mode&0o777,0o600)
        devices.revoke(session);self.assertFalse(devices.valid(session))
    def test_expired_pair_cannot_authorize_device(self):
        pair=devices.issue()
        with patch('codex_devices.time.time',return_value=time.time()+601):
            with self.assertRaises(ValueError):devices.session(pair)
    def test_only_one_concurrent_redemption_succeeds(self):
        pair=devices.issue()
        def redeem(_):
            try:return bool(devices.session(pair))
            except ValueError:return False
        with concurrent.futures.ThreadPoolExecutor(2) as pool:self.assertEqual(sum(pool.map(redeem,range(2))),1)
    def test_new_link_invalidates_old_link_and_revoke_all_removes_devices(self):
        old=devices.issue();new=devices.issue()
        with self.assertRaises(ValueError):devices.session(old)
        session=devices.session(new);devices.revoke();self.assertFalse(devices.valid(session))

    def test_null_and_malformed_pair_tokens_never_create_sessions(self):
        for value in [None,False,0,{},[],"",'invalid']:
            with self.subTest(token_type=type(value).__name__),self.assertRaises(ValueError):devices.session(value)
        self.assertFalse(self.path.exists())

    def test_new_pairing_atomically_replaces_the_only_remote_session(self):
        first=devices.session(devices.issue())
        next_pair=devices.issue()
        self.assertTrue(devices.valid(first))
        second=devices.session(next_pair)
        self.assertFalse(devices.valid(first))
        self.assertTrue(devices.valid(second))
        self.assertEqual(len(json.loads(self.path.read_text())['sessions']),1)
