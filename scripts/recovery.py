#!/usr/bin/env python3
"""Host-only DR archives and scoped, disposable testing copies. Never writes upstream."""
import argparse
import concurrent.futures
import datetime as dt
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
DEFAULT_STORE = Path('/mnt/PILEDRIVE/organization-dr')
REPOS = sorted(p.name for p in ROOT.iterdir() if p.is_dir() and (p/'.git').exists())
CONFIGS = ['OrgPortal/org-worker/wrangler.jsonc', 'OrgPortal/org-worker/journal.wrangler.jsonc',
           'OrgPortal/chat-worker/wrangler.jsonc', 'OrgPortal/cloudflare/wrangler.jsonc',
           'PIdP/serverless/wrangler.jsonc', 'CodeCollective/wrangler.toml',
           'BmoreMedTech/wrangler.jsonc', 'Deism/wrangler.toml', 'peer-worker/wrangler.jsonc']
ADMIN_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, 'orgportal:local-developer-admin'))
TRANSFER_WORKERS = 256


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.pending')
    with open(temporary, 'w', opener=lambda p, flags: os.open(p, flags, 0o600)) as f:
        json.dump(value, f, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    temporary.replace(path)


def command(args, cwd=ROOT, timeout=600):
    result = subprocess.run([str(a) for a in args], cwd=cwd, capture_output=True, timeout=timeout)
    if result.returncode:
        # Subprocess stderr may include SQL rows, signed URLs or credentials.
        raise RuntimeError(f'{Path(str(args[0])).name} failed with status {result.returncode}; private output withheld')
    return result.stdout


def config_file(path):
    if path.suffix == '.toml':
        return tomllib.loads(path.read_text())
    # Remove JSONC comments/trailing commas outside quoted strings, preserving URLs.
    s = path.read_text(); out = []; i = 0; quoted = False
    while i < len(s):
        c = s[i]
        if quoted:
            out.append(c)
            if c == '\\' and i + 1 < len(s):
                i += 1; out.append(s[i])
            elif c == '"':
                quoted = False
        elif c == '"':
            quoted = True; out.append(c)
        elif s[i:i + 2] == '//':
            end = s.find('\n', i); i = len(s) if end < 0 else end; continue
        elif s[i:i + 2] == '/*':
            end = s.find('*/', i + 2)
            if end < 0: raise ValueError('Unclosed JSONC comment')
            i = end + 2; continue
        elif c == ',':
            j = i + 1
            while j < len(s) and s[j].isspace(): j += 1
            if j < len(s) and s[j] in '}]': i += 1; continue
            out.append(c)
        else:
            out.append(c)
        i += 1
    return json.loads(''.join(out))


def inventory():
    databases = {}; buckets = set(); workers = {}; other = []
    for name in CONFIGS:
        path = ROOT / name
        if not path.exists(): continue
        config = config_file(path)
        for env_name, env in [(None, config), *config.get('env', {}).items()]:
            worker_name = env.get('name')
            if worker_name and (env.get('main') or config.get('main')):
                workers[worker_name] = {'config': name, 'environment': env_name}
            for db in env.get('d1_databases', []):
                databases.setdefault(db['database_id'], {**db, 'config': name, 'environment': env_name})
            buckets.update(b['bucket_name'] for b in env.get('r2_buckets', []))
            for kind in ['kv_namespaces', 'durable_objects', 'queues', 'hyperdrive', 'vectorize']:
                if env.get(kind): other.append({'config': name, 'kind': kind, 'bindings': env[kind]})
    return {'databases': databases, 'buckets': sorted(buckets), 'workers': workers, 'otherBindings': other}


def environment():
    values = {}
    file = ROOT / 'PIdP/.env'
    if file.exists():
        for line in file.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                k, v = line.split('=', 1); values[k.strip()] = v.strip().strip('\"\'')
    return {**values, **os.environ}


def list_objects(s3, bucket):
    # Retry the entire enumeration: a failed middle page must never be mistaken
    # for a complete bucket or retain duplicate records from the first attempt.
    for attempt in range(3):
        try:
            return [item for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket)
                    for item in page.get('Contents',[])]
        except Exception:
            if attempt==2:raise
            time.sleep(2**attempt)


def cf_token():
    # Use the operator login for recovery; website/API grants cannot export D1.
    if os.environ.get('CLOUDFLARE_API_TOKEN'): return os.environ['CLOUDFLARE_API_TOKEN']
    for p in [Path.home()/'.config/.wrangler/config/default.toml', Path.home()/'.wrangler/config/default.toml']:
        if p.exists():
            value = tomllib.loads(p.read_text()).get('oauth_token')
            if value: return value
    raise RuntimeError('Cloudflare operator login is required on the host')


def cf_get(account, path, raw=False):
    request = urllib.request.Request('https://api.cloudflare.com/client/v4/accounts/' + account + path,
                                    headers={'Authorization': 'Bearer ' + cf_token()})
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            data = response.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'Cloudflare read failed: HTTP {e.code}') from None
    if raw: return data
    result = json.loads(data)
    if not result.get('success'): raise RuntimeError('Cloudflare read was unsuccessful')
    return result['result']


def inspect_sqlite(path):
    db = sqlite3.connect(path)
    try:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('SQLite integrity check failed')
        violations = sum(1 for _ in db.execute('PRAGMA foreign_key_check'))
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {'tables': {t: db.execute('SELECT count(*) FROM "'+t.replace('"','""')+'"').fetchone()[0] for t in tables},
                'foreignKeyViolations': violations}
    finally:
        db.close()


def restore_sql(sql, target):
    db = sqlite3.connect(target)
    try:
        with gzip.open(sql, 'rt') if str(sql).endswith('.gz') else Path(sql).open() as stream:
            db.executescript('BEGIN;\n'+stream.read()+'\nCOMMIT;')
    finally:
        db.close()
    return inspect_sqlite(target)


def compress_sql(source, target):
    """Replace an export only after checking its lossless compressed copy."""
    temporary = target.with_name(target.name+'.pending')
    original = digest(source)
    with source.open('rb') as incoming, temporary.open('wb') as outgoing:
        with gzip.GzipFile(filename='', mode='wb', fileobj=outgoing, mtime=0, compresslevel=6) as packed:
            shutil.copyfileobj(incoming, packed)
        outgoing.flush(); os.fsync(outgoing.fileno())
    h=hashlib.sha256()
    with gzip.open(temporary, 'rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
    if h.hexdigest()!=original:raise RuntimeError('Compressed SQL verification failed')
    temporary.replace(target)
    os.utime(target,ns=(source.stat().st_atime_ns,source.stat().st_mtime_ns))


def deduplicate_files(snapshot, store, files):
    """Share identical immutable files with completed generations on the same drive."""
    wanted=set(files.values()); previous={}; saved=0
    for folder in sorted(store.iterdir(), reverse=True):
        if not folder.is_dir() or folder==snapshot or folder.name.startswith('.'):continue
        manifest=folder/'manifest.json'
        if not manifest.exists():continue
        data=json.loads(manifest.read_text())
        if not data.get('completedAt'):continue
        for name,sha in data.get('files',{}).items():
            if sha in wanted and sha not in previous:
                candidate=safe_child(folder,name)
                if candidate.is_file() and digest(candidate)==sha:previous[sha]=candidate
    for name,sha in files.items():
        path=safe_child(snapshot,name);candidate=previous.get(sha)
        if candidate is not None and path.stat().st_dev==candidate.stat().st_dev and path.stat().st_ino!=candidate.stat().st_ino:
            link=path.with_name(path.name+'.dedup-pending')
            link.unlink(missing_ok=True);os.link(candidate,link);link.replace(path)
            saved+=path.stat().st_size
        previous.setdefault(sha,path)
    return saved


def wrangler():
    node = shutil.which('node')
    script = ROOT / 'OrgPortal/org-worker/node_modules/wrangler/bin/wrangler.js'
    if not node or not script.exists(): raise RuntimeError('Install OrgPortal org-worker dependencies first')
    return [node, str(script)]


def capture_local(target):
    """The production copy never replaces locally authored newsletter/identity data."""
    running=[json.loads(line) for line in command(['docker','ps','--format','{{json .Names}}']).decode().splitlines()]
    result={}
    # Device enrollment authority and TURN provisioning secrets stay in private DR storage.
    peers=Path.home()/'.config/organization-peers'
    for name in ['signing.key','turn.json']:
        if (peers/name).is_file():
            out=target/'local/peers'/name;out.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
            shutil.copyfile(peers/name,out);out.chmod(0o600)
            result.setdefault('peerSecrets',[]).append(str(out.relative_to(target)))
    if 'local-community-api' in running:
        command(['python3',ROOT/'OrgPortal/scripts/backup-local-storage.py','--container','local-community-api','--output',target/'local/organization'])
        result['organization']='local/organization'
    if 'lifetech-identity-db' in running:
        config=json.loads(command(['docker','inspect','lifetech-identity-db']).decode())[0]
        out=target/'local/identity';out.mkdir(parents=True,mode=0o700,exist_ok=True)
        dump=command(['docker','exec','lifetech-identity-db','sh','-c',
                      'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom'],timeout=600)
        (out/'database.dump').write_bytes(dump)
        result['identity']={'file':'local/identity/database.dump','image':config['Config']['Image']}
    return result


def verify_postgres(snapshot, entry):
    name='organization-dr-drill-'+uuid.uuid4().hex[:12]
    try:
        from docker_utils import ensure_image
        image=ensure_image(entry['image'])
        command(['docker','run','--pull=never','-d','--network','none','--name',name,'-e','POSTGRES_HOST_AUTH_METHOD=trust',image])
        for _ in range(45):
            # The image's temporary initialization server accepts Unix sockets;
            # TCP becomes ready only when the final server is running.
            ready=subprocess.run(['docker','exec',name,'pg_isready','-h','127.0.0.1','-U','postgres'],capture_output=True)
            if ready.returncode==0:break
            time.sleep(1)
        else:raise RuntimeError('Isolated PostgreSQL restore did not start')
        with safe_child(snapshot,entry['file']).open('rb') as stream:
            restored=subprocess.run(['docker','exec','-i',name,'pg_restore','--exit-on-error','--clean','--if-exists','--no-owner','--no-privileges','-U','postgres','-d','postgres'],stdin=stream,capture_output=True,timeout=600)
        if restored.returncode:raise RuntimeError('Isolated PostgreSQL restore failed; private output withheld')
    finally:
        subprocess.run(['docker','rm','-f','-v',name],capture_output=True)


def capture_sources(target, repositories=None):
    """Git bundles include history; the private archive includes dirty source and local config."""
    result = []; secret_names = set()
    for repo_name in repositories or ['.', *REPOS]:
        repo = ROOT / repo_name
        if not (repo / '.git').exists(): continue
        label = 'Organization' if repo_name == '.' else repo_name
        out = target / 'sources' / label; out.mkdir(parents=True, mode=0o700, exist_ok=True)
        command(['git', 'bundle', 'create', out / 'history.bundle', '--all'], cwd=repo)
        names = command(['git','ls-files','--cached','--others','--exclude-standard','-z'], cwd=repo).decode().split('\0')
        selected = set(n for n in names if n and not any(p in {'node_modules','.wrangler','.local','__pycache__'} for p in Path(n).parts))
        # Local secret/config files are intentionally included only in the private DR copy.
        for parent in [repo, repo/'org-worker', repo/'serverless', repo/'cloudflare', repo/'chat-worker']:
            if parent.exists():
                selected.update(str(p.relative_to(repo)) for pattern in ['.env*','.pidp.env*','.dev.vars*'] for p in parent.glob(pattern) if p.is_file() and not p.is_symlink())
        # Stable gzip headers let identical source snapshots share disk blocks.
        with (out/'working-tree.tar.gz').open('wb') as raw, gzip.GzipFile(filename='',mode='wb',fileobj=raw,mtime=0,compresslevel=1) as packed, tarfile.open(fileobj=packed,mode='w') as archive:
            for name in sorted(selected):
                path = repo / name
                if path.is_file() and not path.is_symlink():
                    archive.add(path, arcname=name, recursive=False)
                    if path.name.startswith(('.env','.pidp.env','.dev.vars')):
                        for line in path.read_text(errors='replace').splitlines():
                            if '=' in line and not line.lstrip().startswith('#'):
                                secret_names.add(line.split('=',1)[0].strip().removeprefix('export '))
        result.append({'repository':label,'commit':command(['git','rev-parse','HEAD'],cwd=repo).decode().strip(),
                       'dirty':bool(command(['git','status','--porcelain'],cwd=repo))})
    return result, secret_names


def backup(args):
    import boto3
    from botocore.config import Config
    store = args.store.resolve(); store.mkdir(parents=True, exist_ok=True, mode=0o700); store.chmod(0o700)
    with (store/'.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        inv = inventory(); env = environment(); account = env.get('CLOUDFLARE_ACCOUNT_ID') or env.get('R2_ACCOUNT_ID')
        if not account: raise RuntimeError('Cloudflare account ID is missing')
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        pending = store / ('.pending-'+stamp)
        if args.resume:
            pending=safe_child(store,args.resume)
            if not pending.is_dir() or not pending.name.startswith('.pending-'):raise RuntimeError('Resume requires an existing pending backup')
            stamp=pending.name.removeprefix('.pending-')
        else:pending.mkdir(mode=0o700)
        manifest = {'version':1,'createdAt':now(),'completedAt':None,'status':'incomplete',
                    'inventory':inv,'databases':{},'objects':{},'workers':{},'gaps':[],
                    'consistency':'Per-database snapshots and per-object versions; no global transaction across services'}
        private_json(pending/'manifest.json',manifest)
        try:
            manifest['sources'], saved_names = capture_sources(pending)
            manifest['local']=capture_local(pending)
            if 'local/peers/signing.key' in manifest['local'].get('peerSecrets',[]):saved_names.add('PEER_SIGNING_KEY')
            if 'local/peers/turn.json' in manifest['local'].get('peerSecrets',[]):saved_names.update(['TURN_KEY_ID','TURN_KEY_API_TOKEN'])
            escrow={}
            if args.secrets_file:
                escrow=json.loads(args.secrets_file.read_text())
                if not isinstance(escrow,dict) or any(not isinstance(values,dict) or any(not isinstance(value,str) or not value for value in values.values()) for values in escrow.values()):
                    raise RuntimeError('Secret escrow must map Worker names to nonempty string-valued secret objects')
                private_json(pending/'secrets/production.json',escrow)
            for ident, entry in inv['databases'].items():
                print('Exporting database:',entry['database_name'],flush=True)
                folder = pending/'databases'/ident; folder.mkdir(parents=True, mode=0o700,exist_ok=True)
                config = ROOT/entry['config']; raw = folder/'export.sql'; sql = folder/'export.sql.gz'
                cmd = wrangler()+['d1','export',ident,'--remote','--config',config,'--output',raw]
                if entry['environment']: cmd += ['--env',entry['environment']]
                marker=folder/'export.sha256'
                if not (sql.exists() and marker.exists() and digest(sql)==marker.read_text().strip()):
                    # Reuse a successfully exported plain SQL file from an interrupted run.
                    if not (raw.exists() and marker.exists() and digest(raw)==marker.read_text().strip()):
                        raw.unlink(missing_ok=True)
                        command(cmd,cwd=config.parent,timeout=1800)
                        marker.write_text(digest(raw)+'\n')
                    compress_sql(raw,sql)
                    marker.write_text(digest(sql)+'\n')
                raw.unlink(missing_ok=True)
                restored=folder/'restored.sqlite';restored.unlink(missing_ok=True)
                try:check = restore_sql(sql,restored)
                finally:restored.unlink(missing_ok=True)
                manifest['databases'][ident] = {**entry,**check,'exportCompletedAt':dt.datetime.fromtimestamp(sql.stat().st_mtime,dt.timezone.utc).isoformat()}
                if check['foreignKeyViolations']:
                    manifest['gaps'].append(entry['database_name']+': foreign key violations in source restore; inspect before use')
                private_json(pending/'manifest.json',manifest)
            s3 = boto3.client('s3',endpoint_url='https://'+account+'.r2.cloudflarestorage.com',
                              aws_access_key_id=env.get('R2_ACCESS_KEY_ID'),aws_secret_access_key=env.get('R2_SECRET_ACCESS_KEY'),region_name='auto',
                              config=Config(max_pool_connections=TRANSFER_WORKERS*2,connect_timeout=10,read_timeout=30,retries={'mode':'standard','max_attempts':3}))
            blob_dir = store/'objects'; blob_dir.mkdir(mode=0o700,exist_ok=True)
            for interrupted in blob_dir.glob('.download-*'):interrupted.unlink()
            catalog=sqlite3.connect(store/'object-catalog.sqlite')
            catalog.execute('CREATE TABLE IF NOT EXISTS objects(bucket TEXT,key TEXT,etag TEXT,size INTEGER,record TEXT,PRIMARY KEY(bucket,key))')
            # Application media first; the large, reproducible jobs cache is still
            # copied in full, after the identity and organization uploads.
            for bucket in sorted(inv['buckets'],key=lambda b:(b=='codecollective-usajobs',b)):
                print('Copying object bucket:',bucket,flush=True)
                items = list_objects(s3,bucket)
                cached={row[0]:json.loads(row[1]) for row in catalog.execute('SELECT key,record FROM objects WHERE bucket=?',(bucket,))}
                def download(item):
                    old=cached.get(item['Key'])
                    if old and old['etag']==item['ETag'] and old['size']==item['Size']:
                        blob=blob_dir/old['sha256']
                        if blob.is_file() and blob.stat().st_size==old['size'] and digest(blob)==old['sha256']:
                            return old
                    # Conditional read refuses a key changed after enumeration.
                    reply = s3.get_object(Bucket=bucket,Key=item['Key'],IfMatch=item['ETag'])
                    h = hashlib.sha256(); size = 0
                    with tempfile.NamedTemporaryFile(dir=blob_dir,prefix='.download-',delete=False) as f:
                        temporary = Path(f.name)
                        try:
                            while True:
                                chunk = reply['Body'].read(1024*1024)
                                if not chunk: break
                                f.write(chunk); h.update(chunk); size += len(chunk)
                            f.flush(); os.fsync(f.fileno())
                        finally:
                            reply['Body'].close()
                    if size != item['Size']:
                        temporary.unlink(); raise RuntimeError('Object size changed during backup')
                    sha = h.hexdigest(); dest = blob_dir/sha
                    if dest.exists() and digest(dest) != sha:
                        temporary.unlink(); raise RuntimeError('Existing backup object is corrupt')
                    temporary.replace(dest)
                    return {'key':item['Key'],'sha256':sha,'size':size,'etag':item['ETag'],
                            'lastModified':item['LastModified'].isoformat(),'contentType':reply.get('ContentType'),
                            'metadata':reply.get('Metadata',{}),'cacheControl':reply.get('CacheControl'),
                            'contentDisposition':reply.get('ContentDisposition'),'contentEncoding':reply.get('ContentEncoding')}
                def retry_download(item):
                    for attempt in range(3):
                        try:return download(item)
                        except Exception:
                            if attempt==2:raise
                            time.sleep(2**attempt)
                records=[];failures=[]
                with concurrent.futures.ThreadPoolExecutor(max_workers=TRANSFER_WORKERS) as pool:
                    futures={pool.submit(retry_download,item):item for item in items}
                    for future in concurrent.futures.as_completed(futures):
                        try:record=future.result()
                        except Exception as e:
                            failures.append({'key':futures[future]['Key'],'errorType':type(e).__name__})
                            # Keep cataloging other successful downloads instead of
                            # abandoning progress while executor shutdown drains its queue.
                            if len(failures)<=3:print(bucket,'object read failed; successful copies are retained for retry',flush=True)
                            continue
                        records.append(record)
                        catalog.execute('INSERT OR REPLACE INTO objects VALUES(?,?,?,?,?)',
                                        (bucket,record['key'],record['etag'],record['size'],json.dumps(record)))
                        if len(records)%500==0:
                            catalog.commit();print(bucket,len(records),'/',len(items),'objects',flush=True)
                    catalog.commit()
                records.sort(key=lambda record:record['key'])
                if failures:
                    private_json(pending/('failed-objects-'+bucket+'.json'),failures)
                    raise RuntimeError(bucket+': '+str(len(failures))+' object reads failed; resume the backup to retry only uncached objects')
                # Re-list to detect concurrent additions/deletions/updates instead of claiming a stable bucket.
                current = [(o['Key'],o['ETag'],o['Size']) for o in list_objects(s3,bucket)]
                if sorted(current) != sorted((i['Key'],i['ETag'],i['Size']) for i in items):
                    raise RuntimeError('Object bucket changed during backup; retry to get a complete inventory')
                manifest['objects'][bucket] = records
                private_json(pending/'manifest.json',manifest)
            catalog.close()
            for worker in inv['workers']:
                folder = pending/'workers'/worker; folder.mkdir(parents=True,mode=0o700,exist_ok=True)
                try:
                    settings = cf_get(account,'/workers/scripts/'+worker+'/settings')
                    (folder/'deployed-source.multipart').write_bytes(cf_get(account,'/workers/scripts/'+worker,raw=True))
                    private_json(folder/'settings.json',settings)
                    for binding in settings.get('bindings',[]):
                        kind=binding.get('type')
                        if kind=='d1' and binding.get('id') not in inv['databases']:
                            manifest['gaps'].append(worker+': deployed D1 binding '+binding['name']+' is absent from the configured export inventory')
                        elif kind=='r2_bucket' and binding.get('bucket_name') not in inv['buckets']:
                            manifest['gaps'].append(worker+': deployed R2 binding '+binding['name']+' is absent from the configured object inventory')
                        elif kind not in {'d1','r2_bucket','plain_text','secret_text','service','assets','version_metadata','durable_object_namespace','queue'}:
                            manifest['gaps'].append(worker+': deployed binding type '+str(kind)+' needs an exporter')
                    secrets = [b['name'] for b in settings.get('bindings',[]) if b.get('type')=='secret_text']
                    missing = sorted(set(secrets)-saved_names)
                    manifest['workers'][worker] = {'secretNames':secrets,'secretsMissingFromLocalConfig':missing,
                                                  'operatorEscrowNames':sorted(set(secrets)&set(escrow.get(worker,{}))),
                                                  'secretValuesValidatedAgainstProduction':False}
                    if secrets: manifest['gaps'].append(worker+': deployed secret values cannot be read back; local copies require production validation'+(' (missing: '+', '.join(missing)+')' if missing else ''))
                except RuntimeError:
                    manifest['gaps'].append(worker+': deployed settings/source could not be read; repository source retained')
            # Sockets are transient; messages/membership are durable in the chat D1 export.
            for binding in inv['otherBindings']:
                if binding['kind']=='durable_objects' and binding['config'] in {'OrgPortal/chat-worker/wrangler.jsonc','peer-worker/wrangler.jsonc'}: continue
                if binding['kind']=='queues':
                    manifest['gaps'].append(binding['config']+': in-flight queues are not exported; reconcile durable outboxes before replay')
                else:
                    manifest['gaps'].append(binding['config']+': '+binding['kind']+' requires a dedicated exporter')
            # Include the finished recovery tooling even when it was edited while
            # a large media backup was running. Application captures retain their own commits.
            launcher,_=capture_sources(pending,['.'])
            manifest['sources']=[*launcher,*[s for s in manifest['sources'] if s['repository']!='Organization']]
            # Include nested local backup manifests too; only the root manifest is self-excluded.
            files = {str(p.relative_to(pending)):digest(p) for p in pending.rglob('*') if p.is_file() and p!=pending/'manifest.json'}
            manifest['deduplicatedBytes']=deduplicate_files(pending,store,files)
            manifest.update(files=files,completedAt=now(),status='data-captured-with-gaps' if manifest['gaps'] else 'complete')
            private_json(pending/'manifest.json',manifest)
            verify_snapshot(pending,store)
            final = store/stamp; pending.rename(final)
            private_json(store/'latest.json',{'snapshot':stamp,'status':manifest['status'],'completedAt':manifest['completedAt']})
            print(json.dumps({'snapshot':str(final),'status':manifest['status'],'databases':len(manifest['databases']),
                              'buckets':len(manifest['objects']),'objects':sum(map(len,manifest['objects'].values())),
                              'gaps':manifest['gaps']}),flush=True)
        except Exception:
            private_json(pending/'manifest.json',manifest)
            raise


def safe_child(parent, relative):
    result = (parent/relative).resolve()
    if not result.is_relative_to(parent.resolve()) or result == parent.resolve():
        raise ValueError('Unsafe backup path')
    return result


def resolve_snapshot(store, name):
    if name is None:
        if not (store/'latest.json').exists():raise RuntimeError('No verified DR snapshot is available yet. Complete disaster-recovery backup first.')
        name = json.loads((store/'latest.json').read_text())['snapshot']
    path = Path(name)
    return path.resolve() if path.is_absolute() else safe_child(store,name)


def status(store):
    result={'store':str(store),'latestVerified':None,'pending':[],'cachedObjects':[]}
    if not store.exists():return result
    if (store/'latest.json').exists():result['latestVerified']=json.loads((store/'latest.json').read_text())
    for folder in sorted(store.glob('.pending-*')):
        try:m=json.loads((folder/'manifest.json').read_text())
        except (OSError,ValueError):continue
        result['pending'].append({'snapshot':folder.name,'createdAt':m.get('createdAt'),
                                  'copiedDatabases':len(m.get('databases',{})),
                                  'copiedBuckets':len(m.get('objects',{})),'verified':False})
    if (store/'object-catalog.sqlite').exists():
        db=sqlite3.connect('file:'+str(store/'object-catalog.sqlite')+'?mode=ro',uri=True)
        try:result['cachedObjects']=[{'bucket':r[0],'objects':r[1],'bytes':r[2]} for r in db.execute('SELECT bucket,count(*),sum(size) FROM objects GROUP BY bucket')]
        finally:db.close()
    return result


def verify_snapshot(snapshot, store):
    manifest = json.loads((snapshot/'manifest.json').read_text())
    if manifest.get('version')!=1 or not manifest.get('completedAt') or not manifest.get('files'):
        raise RuntimeError('Incomplete or unsupported snapshot')
    if set(manifest['databases'])!=set(manifest['inventory']['databases']) or set(manifest['objects'])!=set(manifest['inventory']['buckets']):
        raise RuntimeError('Snapshot does not cover its declared inventory')
    for name, sha in manifest['files'].items():
        if digest(safe_child(snapshot,name))!=sha: raise RuntimeError('Backup file hash mismatch')
    for bucket, objects in manifest['objects'].items():
        for obj in objects:
            blob = safe_child(store/'objects',obj['sha256'])
            if blob.stat().st_size!=obj['size'] or digest(blob)!=obj['sha256']:
                raise RuntimeError('Backup media hash mismatch')
    with tempfile.TemporaryDirectory(prefix='recovery-drill-',dir=store) as tmp:
        for ident, entry in manifest['databases'].items():
            check = restore_sql(snapshot/'databases'/ident/'export.sql.gz',Path(tmp)/(ident+'.sqlite'))
            if check['tables']!=entry['tables'] or check['foreignKeyViolations']!=entry['foreignKeyViolations']:
                raise RuntimeError('Restored database differs from captured database')
    for path in (snapshot/'local').rglob('*.sqlite') if (snapshot/'local').exists() else []:
        inspect_sqlite(path)
    if manifest.get('local',{}).get('identity'):
        verify_postgres(snapshot,manifest['local']['identity'])
    return manifest


def restore(args):
    snapshot=resolve_snapshot(args.store,args.snapshot); manifest=verify_snapshot(snapshot,args.store)
    target=args.target.resolve()
    if target.exists() and any(target.iterdir()): raise RuntimeError('Restore target must be empty; active deployments are never overwritten')
    target.mkdir(parents=True,exist_ok=True,mode=0o700); target.chmod(0o700)
    for ident in manifest['databases']:
        out=target/'databases'/ident;out.mkdir(parents=True,mode=0o700)
        restore_sql(snapshot/'databases'/ident/'export.sql.gz',out/'database.sqlite')
    # Original object keys are in the index; hashed paths cannot escape the restore folder.
    for bucket,objects in manifest['objects'].items():
        out=safe_child(target/'r2',bucket);out.mkdir(parents=True,mode=0o700)
        for obj in objects:
            dest=safe_child(out,obj['sha256'])
            if not dest.exists():shutil.copyfile(safe_child(args.store/'objects',obj['sha256']),dest)
        private_json(out/'index.json',objects)
    shutil.copytree(snapshot/'sources',target/'sources')
    if (snapshot/'workers').exists():shutil.copytree(snapshot/'workers',target/'workers')
    if (snapshot/'local').exists():shutil.copytree(snapshot/'local',target/'local')
    if (snapshot/'secrets').exists():shutil.copytree(snapshot/'secrets',target/'secrets')
    private_json(target/'manifest.json',manifest)
    print(json.dumps({'restoredTo':str(target),'status':manifest['status'],'trafficEnabled':False,'gaps':manifest['gaps']}))


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store',type=Path,default=Path(os.environ.get('ORGANIZATION_DR_DIR',DEFAULT_STORE)))
    sub=parser.add_subparsers(dest='action',required=True)
    sub.add_parser('inventory');sub.add_parser('status');b=sub.add_parser('backup');b.add_argument('--resume')
    b.add_argument('--secrets-file',type=Path,default=Path(os.environ['ORGANIZATION_DR_SECRETS_FILE']) if os.environ.get('ORGANIZATION_DR_SECRETS_FILE') else None)
    v=sub.add_parser('verify');v.add_argument('--snapshot')
    r=sub.add_parser('restore');r.add_argument('--snapshot');r.add_argument('--target',type=Path,required=True)
    args=parser.parse_args()
    if args.action=='inventory':print(json.dumps(inventory(),indent=2))
    elif args.action=='status':print(json.dumps(status(args.store),indent=2))
    elif args.action=='backup':backup(args)
    elif args.action=='restore':restore(args)
    else:
        snapshot=resolve_snapshot(args.store,args.snapshot);m=verify_snapshot(snapshot,args.store)
        print(json.dumps({'snapshot':str(snapshot),'verified':True,'status':m['status'],'gaps':m['gaps']}))


if __name__=='__main__':
    try:main()
    except Exception as e:
        # Provider exceptions may contain object keys, signed URLs or SQL values.
        detail=' '+str(e) if isinstance(e,RuntimeError) else ''
        print('Recovery operation failed ('+type(e).__name__+').'+detail+' No production changes were made.',file=sys.stderr)
        sys.exit(1)
