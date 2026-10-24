import importlib.util
import datetime as dt
import io
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
import recovery
import testing_copy


class RecoveryTests(unittest.TestCase):
    def test_listing_retries_partial_pages_without_duplicates(self):
        calls=[]
        class Pages:
            def paginate(self,**kwargs):
                calls.append(1)
                yield {'Contents':[{'Key':'first'}]}
                if len(calls)==1:raise RuntimeError('Temporary listing failure')
                yield {'Contents':[{'Key':'second'}]}
        storage=SimpleNamespace(get_paginator=lambda name:Pages())
        with patch.object(recovery.time,'sleep'):
            self.assertEqual(recovery.list_objects(storage,'bucket'),[{'Key':'first'},{'Key':'second'}])
        self.assertEqual(len(calls),2)

    def test_backup_publishes_only_after_drill_and_resumes_without_redownloading(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Path(folder)/'archive';calls={'exports':0,'downloads':0,'drills':0}
            inv={'databases':{'db':{'database_name':'org','database_id':'db','config':'OrgPortal/org-worker/wrangler.jsonc','environment':None}},'buckets':['unit-bucket'],'workers':{'unit-worker':{'config':'OrgPortal/org-worker/wrangler.jsonc'}},'otherBindings':[]}
            objects={'one':b'first body','two':b'second body'}
            items=[{'Key':key,'Size':len(body),'ETag':'"'+key+'"','LastModified':dt.datetime.now(dt.timezone.utc)} for key,body in objects.items()]
            class Pages:
                def paginate(self,**kwargs):return [{'Contents':items}]
            class Storage:
                def get_paginator(self,*args):return Pages()
                def get_object(self,**kwargs):
                    calls['downloads']+=1
                    return {'Body':io.BytesIO(objects[kwargs['Key']]),'Metadata':{},'ContentType':'application/octet-stream'}
            def execute(args,**kwargs):
                calls['exports']+=1
                Path(args[args.index('--output')+1]).write_text("CREATE TABLE people(id TEXT);INSERT INTO people VALUES('one');")
                return b''
            def sources(target,repositories=None):
                p=target/'sources';p.mkdir(exist_ok=True);(p/'source.txt').write_text('source')
                return [{'repository':'Organization','commit':'test','dirty':False}],set()
            real_verify=recovery.verify_snapshot
            def drill(snapshot,root):
                calls['drills']+=1
                if calls['drills']==1:raise RuntimeError('Injected restore drill failure')
                return real_verify(snapshot,root)
            def cloud(account,path,raw=False):
                if path.endswith('/deployments'):return {'deployments':[{'versions':[{'version_id':'synthetic','percentage':100}]}]}
                return b'export default {}' if raw else {'bindings':[]}
            args=SimpleNamespace(store=store,resume=None,secrets_file=None)
            with patch.object(recovery,'inventory',return_value=inv),patch.object(recovery,'environment',return_value={'R2_ACCOUNT_ID':'test','R2_ACCESS_KEY_ID':'dummy','R2_SECRET_ACCESS_KEY':'dummy'}),patch.object(recovery,'capture_sources',side_effect=sources),patch.object(recovery,'capture_local',return_value={}),patch.object(recovery,'command',side_effect=execute),patch.object(recovery,'wrangler',return_value=['fake-wrangler']),patch.object(recovery,'cf_get',side_effect=cloud),patch('boto3.client',return_value=Storage()),patch.object(recovery,'verify_snapshot',side_effect=drill),patch('builtins.print'):
                with self.assertRaisesRegex(RuntimeError,'Injected'):recovery.backup(args)
                self.assertFalse((store/'latest.json').exists())
                args.resume=next(store.glob('.pending-*')).name
                recovery.backup(args)
            self.assertEqual(calls['exports'],1)
            self.assertEqual(calls['downloads'],2)
            latest=json.loads((store/'latest.json').read_text())
            self.assertEqual(latest['status'],'complete')
            snapshot=store/latest['snapshot']
            self.assertEqual(real_verify(snapshot,store)['databases']['db']['tables'],{'people':1})

    def test_secret_proof_requires_every_current_value_and_exact_active_version(self):
        required=['ENCRYPTION_KEY','SIGNING_KEY']
        values={name:'synthetic-'+name for name in required}
        proof={'sourceVersion':'active','fingerprints':{name:hashlib.sha256(value.encode()).hexdigest() for name,value in values.items()}}
        self.assertTrue(recovery.verified_secret_escrow(required,values,proof,'active'))
        self.assertFalse(recovery.verified_secret_escrow(required,values,proof,'new-version'))
        self.assertFalse(recovery.verified_secret_escrow(required,{**values,'SIGNING_KEY':'changed'},proof,'active'))
        self.assertFalse(recovery.verified_secret_escrow([*required,'MISSING'],values,proof,'active'))

    def test_encrypted_recovery_interoperates_with_webcrypto_and_denies_missing_auth(self):
        import base64
        import recover_production_secrets as module
        key=module.rsa.generate_private_key(public_exponent=65537,key_size=2048)
        numbers=key.public_key().public_numbers()
        encode=lambda value:base64.urlsafe_b64encode(value.to_bytes((value.bit_length()+7)//8,'big')).decode().rstrip('=')
        public={'kty':'RSA','n':encode(numbers.n),'e':encode(numbers.e),'alg':'RSA-OAEP-256','ext':True}
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'original.mjs').write_text("export default {fetch(){return new Response('original')}}")
            (root/'recovery.mjs').write_bytes(module.wrapper('original.mjs','fixture',['KEY'],public,list(hashlib.sha256(b'Bearer fixture').digest()),2000000000000,'challenge'))
            (root/'check.mjs').write_text('''import worker from './recovery.mjs';
const env={KEY:'synthetic-secret',ESCROW_VERSION_METADATA:{id:'fixture-version'}};
const url='https://example.test/__operator_encrypted_escrow';
const denied=await worker.fetch(new Request(url,{method:'POST'}),env,{});
if(denied.status!==404)throw Error('Unauthenticated request accepted');
const allowed=await worker.fetch(new Request(url,{method:'POST',headers:{Authorization:'Bearer fixture'}}),env,{});
console.log(await allowed.text());''')
            result=json.loads(subprocess.check_output(['node',str(root/'check.mjs')],text=True))
            self.assertNotIn('synthetic-secret',json.dumps(result))
            raw=key.decrypt(base64.b64decode(result['wrappedKey']),module.padding.OAEP(mgf=module.padding.MGF1(module.hashes.SHA256()),algorithm=module.hashes.SHA256(),label=None))
            values=module.AESGCM(raw).decrypt(base64.b64decode(result['iv']),base64.b64decode(result['ciphertext']),json.dumps(result['identity'],separators=(',',':')).encode())
            self.assertEqual(json.loads(values),{'KEY':'synthetic-secret'})
            self.assertEqual(result['identity'],{'worker':'fixture','version':'fixture-version','nonce':'challenge'})

    def test_jsonc_preserves_urls_strings_and_comments(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'wrangler.jsonc'
            p.write_text('{/* x */ "url":"https://a.test/x//y", "text":"\\\"//", // comment\n "list":[1,2,],}')
            self.assertEqual(recovery.config_file(p),{'url':'https://a.test/x//y','text':'"//','list':[1,2]})

    def fixture(self,root):
        snapshot=root/'sample';snapshot.mkdir();folder=snapshot/'databases'/'db';folder.mkdir(parents=True)
        (snapshot/'sources').mkdir()
        sql=folder/'export.sql';sql.write_text("CREATE TABLE people(id TEXT PRIMARY KEY,name TEXT); INSERT INTO people VALUES('one','Private Person');")
        check=recovery.restore_sql(sql,folder/'restored.sqlite')
        recovery.compress_sql(sql,folder/'export.sql.gz');sql.unlink();(folder/'restored.sqlite').unlink()
        blob=root/'objects';blob.mkdir();(blob/'placeholder').write_bytes(b'media')
        sha=recovery.digest(blob/'placeholder');(blob/'placeholder').rename(blob/sha)
        manifest={'version':1,'completedAt':'2026-10-23T00:00:00Z','status':'complete',
                  'inventory':{'databases':{'db':{}},'buckets':['images']},
                  'databases':{'db':check},'objects':{'images':[{'sha256':sha,'size':5,'key':'../../outside'}]},
                  'files':{str(p.relative_to(snapshot)):recovery.digest(p) for p in folder.iterdir()},'gaps':[]}
        recovery.private_json(snapshot/'manifest.json',manifest)
        return snapshot,manifest,sha

    def test_restore_drill_rebuilds_database_and_checks_media(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);snapshot,manifest,sha=self.fixture(root)
            self.assertEqual(recovery.verify_snapshot(snapshot,root)['databases']['db']['tables'],{'people':1})
            (root/'objects'/sha).write_bytes(b'wrong')
            with self.assertRaisesRegex(RuntimeError,'media hash'):recovery.verify_snapshot(snapshot,root)

    def test_incomplete_coverage_and_tampered_database_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);snapshot,manifest,_=self.fixture(root)
            manifest['inventory']['databases']['missing']={}
            recovery.private_json(snapshot/'manifest.json',manifest)
            with self.assertRaisesRegex(RuntimeError,'inventory'):recovery.verify_snapshot(snapshot,root)
            del manifest['inventory']['databases']['missing'];recovery.private_json(snapshot/'manifest.json',manifest)
            (snapshot/'databases/db/export.sql.gz').write_text('SELECT 1;')
            with self.assertRaisesRegex(RuntimeError,'hash mismatch'):recovery.verify_snapshot(snapshot,root)

    def test_paths_and_private_manifest_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with self.assertRaises(ValueError):recovery.safe_child(root,'../escape')
            recovery.private_json(root/'manifest.json',{'private':True})
            self.assertEqual((root/'manifest.json').stat().st_mode&0o777,0o600)

    def test_compressed_export_is_deterministic_and_restores_without_retained_sqlite(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);sql=root/'export.sql'
            sql.write_text('CREATE TABLE data(value TEXT);'+"INSERT INTO data VALUES('repeat repeat repeat');"*500)
            first=root/'one.sql.gz';second=root/'two.sql.gz'
            recovery.compress_sql(sql,first);recovery.compress_sql(sql,second)
            self.assertEqual(first.read_bytes(),second.read_bytes())
            self.assertLess(first.stat().st_size,sql.stat().st_size//10)
            self.assertEqual(recovery.restore_sql(first,root/'restored.sqlite')['tables'],{'data':500})

    def test_identical_files_share_storage_without_changing_either_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);old=root/'completed';old.mkdir();new=root/'.pending-new';new.mkdir()
            (old/'same').write_bytes(b'unchanged');(new/'same').write_bytes(b'unchanged');(new/'different').write_bytes(b'new')
            sha=recovery.digest(old/'same')
            recovery.private_json(old/'manifest.json',{'completedAt':'now','files':{'same':sha}})
            saved=recovery.deduplicate_files(new,root,{'same':sha,'different':recovery.digest(new/'different')})
            self.assertEqual(saved,len(b'unchanged'))
            self.assertEqual((old/'same').stat().st_ino,(new/'same').stat().st_ino)
            self.assertEqual((new/'different').read_bytes(),b'new')
            (new/'same').unlink()
            self.assertEqual((old/'same').read_bytes(),b'unchanged')

    def test_testing_copy_restores_compressed_source_and_cleans_temporary_database(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);snapshot,manifest,sha=self.fixture(root)
            manifest['databases']['db']['database_name']='org'
            with patch.object(testing_copy,'create_payload',side_effect=lambda db,*args:db.execute('SELECT name FROM people').fetchone()[0]):
                self.assertEqual(testing_copy.archive_payload(snapshot,manifest,'lifetech',True),'Private Person')
            self.assertFalse(any(snapshot.rglob('*.sqlite')))
            (snapshot/'databases/db/export.sql.gz').write_bytes(b'corrupt')
            with self.assertRaisesRegex(RuntimeError,'hash mismatch'):testing_copy.archive_payload(snapshot,manifest,'lifetech',True)

    def test_restore_keeps_original_object_keys_in_index_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);snapshot,manifest,sha=self.fixture(root);target=root/'restore'
            args=SimpleNamespace(store=root,snapshot='sample',target=target)
            with patch('builtins.print'):recovery.restore(args)
            self.assertEqual((target/'r2/images'/sha).read_bytes(),b'media')
            self.assertEqual(json.loads((target/'r2/images/index.json').read_text())[0]['key'],'../../outside')
            self.assertFalse((root/'outside').exists())
            with self.assertRaisesRegex(RuntimeError,'empty'):recovery.restore(args)

    def test_testing_payload_scopes_members_events_and_public_profiles(self):
        db=sqlite3.connect(':memory:')
        db.executescript('''CREATE TABLE organizations(id TEXT,slug TEXT);INSERT INTO organizations VALUES('a','lifetech'),('b','other');
        CREATE TABLE organization_memberships(organization_id TEXT,user_id TEXT);INSERT INTO organization_memberships VALUES('a','u1'),('b','u2');
        CREATE TABLE group_sites(organization_id TEXT);
        CREATE TABLE organization_personnel_schemes(organization_id TEXT);
        CREATE TABLE organization_volunteer_awards(organization_id TEXT);
        CREATE TABLE organization_personnel_motions(id TEXT,organization_id TEXT);
        CREATE TABLE organization_personnel_votes(motion_id TEXT);
        CREATE TABLE organization_invitations(organization_id TEXT);
        CREATE TABLE organization_personnel_relationships(organization_id TEXT,user_id TEXT);
        CREATE TABLE user_contact_pages(user_id TEXT,enabled INTEGER,user_name TEXT,user_email TEXT,bio TEXT);
        INSERT INTO user_contact_pages VALUES('u1',1,'Public Name','private@example.test','Public bio'),('u2',1,'Other Name','other@example.test','Other bio');
        CREATE TABLE events(id TEXT,host_org_id TEXT);INSERT INTO events VALUES('e1','a'),('e2','b');
        CREATE TABLE event_registrations(event_id TEXT,user_id TEXT);INSERT INTO event_registrations VALUES('e1','attendee1'),('e2','attendee2');''')
        payload=testing_copy.create_payload(db,'lifetech',True,'hash')
        self.assertEqual([r['id'] for r in payload['tables']['events']],['e1'])
        self.assertEqual(payload['tables']['event_registrations'],[{'event_id':'e1','user_id':'attendee1'}])
        self.assertNotIn('user_email',payload['tables']['user_contact_pages'][0])
        self.assertNotIn('Other Name',json.dumps(payload))
        public=testing_copy.create_payload(db,'lifetech',False,'hash')
        self.assertNotIn('organization_memberships',public['tables'])
        db.close()

    def test_import_is_atomic_preserves_unrelated_rows_and_rejects_local_edits(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);dbpath=root/'db.sqlite';db=sqlite3.connect(dbpath)
            db.executescript("CREATE TABLE organizations(id TEXT PRIMARY KEY,slug TEXT);CREATE TABLE organization_memberships(organization_id TEXT,user_id TEXT,user_name TEXT,PRIMARY KEY(organization_id,user_id));INSERT INTO organizations VALUES('other','other');INSERT INTO organization_memberships VALUES('other','other-user','Keep');")
            db.executescript("CREATE TABLE change_journal(payload TEXT);CREATE TRIGGER journal_members_insert AFTER INSERT ON organization_memberships BEGIN INSERT INTO change_journal VALUES(NEW.user_name);END;")
            db.close()
            script=testing_copy.IMPORTER.replace('/app/.wrangler/state/v3/d1',str(root))
            payload={'version':1,'scope':'org','sourceManifestSha256':'hash','expiresAt':'2999-01-01T00:00:00Z',
                     'tables':{'organizations':[{'id':'org','slug':'org'}],
                               'organization_memberships':[{'organization_id':'org','user_id':'u','user_name':'Original'}]}}
            p=root/'input.json';p.write_text(json.dumps(payload))
            def run():return subprocess.run(['node','--input-type=module','-e',script,str(p)],capture_output=True)
            self.assertEqual(run().returncode,0)
            self.assertEqual(run().returncode,0,'repeat import must be idempotent')
            db=sqlite3.connect(dbpath);db.execute("UPDATE organization_memberships SET user_name='Local edit' WHERE user_id='u'");db.commit()
            self.assertEqual(db.execute('SELECT count(*) FROM change_journal').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name='journal_members_insert'").fetchone()[0],1)
            self.assertNotEqual(run().returncode,0)
            self.assertEqual(db.execute("SELECT user_name FROM organization_memberships WHERE user_id='u'").fetchone()[0],'Local edit')
            self.assertEqual(db.execute("SELECT user_name FROM organization_memberships WHERE user_id='other-user'").fetchone()[0],'Keep')
            # Once the conflict is resolved, expiry removes only snapshot-owned records.
            db.execute("UPDATE organization_memberships SET user_name='Original' WHERE user_id='u'")
            db.execute("UPDATE local_test_snapshots SET expires_at='2000-01-01T00:00:00Z'");db.commit();db.close()
            purged=subprocess.run(['node','--input-type=module','-e',script,'purge'],capture_output=True)
            self.assertEqual(purged.returncode,0)
            db=sqlite3.connect(dbpath)
            self.assertEqual(db.execute("SELECT count(*) FROM organization_memberships WHERE user_id='u'").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT user_name FROM organization_memberships WHERE user_id='other-user'").fetchone()[0],'Keep')
            self.assertEqual(db.execute('SELECT count(*) FROM change_journal').fetchone()[0],0)
            db.close()


if __name__=='__main__':unittest.main()
