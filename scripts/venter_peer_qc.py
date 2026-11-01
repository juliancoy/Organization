#!/usr/bin/env python3
"""Owner-authorized public Venter QC, executed by autonomous mTLS peers.

No leader, central job queue, controller RPC, SSH, or internet download is used
at runtime. Partitions can cause duplicate work: content-addressed job IDs and
signed receipts provide idempotent results, not a false exactly-once guarantee.
"""
import argparse
import base64
import fcntl
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import socket
import ssl
import subprocess
import threading
import time
import urllib.request
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

BLOCK = 1024 * 1024
MAX_JSON = 2 * BLOCK
MAX_OUTPUT = 128 * BLOCK
FASTA = r'''import collections,gzip,json
counts=collections.Counter();lengths=[];length=0;records=0
with gzip.open('/input','rt',encoding='ascii') as stream:
 for line in stream:
  if line.startswith('>'):
   if records:lengths.append(length)
   records+=1;length=0
  else:
   seq=line.strip().upper()
   if seq and not records:raise ValueError('Sequence before FASTA header')
   length+=len(seq);counts.update(seq)
if records:lengths.append(length)
if not lengths:raise ValueError('Empty FASTA')
lengths.sort(reverse=True);total=sum(lengths);cumulative=0
for n in lengths:
 cumulative+=n
 if cumulative*2>=total:n50=n;break
acgt=sum(counts[c] for c in 'ACGT')
print(json.dumps({'scope':'descriptive sequence statistics; no assembly validation or reference comparison','sequences':records,'total_bases':total,'longest':lengths[0],'N50':n50,'GC_fraction_acgt':(counts['G']+counts['C'])/acgt if acgt else None,'base_counts':dict(sorted(counts.items()))},sort_keys=True))
'''


BAM_QC = "import gzip,struct,json,collections\n# Record-sized buffers only; no arrays indexed by sequencing cycle/read length.\nMAX_RECORD=64*1024*1024\ncounts=collections.Counter();length_bins=collections.Counter();quality_sum=quality_bases=0;minimum=None;maximum=0\nwith gzip.open('/input','rb') as f:\n def exact(n):\n  if not 0<=n<=MAX_RECORD:raise ValueError('BAM field exceeds bounded parser limit')\n  data=f.read(n)\n  if len(data)!=n:raise ValueError('Truncated BAM')\n  return data\n def integer():return struct.unpack('<i',exact(4))[0]\n if exact(4)!=b'BAM\\x01':raise ValueError('Not BAM')\n exact(integer());references=integer()\n if not 0<=references<=1000000:raise ValueError('Invalid reference count')\n for _ in range(references):exact(integer());integer()\n while True:\n  raw=f.read(4)\n  if not raw:break\n  if len(raw)!=4:raise ValueError('Truncated record length')\n  size=struct.unpack('<i',raw)[0]\n  if size<32:raise ValueError('Short BAM alignment')\n  record=exact(size);refid,pos,binmqname,flagnc,length,mate,mp,tlen=struct.unpack('<iiIIiiii',record[:32])\n  if length<0:raise ValueError('Negative read length')\n  flag=flagnc>>16;name_length=binmqname&255;cigar_count=flagnc&65535\n  sequence_start=32+name_length+4*cigar_count;quality_start=sequence_start+(length+1)//2;end=quality_start+length\n  if name_length<1 or end>len(record):raise ValueError('BAM core extends beyond record')\n  counts['records']+=1;counts['bases']+=length\n  counts['unmapped' if flag&4 else 'mapped']+=1\n  counts['secondary' if flag&256 else 'supplementary' if flag&2048 else 'primary']+=1\n  if flag&512:counts['qc_failed']+=1\n  if flag&1024:counts['duplicates']+=1\n  length_bins[str(length.bit_length())]+=1\n  minimum=length if minimum is None else min(minimum,length);maximum=max(maximum,length)\n  quality=record[quality_start:end];missing=quality.count(255)\n  quality_sum+=sum(quality)-255*missing;quality_bases+=length-missing\nprint(json.dumps({'scope':'Bounded-memory streaming BAM read-level QC; no per-cycle arrays, reference coverage or new variant calls. Length bins use bit_length: k means [2^(k-1),2^k-1], with zero in bin 0.',\n 'counts':dict(counts),'length_bins_log2':dict(length_bins),'minimum_read_length':minimum,'maximum_read_length':maximum,\n 'mean_read_length':counts['bases']/counts['records'] if counts['records'] else None,\n 'quality_bases':quality_bases,'mean_base_quality':quality_sum/quality_bases if quality_bases else None},sort_keys=True))\n"

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(path, algorithm='sha256'):
    h = hashlib.new(algorithm)
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(BLOCK), b''):
            h.update(block)
    return h.hexdigest()


def atomic(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(canonical(value))
    temporary.chmod(0o600)
    temporary.replace(path)


def load(path):
    return json.loads(Path(path).read_text())


def signed(value, key):
    return {'value': value, 'signature': base64.b64encode(key.sign(canonical(value))).decode()}


def verified(envelope, key):
    key.verify(base64.b64decode(envelope['signature'], validate=True), canonical(envelope['value']))
    return envelope['value']


def recipe(job):
    kind = job['kind']
    if kind == 'bam-read-qc':
        return [['samtools', 'quickcheck', '-u', '/input'], ['python3','-c',BAM_QC]]
    if kind == 'variant-summary':
        return [['bcftools', 'stats', '/input']]
    if kind == 'fasta-summary':
        return [['python3', '-c', FASTA]]
    raise ValueError('Unknown QC recipe')


def check_manifest(envelope, public_key, now=None):
    value = verified(envelope, Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key)))
    now = time.time() if now is None else now
    if value['schema'] != 'venter-peer-qc.v1' or not value['created'] <= now <= value['expires']:
        raise ValueError('Unsupported or expired run authorization')
    if not re.fullmatch(r'venter-qc-[a-zA-Z0-9_-]{1,70}', value['id']):
        raise ValueError('Invalid run ID')
    if not 1 <= len(value['nodes']) <= 16 or not 1 <= len(value['jobs']) <= 64:
        raise ValueError('Run exceeds bounds')
    seen = set()
    for job in value['jobs']:
        ident = hashlib.sha256(canonical(job['spec'])).hexdigest()
        if ident != job['id'] or ident in seen:
            raise ValueError('Job identity mismatch or duplicate')
        seen.add(ident)
        spec = job['spec']; recipe(spec)
        if not re.fullmatch('[a-f0-9]{32}', spec['source_md5']) or type(spec['bytes']) is not int or spec['bytes'] <= 0:
            raise ValueError('Invalid source identity')
        if job['preferred_worker'] not in value['nodes']:
            raise ValueError('Unknown preferred worker')
    for node in value['nodes'].values():
        if not re.fullmatch('sha256:[a-f0-9]{64}', node['image']):
            raise ValueError('Unpinned image')
        if not 1 <= node['port'] <= 65535:
            raise ValueError('Invalid peer port')
        x509.load_pem_x509_certificate(node['certificate'].encode())
    return value


def ranked(job, members):
    return sorted(members, key=lambda n: hashlib.sha256((job['id']+'\0'+n).encode()).digest(), reverse=True)


def select_worker(job, members, online, states=None):
    preferred = job['preferred_worker'];states=states or {}
    if job.get('data_local_only'):return preferred
    def usable(name):
        row=states.get(name,{});retry=row.get('retry',{}).get(job['id'],{})
        return (name in online and row.get('current_job') in (None,job['id'])
                and retry.get('state') not in ('blocked','exhausted')
                and not (retry.get('state')=='capacity_wait' and time.time()-retry.get('updated',0)>30))
    if sum(row.get('retry',{}).get(job['id'],{}).get('attempts',0) for row in states.values())>=3:return None
    if usable(preferred):return preferred
    return next((n for n in ranked(job,members) if usable(n)),None)


class RetryLedger:
    """Node-local durable attempts; resource waits do not consume a try."""
    def __init__(self, folder):
        self.path=Path(folder)/'retries.sqlite3'
        with sqlite3.connect(self.path,timeout=30) as db:
            db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS retries(job TEXT PRIMARY KEY,attempts INTEGER NOT NULL,state TEXT NOT NULL,next_due REAL,reason TEXT,updated REAL NOT NULL)')

        self.path.chmod(0o600)
    def append_event(self,value):
        with sqlite3.connect(self.path,timeout=30) as db:
            db.execute('INSERT INTO events(payload) VALUES(?)',(json.dumps(value,sort_keys=True,separators=(',',':')),))
    def recover(self):
        with sqlite3.connect(self.path,timeout=30) as db:
            db.execute("UPDATE retries SET state=CASE WHEN attempts>=3 THEN 'exhausted' ELSE 'backoff' END,next_due=CASE WHEN attempts>=3 THEN NULL ELSE ? END,reason='Worker restarted after interrupted attempt',updated=? WHERE state='running'",(time.time()+30,time.time()))
    def states(self):
        with sqlite3.connect(self.path,timeout=30) as db:
            return {r[0]:dict(zip(('attempts','state','next_due','reason','updated'),r[1:])) for r in db.execute('SELECT * FROM retries')}
    def ready(self,job,now=None):
        row=self.states().get(job);now=time.time() if now is None else now
        return not row or (row['state'] not in ('blocked','exhausted','completed','running') and (row['next_due'] or 0)<=now)
    def begin(self,job):
        with sqlite3.connect(self.path,timeout=30) as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT attempts,state,next_due FROM retries WHERE job=?',(job,)).fetchone()
            if row and (row[0]>=3 or row[1] in ('blocked','exhausted','completed','running') or (row[2] or 0)>time.time()):raise RuntimeError('Retry admission is not due')
            tries=(row[0] if row else 0)+1
            db.execute("INSERT INTO retries VALUES(?,?,'running',NULL,NULL,?) ON CONFLICT(job) DO UPDATE SET attempts=excluded.attempts,state=excluded.state,next_due=NULL,reason=NULL,updated=excluded.updated",(job,tries,time.time()))
        return tries
    def fail(self,job,reason,permanent=False):
        with sqlite3.connect(self.path,timeout=30) as db:
            row=db.execute('SELECT attempts FROM retries WHERE job=?',(job,)).fetchone();attempts=row[0] if row else 0
            state='blocked' if permanent else 'exhausted' if attempts>=3 else 'backoff'
            jitter=.8+int(hashlib.sha256((job+str(attempts)).encode()).hexdigest()[:4],16)/65535*.4
            due=None if state in ('blocked','exhausted') else time.time()+min(900,30*2**max(0,attempts-1))*jitter
            db.execute('INSERT INTO retries VALUES(?,?,?,?,?,?) ON CONFLICT(job) DO UPDATE SET state=excluded.state,next_due=excluded.next_due,reason=excluded.reason,updated=excluded.updated',(job,attempts,state,due,reason[:300],time.time()))
        return state,due
    def defer(self,job,reason):
        with sqlite3.connect(self.path,timeout=30) as db:
            # Keep the first wait time so other peers can prefer a free worker.
            db.execute("INSERT INTO retries VALUES(?,0,'capacity_wait',?,?,?) ON CONFLICT(job) DO UPDATE SET state='capacity_wait',next_due=excluded.next_due,reason=excluded.reason,updated=CASE WHEN retries.state='capacity_wait' THEN retries.updated ELSE excluded.updated END",(job,time.time()+15,reason[:300],time.time()))
    def complete(self,job):
        with sqlite3.connect(self.path,timeout=30) as db:db.execute("UPDATE retries SET state='completed',next_due=NULL,updated=? WHERE job=?",(time.time(),job))


class CommandFailure(RuntimeError):
    def __init__(self,code):
        self.code=code
        super().__init__('QC command failed (exit '+str(code)+')'+('; resource/termination failure requires review' if code==137 else ''))


class Peer:
    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.instance_lock = (self.folder/'daemon.lock').open('a')
        # A status reader does not acquire this lock; main() owns daemon admission.
        self.config = load(self.folder/'config.json')
        enrollment = load(Path.home()/'.config/lifetech-launcher/peers.json')
        self.manifest = check_manifest(load(self.folder/'manifest.json'), enrollment['controller_public_key'])
        if digest(Path(__file__)) != self.manifest['worker_sha256']:
            raise ValueError('Worker code differs from signed authorization')
        if digest(self.folder/'lease.py') != self.manifest['lease_sha256']:
            raise ValueError('Resource admission code differs from signed authorization')
        self.name = self.config['name']
        if self.name != enrollment['name'] or self.name not in self.manifest['nodes']:
            raise ValueError('Worker is not enrolled for this run')
        self.node = self.manifest['nodes'][self.name]
        if hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest() != self.node['machine']:
            raise ValueError('Machine identity changed')
        self.cert = self.folder/'identity.pem'; self.private = self.folder/'identity.key'
        self.key = serialization.load_pem_private_key(self.private.read_bytes(), password=None)
        if self.cert.read_text() != self.node['certificate']:
            raise ValueError('Local TLS certificate differs from authorization')
        self.jobs = {j['id']: j for j in self.manifest['jobs']}
        self.objects = {j['spec']['source_md5']: j['spec'] for j in self.jobs.values()}
        self.retries=RetryLedger(self.folder)
        self.receipts = {}; self.online = {}; self.peers = {}; self.failures = {}
        self.observed_receipts = set(); self.conflicts = set()
        self.stop = threading.Event(); self.lock = threading.RLock()
        self.started = time.monotonic(); self.current = None; self.reason = 'starting'
        self.cache = self.folder/'cache'; self.cache.mkdir(exist_ok=True, mode=0o700)
        self.results = self.folder/'results'; self.results.mkdir(exist_ok=True, mode=0o700)
        self.keys = {name: x509.load_pem_x509_certificate(n['certificate'].encode()).public_key()
                     for name, n in self.manifest['nodes'].items()}
        self.cert_names = {hashlib.sha256(x509.load_pem_x509_certificate(n['certificate'].encode()).public_bytes(serialization.Encoding.DER)).hexdigest(): name
                           for name, n in self.manifest['nodes'].items()}
        for path in self.results.glob('*/receipt.json'):
            try:
                receipt = load(path); self.check_receipt(receipt)
                self.validate_outputs(receipt)
                self.receipts[path.parent.name] = receipt
            except (OSError, ValueError, KeyError):
                pass

    def context(self, server=False, remote=None):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server else ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.check_hostname = False
        context.verify_mode = ssl.CERT_REQUIRED
        trusted = [self.manifest['nodes'][remote]] if remote else self.manifest['nodes'].values()
        context.load_verify_locations(cadata='\n'.join(n['certificate'] for n in trusted))
        context.load_cert_chain(str(self.cert), str(self.private))
        return context

    def open_peer(self, name, path, timeout=5, headers=None):
        node = self.manifest['nodes'][name]
        url = f'https://{node["address"]}:{node["port"]}{path}'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                            urllib.request.HTTPSHandler(context=self.context(remote=name)))
        return opener.open(urllib.request.Request(url, headers=headers or {}), timeout=timeout)

    def remote_json(self, name, path):
        with self.open_peer(name, path) as response:
            raw = response.read(MAX_JSON+1)
        if len(raw) > MAX_JSON:
            raise ValueError('Peer JSON exceeds bound')
        return json.loads(raw)

    def event(self, action, **details):
        value={'time':time.time(),'node':self.name,'action':action,'event_id':self.name+':'+self.manifest['id']+':'+uuid.uuid4().hex,**details}
        with self.lock:
            if hasattr(self,'retries'):self.retries.append_event(value)
            with (self.folder/'events.jsonl').open('ab') as stream:stream.write(canonical(value)+b'\n')
        print(action,json.dumps(details),flush=True)

    def status(self):
        with self.lock:
            return signed({'run': self.manifest['id'], 'node': self.name, 'time': time.time(),
                           'current_job': self.current, 'reason': self.reason, 'retry':self.retries.states(),
                           'completed': sorted(self.receipts), 'conflicts': sorted(self.conflicts),
                           'objects': [md5 for md5 in self.objects if self.object_path(md5)],
                           'free_bytes': shutil.disk_usage(self.folder).free}, self.key)

    def check_receipt(self, envelope):
        value = envelope['value']; name = value['node']
        if name not in self.keys:
            raise ValueError('Unknown result signer')
        verified(envelope, self.keys[name])
        job = self.jobs[value['job']]
        if value['run'] != self.manifest['id'] or value['source_md5'] != job['spec']['source_md5'] or value['image'] != self.manifest['nodes'][name]['image'] or value['worker_sha256'] not in self.manifest.get('accepted_receipt_workers',[self.manifest['worker_sha256']]):
            raise ValueError('Result provenance mismatch')
        if value['machine'] != self.manifest['nodes'][name]['machine'] or value['source_bytes'] != job['spec']['bytes'] or value['tool_versions'] != self.manifest['nodes'][name]['tool_versions']:
            raise ValueError('Worker or tool identity mismatch')
        if value['commands'] != recipe(job['spec']) or value['exit_code'] != 0:
            raise ValueError('Result recipe failed or changed')
        if len(value['outputs']) != len(recipe(job['spec'])):
            raise ValueError('Invalid outputs')
        for output in value['outputs']:
            if not re.fullmatch(r'result-[01]\.txt', output['name']) or not re.fullmatch('[a-f0-9]{64}', output['sha256']) or not 0 <= output['bytes'] <= MAX_OUTPUT:
                raise ValueError('Invalid output identity')
        return value

    def validate_outputs(self, envelope):
        value = self.check_receipt(envelope)
        for output in value['outputs']:
            path = self.results/value['job']/output['name']
            if path.stat().st_size != output['bytes'] or digest(path) != output['sha256']:
                raise ValueError('Output checksum mismatch')

    def object_path(self, md5):
        if md5 not in self.objects:
            return None
        spec = self.objects[md5]
        owned = self.config.get('sources', {}).get(md5)
        if owned:
            path = Path(owned['path'])
            try:
                stat = path.stat()
                if all(stat.__getattribute__(field) == owned[field] for field in ('st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_ino', 'st_dev')):
                    return path
            except OSError:
                pass
        path = self.cache/md5; marker = self.cache/(md5+'.verified.json')
        if path.exists() and marker.exists():
            record = load(marker); stat = path.stat()
            if record == {'bytes': spec['bytes'], 'mtime_ns': stat.st_mtime_ns, 'ctime_ns': stat.st_ctime_ns} and stat.st_size == spec['bytes']:
                return path
        return None

    def poll(self):
        def visit(name):
            if name == self.name:
                return
            try:
                envelope = self.remote_json(name, '/status')
                value = verified(envelope, self.keys[name])
                if value['run'] != self.manifest['id'] or value['node'] != name or abs(time.time()-value['time']) > 60:
                    raise ValueError('Stale peer or wrong run')
                with self.lock:
                    self.online[name] = time.monotonic(); self.peers[name] = value
                for ident in value['completed']:
                    if ident not in self.jobs or (name, ident) in self.observed_receipts:
                        continue
                    receipt = self.remote_json(name, '/receipts/'+ident)
                    result = self.check_receipt(receipt)
                    if ident in self.receipts:
                        expected = self.receipts[ident]['value']['outputs']
                        if result['outputs'] != expected:
                            with self.lock:
                                self.conflicts.add(ident)
                            conflict = self.folder/'conflicts'; conflict.mkdir(exist_ok=True)
                            atomic(conflict/(ident+'-'+name+'.json'), receipt)
                            self.event('result_conflict', job=ident, peer=name, reason='same job produced different output hashes; review required')
                        self.observed_receipts.add((name, ident))
                        continue
                    destination = self.results/ident; destination.mkdir(exist_ok=True)
                    for output in result['outputs']:
                        target = destination/output['name']
                        with self.open_peer(name, '/results/'+ident+'/'+output['name'], timeout=30) as response, target.with_suffix('.part').open('wb') as stream:
                            received = 0
                            buffer=bytearray(BLOCK);view=memoryview(buffer)
                            while count := response.readinto(buffer):
                                received += count
                                if received > output['bytes']:
                                    raise ValueError('Result size overflow')
                                stream.write(view[:count])
                        partial = target.with_suffix('.part')
                        if partial.stat().st_size != output['bytes'] or digest(partial) != output['sha256']:
                            raise ValueError('Result transfer checksum mismatch')
                        partial.replace(target)
                    atomic(destination/'receipt.json', receipt)
                    with self.lock:
                        self.receipts[ident] = receipt
                    self.observed_receipts.add((name, ident))
                    self.event('receipt_replicated', job=ident, from_peer=name)
            except Exception as error:
                with self.lock:
                    self.peers.setdefault(name, {})['last_error'] = type(error).__name__+': '+str(error)[:200]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(visit, self.manifest['nodes']))

    def fetch_input(self, job):
        spec = job['spec']; md5 = spec['source_md5']
        if path := self.object_path(md5):
            return path
        if shutil.disk_usage(self.folder).free < spec['bytes'] + 10*1024**3:
            raise RuntimeError('Waiting for input-cache disk headroom')
        partial = self.cache/(md5+'.part')
        with self.lock:
            sources = [n for n, row in self.peers.items() if md5 in row.get('objects', []) and time.monotonic()-self.online.get(n, 0) < 180]
        for name in sources:
            try:
                started = time.monotonic()
                transferred = 0
                offset = partial.stat().st_size if partial.exists() else 0
                if offset > spec['bytes']:
                    raise ValueError('Partial input exceeds expected size')
                if offset < spec['bytes']:
                    with self.open_peer(name, '/objects/'+md5, timeout=30,
                                        headers={'Range': f'bytes={offset}-'} if offset else {}) as response:
                        if offset and response.status != 206:
                            raise ValueError('Peer did not honor resume offset')
                        with partial.open('ab') as stream:
                            buffer=bytearray(BLOCK);view=memoryview(buffer)
                            while count := response.readinto(buffer):
                                if self.stop.is_set():
                                    raise InterruptedError('Stopping input transfer')
                                offset += count
                                transferred += count
                                if offset > spec['bytes']:
                                    raise ValueError('Input size overflow')
                                stream.write(view[:count])
                if partial.stat().st_size != spec['bytes'] or digest(partial, 'md5') != md5:
                    partial.rename(partial.with_name(partial.name+f'.bad-{time.time_ns()}'))
                    raise ValueError('Publisher input MD5 mismatch; retained evidence')
                target = self.cache/md5; partial.replace(target); stat = target.stat()
                atomic(self.cache/(md5+'.verified.json'), {'bytes': spec['bytes'], 'mtime_ns': stat.st_mtime_ns, 'ctime_ns': stat.st_ctime_ns})
                self.event('input_cached', job=job['id'], from_peer=name, bytes=spec['bytes'], network_bytes=transferred, verified_transfer_seconds=time.monotonic()-started, buffer_bytes=BLOCK)
                return target
            except Exception as error:
                self.event('input_transfer_retry', peer=name, error=str(error)[:200])
        raise RuntimeError('Waiting for a reachable verified source peer')

    def run_job(self, job):
        # Import the reviewed, existing node-local pool; no controller lease RPC.
        from lease import ResourceLease, worker_policy
        policy = worker_policy(Path.home()/'.config/lifetech-launcher/peers.json')
        if not policy['accepting_jobs']:
            raise RuntimeError(policy['reason'])
        if not hasattr(self,'retries'):self.retries=RetryLedger(self.folder)
        ident = job['id']; name = 'venter-qc-'+hashlib.sha256(self.manifest['id'].encode()).hexdigest()[:8]+'-'+ident[:16]
        lease = ResourceLease('venter-public-qc', 2, 2048, name,
                              directory=self.config['resource_directory'], pids=64)
        process = None
        try:
            if not lease.try_acquire():
                raise RuntimeError('Waiting for the existing local CPU/RAM pool')
            source = self.fetch_input(job)
            # A result may have arrived while data was being transferred.
            if ident in self.receipts:
                return
            destination = self.results/ident; destination.mkdir(exist_ok=True)
            commands = recipe(job['spec']); start = time.monotonic(); outputs = []
            self.retries.begin(ident);self.current=ident
            self.event('job_started', job=ident, reason='deterministic owner; local resource admission succeeded', preferred_worker=job['preferred_worker'], observed_peers=sorted(self.online), input_md5=job['spec']['source_md5'])
            for index, command in enumerate(commands):
                target = destination/f'result-{index}.txt'
                argv = ['docker', 'run', '--rm', '--pull=never', '--name', name,
                        '--label', 'org.lifetech.venter-peer='+self.manifest['id'],
                        '--network', 'none', '--read-only', '--log-driver', 'none',
                        '--cpus', '2', '--memory', '2048m', '--memory-swap', '2048m',
                        '--pids-limit', '64', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                        '--ulimit', 'core=0:0', '--user', f'{os.getuid()}:{os.getgid()}',
                        '--mount', 'type=bind,src='+str(source)+',dst=/input,readonly', self.node['image'], *command]
                with target.with_suffix('.part').open('wb') as stdout, (destination/f'stderr-{index}.txt').open('wb') as stderr:
                    process = subprocess.Popen(argv, stdout=stdout, stderr=stderr)
                    while process.poll() is None:
                        if self.stop.wait(1) or target.with_suffix('.part').stat().st_size > MAX_OUTPUT:
                            raise InterruptedError('Job stopped or aggregate output exceeded bound')
                    if process.returncode:
                        raise CommandFailure(process.returncode)
                target.with_suffix('.part').replace(target)
                outputs.append({'name': target.name, 'bytes': target.stat().st_size, 'sha256': digest(target)})
            receipt = signed({'run': self.manifest['id'], 'job': ident, 'node': self.name,
                              'machine': self.node['machine'], 'source_md5': job['spec']['source_md5'],
                              'source_bytes': job['spec']['bytes'], 'image': self.node['image'],
                              'input_validation': 'publisher receipt plus unchanged local file identity' if job['spec']['source_md5'] in self.config.get('sources', {}) else 'full cached compressed-input MD5',
                              'tool_versions': self.node['tool_versions'], 'commands': commands,
                              'worker_sha256': self.manifest['worker_sha256'], 'exit_code': 0,
                              'resources': {'cpus': 2, 'memory_mb': 2048},
                              'elapsed_seconds': time.monotonic()-start, 'completed_at': time.time(),
                              'outputs': outputs}, self.key)
            self.validate_outputs(receipt)
            atomic(destination/'receipt.json', receipt)
            with self.lock:
                self.receipts[ident] = receipt
            self.retries.complete(ident)
            self.event('job_completed', job=ident, outputs=outputs)
        finally:
            # Keep orphan admission reserved if Docker cleanup cannot be confirmed.
            if process is not None:
                subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=30)
                check = subprocess.run(['docker', 'inspect', '-f', '{{.State.Running}}', name], capture_output=True, timeout=10)
                if check.returncode == 0 and check.stdout.strip() == b'true':
                    self.event('cleanup_unconfirmed', container=name)
                    # The shared pool retains orphan admission until Docker
                    # confirms the scoped container has stopped.
                    lease.file.close()
                else:
                    lease.close()
            else:
                lease.close()

    def worker(self):
        while not self.stop.is_set():
            try:
                self.poll()
                with self.lock:
                    online = {self.name} | {n for n,t in self.online.items() if time.monotonic()-t < 180}
                    complete = set(self.receipts)
                if self.conflicts:
                    self.reason = 'conflicting peer results retained; independent review required'
                elif len(complete) == len(self.jobs):
                    self.reason = 'all results verified and replicated; continuing peer service'
                elif time.time() > self.manifest['expires']:
                    self.reason = 'run authorization expired; no new work'
                elif time.monotonic()-self.started < 15:
                    self.reason = 'discovering authorized peers before selecting jobs'
                else:
                    for job in sorted(self.jobs.values(), key=lambda j: j['spec']['bytes']):
                        if job['id'] in complete or not self.retries.ready(job['id']):
                            continue
                        states={self.name:{'retry':self.retries.states(),'current_job':self.current},**self.peers}
                        owner = select_worker(job, self.manifest['nodes'], online,states)
                        if owner != self.name:
                            continue
                        self.reason = 'requesting node-local admission after peer selection'
                        try:
                            self.run_job(job)
                        except Exception as error:
                            self.reason = str(error)[:300]
                            if self.current==job['id']:
                                state,due=self.retries.fail(job['id'],self.reason,isinstance(error,CommandFailure) and error.code in (126,127,137))
                                self.event('job_failed',job=job['id'],reason=self.reason,retry_state=state,next_due=due,attempts=self.retries.states()[job['id']]['attempts'])
                            else:
                                self.retries.defer(job['id'],self.reason)
                                self.event('job_deferred', job=job['id'], reason=self.reason)
                        finally:
                            self.current = None
                        break
            except Exception as error:
                self.reason = str(error)[:300]
                self.event('peer_retry', reason=self.reason)
            self.stop.wait(10)

    def heartbeat(self):
        while not self.stop.is_set():
            with self.lock:
                value = {'node': self.name, 'run': self.manifest['id'],
                         'time': time.time(), 'reason': self.reason,
                         'current_job': self.current, 'completed': sorted(self.receipts), 'retry':self.retries.states(),
                         'jobs_total': len(self.jobs), 'conflicts': sorted(self.conflicts), 'peers': json.loads(json.dumps(self.peers))}
            atomic(self.folder/'state.json', value)
            self.stop.wait(5)

    def serve(self):
        peer = self
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *args):
                pass
            def do_GET(self):
                try:
                    certificate = self.connection.getpeercert(binary_form=True)
                    if hashlib.sha256(certificate).hexdigest() not in peer.cert_names:
                        self.send_error(403); return
                    path = self.path
                    if path == '/status':
                        data = canonical(peer.status()); self.send_response(200)
                        self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data); return
                    if path.startswith('/receipts/'):
                        ident = path.split('/')[-1]
                        with peer.lock:
                            receipt = peer.receipts.get(ident)
                        if not receipt:
                            self.send_error(404); return
                        data = canonical(receipt); self.send_response(200)
                        self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data); return
                    if path.startswith('/objects/'):
                        target = peer.object_path(path.split('/')[-1])
                    elif re.fullmatch(r'/results/[a-f0-9]{64}/result-[01]\.txt', path):
                        _,_,ident,name = path.split('/')
                        with peer.lock:
                            receipt = peer.receipts.get(ident)
                        target = peer.results/ident/name if receipt and any(o['name']==name for o in receipt['value']['outputs']) else None
                    else:
                        target = None
                    if not target:
                        self.send_error(404); return
                    size = target.stat().st_size; start = 0
                    header = self.headers.get('Range')
                    if header:
                        if not re.fullmatch(r'bytes=\d+-', header):
                            self.send_error(416); return
                        start = int(header[6:-1])
                        if start >= size:
                            self.send_error(416); return
                    self.send_response(206 if header else 200)
                    self.send_header('Content-Length', str(size-start))
                    if header:
                        self.send_header('Content-Range', f'bytes {start}-{size-1}/{size}')
                    self.end_headers()
                    with target.open('rb') as source:
                        source.seek(start)
                        buffer=bytearray(BLOCK);view=memoryview(buffer)
                        while count := source.readinto(buffer):
                            self.wfile.write(view[:count])
                except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                    pass
        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True
            request_queue_size = 16
        self.server = Server(('0.0.0.0', self.node['port']), Handler)
        self.server.socket = self.context(server=True).wrap_socket(self.server.socket, server_side=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        threading.Thread(target=self.heartbeat, daemon=True).start()
        self.worker()
        self.server.shutdown(); self.server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    peer = Peer(args.root)
    fcntl.flock(peer.instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # Recover only containers labeled for this exact authorized run.
    peer.retries.recover()
    old = subprocess.check_output(['docker', 'ps', '-aq', '--filter', 'label=org.lifetech.venter-peer='+peer.manifest['id']], text=True).split()
    if old:
        subprocess.run(['docker', 'rm', '-f', *old], check=True, capture_output=True)
    signal.signal(signal.SIGTERM, lambda *_: peer.stop.set())
    signal.signal(signal.SIGINT, lambda *_: peer.stop.set())
    peer.serve()


if __name__ == '__main__':
    main()
