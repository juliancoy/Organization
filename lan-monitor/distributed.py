"""Bounded, user-triggered application test shards over the saved LAN SSH key."""
import concurrent.futures
import copy
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
from distributed_worker import SUITES

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(__file__).with_name('.runtime') / 'distributed'
INVENTORY = Path.home() / '.local/state/orgportal/lan-ssh-devices.json'
KEY = Path.home() / '.ssh/orgportal_lan_ed25519'
FINAL = {'passed', 'failed', 'unavailable', 'timed_out', 'interrupted', 'stopped'}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def targets(path=INVENTORY):
    accounts = json.loads(Path(path).read_text()).get('accounts', {})
    result = []
    for host, user in sorted(accounts.items()):
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.local', host) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*', user):
            raise ValueError('SSH inventory contains an invalid saved account')
        result.append({'host': host, 'user': user})
    if not result or len(result) > 8:
        raise ValueError('Configure between one and eight LAN SSH accounts')
    return result


def ssh(target):
    return ['ssh', '-i', str(KEY), '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=8', '-o', 'ConnectionAttempts=1',
            '-o', 'ForwardAgent=no', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
            target['user'] + '@' + target['host']]


def plan(hosts):
    hosts = copy.deepcopy(hosts)
    for host in hosts:
        host.update(status='queued', suites=['monitor', 'map'], jobs={})
    for index, suite in enumerate(['org-api', 'web', 'identity', 'chat', 'workspace', 'identity-python']):
        # Browser/identity work goes to the last host; frontend to the middle.
        slot = {'org-api': 0, 'chat': 0, 'web': 1, 'workspace': 1, 'identity': 2, 'identity-python': 2}[suite] % len(hosts)
        hosts[slot]['suites'].append(suite)
    for host in hosts:
        host['jobs'] = {suite: {'name': SUITES[suite]['name'], 'status': 'queued', 'phase': None, 'logs': []} for suite in host['suites']}
    return hosts


def bundle(root, destination, run):
    spec = importlib.util.spec_from_file_location('distributed_snapshot', root / 'vm_runner.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    digest = hashlib.sha256()
    count = total = 0
    with tarfile.open(destination, 'w:gz', compresslevel=2) as archive:
        for name in ('.', 'OrgPortal', 'PIdP'):
            repository = root / name
            commit = subprocess.check_output(['git', '-C', str(repository), 'rev-parse', 'HEAD'], text=True).strip()
            dirty = bool(subprocess.check_output(['git', '-C', str(repository), 'status', '--porcelain']))
            run['repositories'][name] = {'commit': commit, 'dirty': dirty}
            for source, relative in module.source_files(repository):
                if source.suffix.lower() in {'.pem', '.key', '.p12', '.pfx', '.sqlite', '.sqlite3', '.db', '.har', '.log', '.jks', '.keystore'}:
                    continue
                if any(part in {'artifacts', 'dist', 'dist-lifetech', 'build', '.stable-backups'} for part in relative.parts):
                    continue
                size = source.stat().st_size
                if size > 64 * 1024 * 1024:
                    raise ValueError('Source file exceeds the 64 MiB snapshot limit: ' + str(relative))
                total += size
                count += 1
                if total > 512 * 1024 * 1024 or count > 30000:
                    raise ValueError('Source snapshot exceeds its size or file limit')
                data = source.read_bytes()
                logical = str(Path(name) / relative)
                digest.update(logical.encode() + b'\0' + hashlib.sha256(data).digest())
                info = tarfile.TarInfo(logical)
                info.size = len(data)
                info.mode = source.stat().st_mode & 0o777
                archive.addfile(info, io.BytesIO(data))
        run['source_sha256'] = digest.hexdigest()
        run['source_files'] = count
        run['source_bytes'] = total
        with tempfile.NamedTemporaryFile('w', delete=False) as manifest:
            json.dump(run, manifest)
        try:
            archive.add(manifest.name, arcname='distributed-plan.json', recursive=False)
        finally:
            os.unlink(manifest.name)
    return hashlib.sha256(destination.read_bytes()).hexdigest()


class DistributedTests:
    def __init__(self, root=ROOT, state=STATE, inventory=INVENTORY):
        self.root, self.folder, self.inventory = Path(root), Path(state), Path(inventory)
        self.lock = threading.RLock()
        self.running = False
        self.run = None
        self.last_save = 0
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.prune()
        files = sorted(self.folder.glob('*.json'))
        if files:
            try:
                self.run = json.loads(files[-1].read_text())
                if self.run.get('status') in ('running', 'preparing'):
                    self.run.update(status='interrupted', finished=now())
                    for host in self.run['hosts']:
                        if host['status'] not in FINAL:
                            host['status'] = 'interrupted'
                        for job in host.get('jobs', {}).values():
                            if job.get('status') not in FINAL:
                                job['status'] = 'interrupted'
                    self.save()
            except (OSError, ValueError):
                self.run = None

    def prune(self):
        cutoff = time.time() - 30 * 86400
        files = sorted(self.folder.glob('*.json'), reverse=True)
        for index, path in enumerate(files):
            if index >= 100 or path.stat().st_mtime < cutoff:
                path.unlink()

    def save(self):
        if not self.run:
            return
        path = self.folder / (self.run['id'] + '.json')
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.run))
        temporary.chmod(0o600)
        temporary.replace(path)
        self.last_save = time.monotonic()

    def view(self, ident=None):
        with self.lock:
            current = copy.deepcopy(self.run)
            if ident and (not current or ident != current['id']):
                if not re.fullmatch(r'[0-9TZ-]+-[a-f0-9]{8}', ident):
                    raise ValueError('Invalid run ID')
                path = self.folder / (ident + '.json')
                if not path.is_file():
                    raise ValueError('Run not found')
                current = json.loads(path.read_text())
            history = []
            for path in sorted(self.folder.glob('*.json'), reverse=True)[:100]:
                try:
                    data = json.loads(path.read_text())
                    history.append({key: data.get(key) for key in ('id', 'started', 'finished', 'status', 'source_sha256')})
                except (OSError, ValueError):
                    pass
            return {'running': self.running, 'run': current, 'history': history, 'retention_days': 30}

    def start(self):
        with self.lock:
            if self.running:
                return False
            hosts = plan(targets(self.inventory))
            if not KEY.is_file():
                raise ValueError('The saved LAN SSH private key is not available')
            self.prune()
            self.run = {'id': dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8],
                        'status': 'preparing', 'started': now(), 'finished': None,
                        'hosts': hosts, 'repositories': {}, 'source_sha256': None,
                        'scope': 'Isolated application suites; tests have no network or deployment credentials. All application test execution is container-isolated.'}
            self.running = True
            self.save()
            threading.Thread(target=self.execute, daemon=True).start()
            return True

    def stop(self):
        with self.lock:
            if not self.running or self.run.get('stop_requested'):
                return False
            self.run.update(stop_requested=True, status='stopping')
            self.save()
            threading.Thread(target=self.stop_hosts, args=(self.run,), daemon=True).start()
            return True

    def stop_hosts(self, run):
        def stop_host(host):
            remote = '/home/' + host['user'] + '/organization-test-runs/' + run['id']
            names = ['orgportal-test-' + run['id'] + '-' + suite for suite in host['suites']]
            command = 'mkdir -p ' + shlex.quote(remote) + ' && touch ' + shlex.quote(remote + '/STOP') + '; docker rm -f ' + ' '.join(map(shlex.quote, names))
            try:
                subprocess.run(ssh(host) + [command], capture_output=True, timeout=40)
            except (OSError, subprocess.TimeoutExpired):
                pass  # Host completion reports failures; never claim it stopped before acknowledgement.
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(stop_host, run['hosts']))

    def update(self, host, event):
        with self.lock:
            kind = event.get('kind')
            if kind == 'host':
                host.update(observed_hostname=event.get('hostname'), cpus=event.get('cpus'), python=event.get('python'))
            suite = event.get('suite')
            if suite in host['jobs']:
                job = host['jobs'][suite]
                if kind == 'suite':
                    job.update({key: event[key] for key in ('status', 'exit_code', 'duration_seconds') if key in event})
                    if job['status'] == 'running':
                        job['started'] = now()
                elif kind == 'phase':
                    job['phase'] = event.get('phase')
                elif kind == 'failure':
                    job.setdefault('failure_details', []).append(str(event.get('line', ''))[:2000])
                    job['failure_details'] = job['failure_details'][:240]
                elif kind == 'counts':
                    job['test_counts'] = {key: value for key, value in event.get('counts', {}).items() if key in ('tests', 'passed', 'failed', 'skipped') and isinstance(value, int) and value >= 0}
                elif kind == 'log':
                    job['logs'].append(str(event.get('line', ''))[:2000])
                    job['logs'] = job['logs'][-80:]
            self.run['updated'] = now()
            if kind != 'log' or time.monotonic() - self.last_save > 1:
                self.save()

    def host(self, index, host, archive, archive_hash, ident):
        started = time.monotonic()
        remote = '/home/' + host['user'] + '/organization-test-runs/' + ident
        try:
            with self.lock:
                host.update(status='transferring', started=now())
                self.save()
            subprocess.run(ssh(host) + ['mkdir -p -m 700 ' + shlex.quote(remote)], check=True, capture_output=True, timeout=25)
            with archive.open('rb') as stream:
                subprocess.run(ssh(host) + ['cat > ' + shlex.quote(remote + '/source.tgz')], stdin=stream, check=True, capture_output=True, timeout=300)
            if self.run.get('stop_requested'):
                return
            bootstrap = '''import hashlib,pathlib,tarfile,subprocess,sys
root=pathlib.Path(REMOTE);archive=root/'source.tgz'
assert hashlib.sha256(archive.read_bytes()).hexdigest()==DIGEST,'Snapshot hash mismatch'
work=root/'source';work.mkdir(mode=0o700)
with tarfile.open(archive) as bundle:
 for member in bundle:
  path=pathlib.PurePosixPath(member.name)
  assert member.isfile() and not path.is_absolute() and '..' not in path.parts,'Unsafe snapshot entry'
  target=work.joinpath(*path.parts);target.parent.mkdir(parents=True,exist_ok=True)
  with bundle.extractfile(member) as source:target.write_bytes(source.read())
  target.chmod(0o700 if member.mode&0o111 else 0o600)
archive.unlink()
sys.exit(subprocess.call([sys.executable,str(work/'lan-monitor/distributed_worker.py'),str(INDEX)]))
'''.replace('REMOTE', repr(remote)).replace('DIGEST', repr(archive_hash)).replace('INDEX', str(index))
            with self.lock:
                host['status'] = 'running'
                self.save()
            process = subprocess.Popen(ssh(host) + ['python3 -'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, errors='replace')
            process.stdin.write(bootstrap)
            process.stdin.close()
            timer = threading.Timer(7200, process.kill)
            timer.start()
            try:
                for line in process.stdout:
                    if len(line) > 16000:
                        continue
                    try:
                        self.update(host, json.loads(line))
                    except (ValueError, TypeError):
                        continue
                code = process.wait(timeout=20)
                error = process.stderr.read(2048)
            finally:
                timer.cancel()
                if process.poll() is None:
                    process.kill()
            with self.lock:
                jobs = host['jobs'].values()
                host['status'] = 'stopped' if self.run.get('stop_requested') else 'passed' if all(job['status'] == 'passed' for job in jobs) else 'failed'
                if code != 0:
                    host['status'] = 'unavailable'
                    host['error'] = error[-1000:] or 'Remote worker exited without completing its jobs'
        except Exception as exc:
            with self.lock:
                host.update(status='unavailable', error=type(exc).__name__ + ': ' + str(exc)[:400])
        finally:
            with self.lock:
                host.update(finished=now(), duration_seconds=round(time.monotonic() - started, 2))
                for job in host['jobs'].values():
                    if job['status'] not in FINAL:
                        job['status'] = 'stopped' if self.run.get('stop_requested') else 'unavailable'
                if self.run.get('stop_requested'):
                    host['status'] = 'stopped'
                self.save()
            try:
                subprocess.run(ssh(host) + ['rm -rf -- ' + shlex.quote(remote)], timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def execute(self):
        try:
            with tempfile.TemporaryDirectory(prefix='orgportal-distributed-') as temporary:
                archive = Path(temporary) / 'source.tgz'
                with self.lock:
                    run = self.run
                    hosts = run['hosts']
                    frozen = copy.deepcopy(run)
                checksum = bundle(self.root, archive, frozen)
                with self.lock:
                    run.update({key: frozen[key] for key in ('repositories', 'source_sha256', 'source_files', 'source_bytes')})
                    run.update(status='stopping' if run.get('stop_requested') else 'running', archive_sha256=checksum, archive_bytes=archive.stat().st_size)
                    self.save()
                if run.get('stop_requested'):
                    return
                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                    futures = [pool.submit(self.host, index, host, archive, checksum, run['id']) for index, host in enumerate(hosts)]
                    for future in futures:
                        future.result()
                with self.lock:
                    run['status'] = 'passed' if all(host['status'] == 'passed' for host in hosts) else 'failed'
        except Exception as exc:
            with self.lock:
                self.run.update(status='failed', error=type(exc).__name__ + ': ' + str(exc)[:500])
        finally:
            with self.lock:
                if self.run.get('stop_requested'):
                    self.run['status'] = 'stopped'
                    for host in self.run['hosts']:
                        if host['status'] not in FINAL:
                            host['status'] = 'stopped'
                        for job in host['jobs'].values():
                            if job['status'] not in FINAL:
                                job['status'] = 'stopped'
                self.running = False
                self.run['finished'] = now()
                self.save()
