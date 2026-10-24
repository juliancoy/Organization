import base64
import hashlib
import hmac
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import peer_archive


class PeerArchiveTests(unittest.TestCase):
    def test_packages_preserve_all_bytes_and_deduplicate_repeated_chunks(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'testing.json';source.write_bytes(b'x'*peer_archive.CHUNK_SIZE*2+b'end')
            out=root/'package';room=peer_archive.package_file(source,out,'testing')
            self.assertEqual(hashlib.sha256((out/'manifest.json').read_bytes()).hexdigest(),room)
            m=json.loads((out/'manifest.json').read_text());file=m['files'][0]
            self.assertEqual(len(list((out/'chunks').iterdir())),2)
            self.assertEqual(b''.join((out/'chunks'/part['hash']).read_bytes() for part in file['chunks']),source.read_bytes())
            self.assertEqual((out/'manifest.json').stat().st_mode&0o777,0o600)
            with self.assertRaises(RuntimeError):peer_archive.package_file(source,out,'testing')

    def test_plaintext_dr_and_unbounded_device_sessions_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'production.tar';source.write_bytes(b'private account data')
            with self.assertRaises(RuntimeError):peer_archive.package_file(source,root/'package','encrypted-dr')
            self.assertFalse((root/'package').exists())
        with self.assertRaises(RuntimeError):peer_archive.ticket('x'*48,'a'*64,3601)

    def test_device_ticket_contains_only_bound_dataset_device_and_expiry(self):
        secret='synthetic-signing-key-at-least-32-characters'
        claims,token=peer_archive.ticket(secret,'a'*64,300)
        header,body,signature=token.split('.')
        self.assertEqual(json.loads(base64.urlsafe_b64decode(body+'==')),claims)
        expected=base64.urlsafe_b64encode(hmac.new(secret.encode(),(header+'.'+body).encode(),hashlib.sha256).digest()).rstrip(b'=').decode()
        self.assertEqual(signature,expected)
        self.assertEqual(claims['exp']-claims['iat'],300)


if __name__=='__main__':unittest.main()
