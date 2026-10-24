#!/usr/bin/env python3
"""Import one organization's testing data from a verified local DR snapshot."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import datetime as dt

from recovery import ROOT, DEFAULT_STORE, ADMIN_ID, command, resolve_snapshot, digest, restore_sql

TABLES = ['organizations','group_sites','user_contact_pages','organization_memberships',
          'organization_personnel_schemes','organization_volunteer_awards','organization_personnel_motions',
          'organization_personnel_votes','organization_invitations','organization_personnel_relationships',
          'events','event_registrations']


def inspect_target(name):
    context=json.loads(command(['docker','context','inspect']).decode())[0]
    if not os.environ.get('DOCKER_HOST',context['Endpoints']['docker']['Host']).startswith('unix://'):
        raise RuntimeError('Testing copies require a host-local Docker socket')
    target=json.loads(command(['docker','inspect',name]).decode())[0]
    mounts=target['Mounts']
    if not target['State']['Running'] or not any(m['Destination']=='/app' and Path(m['Source']).resolve()==ROOT/'OrgPortal/org-worker' for m in mounts):
        raise RuntimeError('Target must be this workspace\'s running local organization worker')
    if not any(m['Destination']=='/app/.wrangler' for m in mounts):raise RuntimeError('Target lacks persistent local storage')
    cmd=' '.join(target['Config'].get('Cmd') or [])
    if 'wrangler dev --local' not in cmd or 'ORGANIZATION_REPLICA_SOURCE:https://' not in cmd:
        raise RuntimeError('Private testing requires a local read replica; outbound production jobs must stay disabled')
    port=re.search(r'--port\s+(\d+)',cmd)
    if not port:raise RuntimeError('Could not determine the local worker port')
    probe="const r=await fetch('http://127.0.0.1:"+port[1]+"/api/network/replication/status',{redirect:'error',signal:AbortSignal.timeout(5000)});const s=await r.json();if(!r.ok||s.mode!=='replica'||!s.source)throw Error('Replica mode is required');"
    command(['docker','exec',name,'node','--input-type=module','-e',probe],timeout=15)
    return target


def create_payload(db,slug,private,manifest_hash):
    db.row_factory=sqlite3.Row
    org=db.execute('SELECT * FROM organizations WHERE slug=? OR id=?',(slug,slug)).fetchall()
    if len(org)!=1:raise RuntimeError('Choose an exact organization slug or ID from the archive')
    ident=org[0]['id'];tables={'organizations':[dict(org[0])]}
    queries={
      'group_sites':('organization_id=?',(ident,)),
      'organization_memberships':('organization_id=?',(ident,)),
      'organization_personnel_schemes':('organization_id=?',(ident,)),
      'organization_volunteer_awards':('organization_id=?',(ident,)),
      'organization_personnel_motions':('organization_id=?',(ident,)),
      'organization_personnel_votes':('motion_id IN (SELECT id FROM organization_personnel_motions WHERE organization_id=?)',(ident,)),
      'organization_invitations':('organization_id=?',(ident,)),
      'organization_personnel_relationships':('organization_id=?',(ident,)),
      'user_contact_pages':("enabled=1 AND user_id IN (SELECT user_id FROM organization_memberships WHERE organization_id=? UNION SELECT user_id FROM organization_personnel_relationships WHERE organization_id=?)",(ident,ident)),
      'events':('host_org_id=?',(ident,)),
      'event_registrations':('event_id IN (SELECT id FROM events WHERE host_org_id=?)',(ident,)),
    }
    public={'group_sites','events'}
    for table in TABLES[1:]:
        if not private and table not in public:continue
        where,params=queries[table]
        rows=[dict(row) for row in db.execute('SELECT * FROM '+table+' WHERE '+where,params)]
        if table=='user_contact_pages':
            # Organization management never authorizes private contact-profile fields.
            allowed={'id','user_id','user_name','slug','enabled','headline','bio','photo_url','email_public','phone_public','linkedin_url','github_url','x_url','website_url','links','created_at','updated_at'}
            rows=[{k:v for k,v in row.items() if k in allowed} for row in rows]
        tables[table]=rows
    if private:
        tables['organization_memberships'].append({'organization_id':ident,'user_id':ADMIN_ID,'user_name':'Local Administrator',
            'user_email':'administrator@local.orgportal.example.com','role':'administrator','status':'active',
            'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'updated_at':dt.datetime.now(dt.timezone.utc).isoformat()})
    return {'version':1,'scope':ident,'sourceManifestSha256':manifest_hash,
            'expiresAt':(dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=7)).isoformat(),'tables':tables,'private':private}


def archive_payload(snapshot, manifest, org, private):
    entries=[(ident,entry) for ident,entry in manifest['databases'].items() if entry['database_name']=='org']
    if len(entries)!=1:raise RuntimeError('Archive must contain one organization database')
    ident,entry=entries[0];relative='databases/'+ident+'/export.sql.gz';source=snapshot/relative
    if digest(source)!=manifest['files'][relative]:raise RuntimeError('Archive database hash mismatch')
    with tempfile.TemporaryDirectory(prefix='organization-test-source-') as folder:
        restored=Path(folder)/'source.sqlite'
        check=restore_sql(source,restored)
        if check['tables']!=entry['tables'] or check['foreignKeyViolations']!=entry['foreignKeyViolations']:
            raise RuntimeError('Restored database differs from captured database')
        db=sqlite3.connect('file:'+str(restored)+'?mode=ro',uri=True)
        try:return create_payload(db,org,private,digest(snapshot/'manifest.json'))
        finally:db.close()


def main():
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--store',type=Path,default=Path(os.environ.get('ORGANIZATION_DR_DIR',DEFAULT_STORE)))
    p.add_argument('--from',dest='snapshot');p.add_argument('--org');p.add_argument('--include-private',action='store_true')
    p.add_argument('--container',default='local-community-api');p.add_argument('--dry-run',action='store_true')
    p.add_argument('--purge-expired',action='store_true')
    args=p.parse_args();inspect_target(args.container)
    if args.purge_expired:
        print(command(['docker','exec','-w','/app',args.container,'node','--input-type=module','-e',IMPORTER,'purge']).decode().strip());return
    if not args.org:p.error('--org is required')
    snapshot=resolve_snapshot(args.store,args.snapshot);manifest=json.loads((snapshot/'manifest.json').read_text())
    if manifest.get('version')!=1 or not manifest.get('completedAt'):raise RuntimeError('Archive is incomplete')
    payload=archive_payload(snapshot,manifest,args.org,args.include_private)
    plan={table:len(rows) for table,rows in payload['tables'].items()}
    print(json.dumps({'organization':args.org,'private':args.include_private,'rows':plan,'dryRun':args.dry_run}),flush=True)
    if args.dry_run:return
    command(['python3',ROOT/'OrgPortal/scripts/backup-local-storage.py','--container',args.container])
    command(['docker','exec','-e','CI=true','-w','/app',args.container,'npm','run','db:migrate:local'])
    with tempfile.TemporaryDirectory(prefix='organization-test-copy-') as folder:
        path=Path(folder)/'snapshot.json';path.write_text(json.dumps(payload));path.chmod(0o600)
        remote='/tmp/local-testing-'+os.urandom(12).hex()+'.json'
        command(['docker','cp',path,args.container+':'+remote])
        try:print(command(['docker','exec','-w','/app',args.container,'node','--input-type=module','-e',IMPORTER,remote]).decode().strip())
        finally:command(['docker','exec',args.container,'rm','-f',remote])


# A single SQLite transaction is safe alongside the running local workerd process.
# Snapshot data is parameter-bound, never interpolated into SQL or shell commands.
IMPORTER=r'''
import {DatabaseSync} from 'node:sqlite';
import fs from 'node:fs';import path from 'node:path';import {createHash} from 'node:crypto';
const allowed=['organizations','group_sites','user_contact_pages','organization_memberships','organization_personnel_schemes','organization_volunteer_awards','organization_personnel_motions','organization_personnel_votes','organization_invitations','organization_personnel_relationships','events','event_registrations'];
const quote=v=>'"'+v.replaceAll('"','""')+'"';
const hash=row=>createHash('sha256').update(JSON.stringify(Object.fromEntries(Object.entries(row).sort(([a],[b])=>a.localeCompare(b))))).digest('hex');
function files(dir){return fs.readdirSync(dir,{withFileTypes:true}).flatMap(e=>e.isDirectory()?files(path.join(dir,e.name)):e.name.endsWith('.sqlite')?[path.join(dir,e.name)]:[])}
const candidates=files('/app/.wrangler/state/v3/d1').filter(file=>{const db=new DatabaseSync(file,{readOnly:true});try{return !!db.prepare("SELECT 1 FROM sqlite_master WHERE name='organization_memberships'").get()}finally{db.close()}});
if(candidates.length!==1)throw Error('Expected one local organization database');
fs.chmodSync(path.dirname(candidates[0]),0o700);
for(const suffix of ['', '-wal', '-shm'])if(fs.existsSync(candidates[0]+suffix))fs.chmodSync(candidates[0]+suffix,0o600);
const db=new DatabaseSync(candidates[0]);db.exec('PRAGMA foreign_keys=ON; PRAGMA busy_timeout=10000');
const action=process.argv[1];let payload;
if(action!=='purge'){
 payload=JSON.parse(fs.readFileSync(action,'utf8'));
 if(payload.version!==1||typeof payload.scope!=='string'||!Number.isFinite(Date.parse(payload.expiresAt))||Date.parse(payload.expiresAt)<=Date.now()||Object.keys(payload.tables).some(t=>!allowed.includes(t)))throw Error('Invalid or expired testing snapshot');
}
db.exec(`CREATE TABLE IF NOT EXISTS local_test_snapshots(scope TEXT PRIMARY KEY,source_hash TEXT NOT NULL,expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS local_test_snapshot_rows(scope TEXT NOT NULL,table_name TEXT NOT NULL,key_json TEXT NOT NULL,row_hash TEXT NOT NULL,PRIMARY KEY(scope,table_name,key_json));`);
function existing(table,key){const names=Object.keys(key);return db.prepare('SELECT * FROM '+quote(table)+' WHERE '+names.map(k=>quote(k)+'=?').join(' AND ')).get(...names.map(k=>key[k]))}
function removeOwned(scope){
 const owned=db.prepare('SELECT * FROM local_test_snapshot_rows WHERE scope=?').all(scope).sort((a,b)=>allowed.indexOf(b.table_name)-allowed.indexOf(a.table_name));
 for(const row of owned){
  if(!allowed.includes(row.table_name))throw Error('Unknown tracked table');
  const key=JSON.parse(row.key_json),current=existing(row.table_name,key);
  if(current&&hash(current)!==row.row_hash)throw Error('Local edits conflict with snapshot refresh; back up and resolve them first');
 }
 for(const row of owned){
  // Public organization configuration remains under the public replica's policy.
  if(['organizations','group_sites'].includes(row.table_name))continue;
  const key=JSON.parse(row.key_json),names=Object.keys(key);
  if(row.table_name==='user_contact_pages'){
   const profile=existing(row.table_name,key);
   if(profile&&db.prepare('SELECT 1 FROM organization_memberships WHERE user_id=? LIMIT 1').get(profile.user_id))continue;
   // Preserve independent local profile data rather than cascading its deletion.
   if(profile)for(const t of db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all()){
    for(const fk of db.prepare('PRAGMA foreign_key_list('+quote(t.name)+')').all()){
     if(fk.table==='user_contact_pages'&&profile[fk.to]!==undefined&&db.prepare('SELECT 1 FROM '+quote(t.name)+' WHERE '+quote(fk.from)+'=? LIMIT 1').get(profile[fk.to]))throw Error('Local profile references prevent snapshot cleanup; resolve them first');
    }
   }
  }
  db.prepare('DELETE FROM '+quote(row.table_name)+' WHERE '+names.map(k=>quote(k)+'=?').join(' AND ')).run(...names.map(k=>key[k]));
 }
 db.prepare('DELETE FROM local_test_snapshot_rows WHERE scope=?').run(scope);
 db.prepare('DELETE FROM local_test_snapshots WHERE scope=?').run(scope);
}
try{
 db.exec('BEGIN IMMEDIATE; PRAGMA defer_foreign_keys=ON');
 // Replica imports/expiry are not new business events. Suppress journal triggers
 // transactionally so temporary personal data does not enter immutable history.
 const quiet=db.prepare("SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'journal_%'").all().filter(t=>allowed.includes(t.tbl_name));
 for(const trigger of quiet)db.exec('DROP TRIGGER '+quote(trigger.name));
 const restoreTriggers=()=>{for(const trigger of quiet)db.exec(trigger.sql)};
 if(action==='purge'){
  const expired=db.prepare('SELECT scope FROM local_test_snapshots WHERE expires_at<=?').all(new Date().toISOString());
  for(const row of expired)removeOwned(row.scope);
  restoreTriggers();db.exec('COMMIT');console.log(JSON.stringify({expiredScopesRemoved:expired.length}));
 }else{
  removeOwned(payload.scope);
  for(const table of allowed){
   for(const row of payload.tables[table]||[]){
    if(row.organization_id&&row.organization_id!==payload.scope)throw Error('Cross-organization row');
    if(table==='organizations'&&row.id!==payload.scope)throw Error('Cross-organization root');
    if(table==='events'&&row.host_org_id!==payload.scope)throw Error('Cross-organization event');
    const schema=db.prepare('PRAGMA table_info('+quote(table)+')').all(),columns=new Set(schema.map(c=>c.name));
    const names=Object.keys(row),keys=schema.filter(c=>c.pk).sort((a,b)=>a.pk-b.pk).map(c=>c.name);
    if(!keys.length||keys.some(k=>row[k]===undefined)||names.some(n=>!columns.has(n)))throw Error('Source and local schemas differ; apply supported migrations');
    const updates=names.filter(k=>!keys.includes(k));
    const sql='INSERT INTO '+quote(table)+' ('+names.map(quote).join(',')+') VALUES ('+names.map(()=>'?').join(',')+') ON CONFLICT ('+keys.map(quote).join(',')+') '+(updates.length?'DO UPDATE SET '+updates.map(k=>quote(k)+'=excluded.'+quote(k)).join(','):'DO NOTHING');
    db.prepare(sql).run(...names.map(n=>row[n]));
    const key=Object.fromEntries(keys.map(k=>[k,row[k]])),current=existing(table,key);
    db.prepare('INSERT INTO local_test_snapshot_rows VALUES(?,?,?,?)').run(payload.scope,table,JSON.stringify(key),hash(current));
   }
  }
  db.prepare('INSERT INTO local_test_snapshots VALUES(?,?,?)').run(payload.scope,payload.sourceManifestSha256,payload.expiresAt);
  restoreTriggers();db.exec('COMMIT');console.log(JSON.stringify({imported:true,scope:payload.scope,expiresAt:payload.expiresAt,productionWrites:false}));
 }
}catch(error){try{db.exec('ROLLBACK')}catch{}throw error}finally{db.close()}
'''


if __name__=='__main__':
    try:main()
    except Exception as e:
        print('Testing copy failed ('+type(e).__name__+'); private output withheld. Existing local backup was retained.',file=sys.stderr)
        sys.exit(1)
