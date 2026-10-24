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
import public_data


class PeerArchiveTests(unittest.TestCase):
    def test_public_packages_reject_private_projections_and_extra_manifest_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'public.json'
            value={'version':3,**{name:[] for name in public_data.FIELDS}}
            source.write_text(json.dumps({**value,'memberships':[{'email':'private@example.invalid'}]}))
            with self.assertRaises(ValueError):peer_archive.package_file(source,root/'rejected','public')
            self.assertFalse((root/'rejected').exists())
            source.write_text(json.dumps(value));out=root/'package'
            peer_archive.package_file(source,out,'public')
            manifest,raw,snapshot,chunks=public_data.load_package(out)
            self.assertEqual(json.loads(snapshot),value)
            manifest['credentials']='must never be served'
            (out/'manifest.json').write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):public_data.load_package(out)

    def test_public_packages_reject_corrupt_or_symlinked_chunks(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'public.json'
            source.write_text(json.dumps({'version':3,**{name:[] for name in public_data.FIELDS}}))
            out=root/'package';peer_archive.package_file(source,out,'public')
            chunk=next((out/'chunks').iterdir());original=chunk.read_bytes()
            chunk.write_bytes(b'x'*len(original))
            with self.assertRaises(ValueError):public_data.load_package(out)
            chunk.unlink();chunk.symlink_to(source)
            with self.assertRaises(ValueError):public_data.load_package(out)

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
