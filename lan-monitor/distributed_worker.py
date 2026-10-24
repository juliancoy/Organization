#!/usr/bin/env python3
"""Run a fixed GitHub commit test shard in disposable Docker containers."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time

NODE = 'node:22-bookworm'
BROWSER = 'mcr.microsoft.com/playwright:v1.59.1-noble'
PYTHON = 'python:3.12-bookworm'
# Commands are application-owned, never accepted from dashboard requests.
SUITES = {
    'p2p': {'name': 'Cross-device WebRTC P2P', 'coordinated': True, 'image': 'selenium/standalone-chrome:latest'},
    'monitor': {'name': 'LAN monitor Python', 'cwd': 'lan-monitor', 'image': PYTHON,
                'test': ['python3', '-m', 'unittest', 'discover', '-s', 'tests', '-v']},
    'map': {'name': 'LAN map model', 'cwd': 'lan-monitor', 'image': NODE,
            'test': ['npm', 'test']},
    'workspace': {'name': 'Workspace tooling', 'cwd': '.', 'image': 'orgportal-distributed-python-node:1',
                  'prepare': ['python', '-m', 'pip', 'install', '--target', '/work/.test-deps/workspace', '-r', 'requirements.txt'],
                  'test': ['python', '-m', 'unittest', 'discover', '-s', 'tests', '-v']},
    'org-api': {'name': 'Organization API', 'cwd': 'OrgPortal/org-worker', 'image': NODE,
                'prepare': ['sh', '-c', 'npm ci --prefix ../chat-worker --omit=dev --no-audit --no-fund && npm ci --no-audit --no-fund'],
                'test': ['sh', '-c', 'npm test && npm run typecheck && npm run check:bundle']},
    'chat': {'name': 'Chat API', 'cwd': 'OrgPortal/chat-worker', 'image': NODE,
             'prepare': ['npm', 'ci', '--no-audit', '--no-fund'],
             'test': ['sh', '-c', 'npm test && npm run typecheck']},
    'web': {'name': 'Portal frontend', 'cwd': 'OrgPortal/web', 'image': NODE,
            'prepare': ['sh', '-c', 'npm ci --prefix ../org-worker --omit=dev --no-audit --no-fund && npm ci --no-audit --no-fund'],
            'test': ['sh', '-c', 'npm test -- --maxWorkers=2 && npm run build']},
    'identity': {'name': 'Identity API and browser regression', 'cwd': 'PIdP/serverless', 'image': BROWSER,
                 'prepare': ['npm', 'ci', '--no-audit', '--no-fund'],
                 'test': ['sh', '-c', 'npm run typecheck && node --import tsx --test test/*.test.mjs test/*.test.ts']},
    'identity-python': {'name': 'Identity Python', 'cwd': 'PIdP', 'image': PYTHON,
                        'prepare': ['python', '-m', 'pip', 'install', '--target', '/work/.test-deps/identity-python', '-r', 'requirements-test.txt', 'pytest'],
                        'test': ['python', '-m', 'pytest', 'tests', '-q', '--tb=short']},
}


def emit(kind, **fields):
    print(json.dumps({'kind': kind, **fields}), flush=True)


def clean(line):
    line = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', line).rstrip()
    line = re.sub(r'(?i)(bearer\s+)[A-Za-z0-9._~-]+', r'\1[redacted]', line)
    line = re.sub(r'(?i)((?:password|secret|api[_-]?key|access[_-]?token)\s*[=:]\s*)[^\s,;]+', r'\1[redacted]', line)
    return line[:2000]


def parse_counts(line, counts):
    fields = {'tests': 'tests', 'pass': 'passed', 'fail': 'failed', 'skipped': 'skipped'}
    match = re.match(r'^(?:#|ℹ)\s+(tests|pass|fail|skipped)\s+(\d+)', line)
    if match:
        counts[fields[match[1]]] = int(match[2])
    for word, key in [('passed', 'passed'), ('failed', 'failed'), ('skipped', 'skipped')]:
        match = re.search(r'(\d+) ' + word + r'\b', line)
        if match and ('Tests ' in line or re.search(r'\d+ (?:passed|failed).* in ', line)):
            counts[key] = int(match[1])
    match = re.match(r'^Ran (\d+) tests? in ', line)
    if match:
        counts['tests'] = int(match[1])
    if line == 'OK' and 'tests' in counts:
        counts['passed'] = counts['tests']; counts['failed'] = 0
    match = re.match(r'^FAILED \((.*)\)', line)
    if match:
        counts['failed'] = sum(int(n) for n in re.findall(r'(?:failures|errors)=(\d+)', match[1]))
        if 'tests' in counts:
            counts['passed'] = max(0, counts['tests'] - counts['failed'])
    if 'tests' not in counts and ('passed' in counts or 'failed' in counts):
        counts['tests'] = sum(counts.get(k, 0) for k in ('passed', 'failed', 'skipped'))
    return counts


def dependency_cache(work,suite_id,spec):
    """Keep package downloads, never installed dependencies, on the worker host."""
    digest=hashlib.sha256((suite_id+'\0'+spec['image']+'\0'+os.uname().machine).encode())
    ignored={'node_modules','.git','.venv','venv','dist','build','.test-deps','__pycache__'}
    locks={'package-lock.json','npm-shrinkwrap.json','yarn.lock','pnpm-lock.yaml',
           'pyproject.toml','poetry.lock','uv.lock','Pipfile.lock'}
    for directory,folders,files in os.walk(work):
        folders[:]=sorted(name for name in folders if name not in ignored)
        for name in sorted(files):
            if name not in locks and not (name.startswith('requirements') and name.endswith('.txt')):continue
            path=Path(directory)/name
            digest.update(str(path.relative_to(work)).encode()+b'\0'+path.read_bytes())
    root=Path.home()/'.cache/organization-tests/packages'/digest.hexdigest()
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    for name in ('npm','pip'):(root/name).mkdir(exist_ok=True,mode=0o700)
    return root


def run(work, suite_id, phase, spec, run_id, env):
    if (work.parent / 'STOP').exists():
        return 130
    command = spec.get(phase)
    if not command:
        return 0
    name = 'orgportal-test-' + run_id + '-' + suite_id
    cwd = work / spec['cwd']
    native = spec.get('native')
    if not native:
        cache=dependency_cache(work,suite_id,spec)
        command = ['docker', 'run', '--pull=never', '--rm', '--name', name, '--user', str(os.getuid()) + ':' + str(os.getgid()), '-e', 'HOME=/tmp', '--cpus', '2', '--memory', '3g',
                   '--pids-limit', '512', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                   '--shm-size', '256m', '--network', 'host' if phase == 'prepare' else 'none',
                   '--mount', 'type=bind,src=' + str(work) + ',dst=/work',
                   '--mount', 'type=bind,src=' + str(cache) + ',dst=/cache',
                   '-e','npm_config_cache=/cache/npm','-e','PIP_CACHE_DIR=/cache/pip',
                   '-e','PIP_DISABLE_PIP_VERSION_CHECK=1', '-w', '/work/' + spec['cwd'],
                   '-e', 'CI=true', '-e', 'PYTHONPATH=/work/.test-deps/' + suite_id + ':/work', '-e', 'SECRET_KEY=distributed-tests-only', '-e', 'DATABASE_URL=postgresql+asyncpg://test:test@127.0.0.1/test',
                   '-e', 'PIDP_BUILD_COMMIT=' + env['PIDP_BUILD_COMMIT'], '-e', 'PIDP_BUILD_DIRTY=false',
                   '-e', 'ORGPORTAL_BUILD_COMMIT=' + env['ORGPORTAL_BUILD_COMMIT'], '-e', 'ORGPORTAL_BUILD_DIRTY=false',
                   spec['image'], *command]
    if (work.parent / 'STOP').exists():
        return 130
    emit('phase', suite=suite_id, phase=phase)
    timeout = 900 if phase == 'prepare' else 1200
    process = None
    counts = {}
    failure_context = 0
    failure_lines = 0
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, errors='replace', start_new_session=True)
        expired = threading.Event()
        def expire():
            expired.set()
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        def watch_stop():
            while process.poll() is None:
                if (work.parent / 'STOP').exists():
                    os.killpg(process.pid, signal.SIGTERM)
                    return
                time.sleep(0.25)
        threading.Thread(target=watch_stop, daemon=True).start()
        timer = threading.Timer(timeout, expire)
        timer.start()
        try:
            for line in process.stdout:
                text = clean(line)
                emit('log', suite=suite_id, phase=phase, line=text)
                if re.search(r'^(?:not ok |FAIL(?:ED)?|ERROR[: ])|(?:Error|AssertionError):', text):
                    failure_context = 30
                if failure_context and failure_lines < 240:
                    emit('failure', suite=suite_id, line=text)
                    failure_context -= 1
                    failure_lines += 1
                previous = dict(counts)
                parse_counts(text, counts)
                if counts != previous and phase == 'test':
                    emit('counts', suite=suite_id, counts=counts)
            code = process.wait(timeout=10)
        finally:
            timer.cancel()
        return 124 if expired.is_set() else code
    except Exception as exc:
        emit('log', suite=suite_id, phase=phase, line=type(exc).__name__ + ': ' + clean(str(exc)))
        return 125
    finally:
        if process and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        if not native:
            subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('index', type=int)
    args = parser.parse_args()
    work = Path(__file__).resolve().parents[1]
    manifest = json.loads((work / 'distributed-plan.json').read_text())
    target = manifest['hosts'][args.index]
    # No inherited deployment, cloud, database, or SSH credentials enter a job.
    env = {key: value for key, value in os.environ.items() if key in ('PATH', 'LANG', 'LC_ALL', 'HOME')}
    env.update(CI='true', PYTHONDONTWRITEBYTECODE='1',
               PIDP_BUILD_COMMIT=manifest['repositories']['PIdP']['commit'],
               ORGPORTAL_BUILD_COMMIT=manifest['repositories']['OrgPortal']['commit'])
    emit('host', hostname=__import__('socket').gethostname(), cpus=os.cpu_count(),
         python=__import__('sys').version.split()[0], suites=target['suites'])
    for image in dict.fromkeys([PYTHON]+[SUITES[suite]['image'] for suite in target['suites'] if target['jobs'][suite]['status']!='reused']):
        result = subprocess.run(['docker', 'image', 'inspect', image, '--format', '{{json .Id}}'],
                                capture_output=True, text=True, timeout=25)
        if result.returncode or json.loads(result.stdout) != target['images'].get(image):
            emit('log', phase='images', line='Required image was not distributed or its ID differs: ' + image)
            raise SystemExit(125)
    # Bind sources belong to this Docker daemon's host, not the coordinator.
    # Probe the exact GitHub checkout manifest before allowing any suite to execute.
    snapshot=work/'distributed-plan.json'
    expected=hashlib.sha256(snapshot.read_bytes()).hexdigest()
    probe=['docker','run','--pull=never','--rm','--network','none',
           '--user',str(os.getuid())+':'+str(os.getgid()),'--cap-drop','ALL',
           '--memory','128m','--pids-limit','32',
           '--mount','type=bind,src='+str(work)+',dst=/work,readonly',
           target['images'][PYTHON],'python3','-c',
           "import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('/work/distributed-plan.json').read_bytes()).hexdigest())"]
    result=subprocess.run(probe,capture_output=True,text=True,timeout=45,env=env)
    if result.returncode or result.stdout.strip()!=expected:
        emit('log',phase='mount',line='Docker cannot read this host snapshot. Bind mounts must reference files on the daemon host; use a local Docker context on each agent worker.')
        raise SystemExit(125)
    emit('log',phase='mount',line='Verified host-local source bind mount against the snapshot SHA-256')
    for suite_id in target['suites']:
        if (work.parent / 'STOP').exists():
            break
        if target.get('jobs',{}).get(suite_id,{}).get('status')=='reused':
            continue
        spec = SUITES[suite_id]
        if spec.get('coordinated'):
            continue  # The coordinator must drive real peers together, across hosts.
        # Execute the verified image ID, independent of later tag changes.
        spec = {**spec, 'image': target['images'][spec['image']]}
        if suite_id=='org-api':
            shard=target['org_api_shard']
            index,count=shard['index'],shard['count']
            if not isinstance(index,int) or not isinstance(count,int) or not 1<=index<=count<=24:
                raise ValueError('Invalid Org API shard')
            command=f'npm run build:metadata && node --import tsx --test --test-concurrency=2 --test-shard={index}/{count} test/*.test.ts'
            if index==1:command+=' && npm run typecheck && npm run check:bundle'
            spec={**spec,'test':['sh','-c',command]}
        started = time.monotonic()
        emit('suite', suite=suite_id, status='running', name=spec['name'])
        status = run(work, suite_id, 'prepare', spec, manifest['id'], env)
        if status == 0:
            status = run(work, suite_id, 'test', spec, manifest['id'], env)
        emit('suite', suite=suite_id, status='passed' if status == 0 else 'timed_out' if status == 124 else 'failed',
             exit_code=status, duration_seconds=round(time.monotonic() - started, 3))
    emit('complete', observed=datetime.datetime.now(datetime.timezone.utc).isoformat())


if __name__ == '__main__':
    main()
