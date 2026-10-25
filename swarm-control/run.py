#!/usr/bin/env python3
"""Install and operate the complete Swarm Control application."""
import argparse
import json
import os
import fcntl
import shutil
import tarfile
import tempfile
from urllib.request import urlopen
from pathlib import Path
import subprocess
import sys
import venv

APP = Path(__file__).resolve().parent
ROOT = APP.parent
RUNTIME = Path.home() / '.local/share/swarm-control/venv'
UNITS = Path.home() / '.config/systemd/user'


def run(*args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'deploy', 'start', 'stop', 'restart', 'status', 'test'])
    parser.add_argument('--revision',help='Reviewed Git revision to deploy; defaults to HEAD.')
    args = parser.parse_args()
    if args.action in ('install','deploy'):
        source=APP
        state=Path.home()/'.local/state/swarm-control'
        config=Path.home()/'.config/swarm-control'
        if args.action=='deploy':
            sys.path.insert(0,str(APP/'server'))
            import identity
            identity.config('https://compai.lifetech.fyi')
        if args.action=='deploy':
            revision=subprocess.check_output(['git','-C',str(ROOT),'rev-parse',args.revision or 'HEAD'],text=True).strip()
            release=Path.home()/'.local/share/swarm-control/releases'/revision
            if not release.exists():
                release.parent.mkdir(parents=True,exist_ok=True)
                staging=Path(tempfile.mkdtemp(prefix='.staging-',dir=release.parent))
                try:
                    with tempfile.TemporaryFile() as archive:
                        run('git','-C',ROOT,'archive','--format=tar',revision,'swarm-control',stdout=archive)
                        archive.seek(0)
                        with tarfile.open(fileobj=archive) as tree:tree.extractall(staging,filter='data')
                    staging.rename(release)
                except Exception:
                    shutil.rmtree(staging);raise
            source=release/'swarm-control'
        if not (RUNTIME / 'bin/python').exists():
            venv.EnvBuilder(with_pip=True).create(RUNTIME)
        run(RUNTIME / 'bin/python', '-m', 'pip', 'install', '-r', APP / 'requirements.txt', cwd=ROOT)
        # One-time data migration preserves credentials and expiry, without an old API.
        config.mkdir(parents=True,exist_ok=True,mode=0o700)
        integration=Path.home()/'.config/lan-monitor/orgportal.json'
        if integration.is_file() and not (config/'integrations/orgportal.json').exists():
            (config/'integrations').mkdir(exist_ok=True,mode=0o700)
            fd=os.open(config/'integrations/orgportal.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as stream:stream.write(integration.read_bytes())
        old=Path.home()/'.config/map-master/codex-devices.json'
        if old.exists() and not (config/'devices.json').exists():
            with old.with_suffix('.lock').open('a+') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                value=json.loads(old.read_text())
                for key,expiry in value['sessions'].items():
                    if isinstance(expiry,(int,float)):
                        value['sessions'][key]={'label':'Previously paired browser','expires':expiry,'created':expiry-30*86400,'last_seen':expiry-30*86400}
                fd=os.open(config/'devices.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'w') as stream:json.dump(value,stream)
        UNITS.mkdir(parents=True, exist_ok=True)
        if args.action=='deploy':
            try:
                with urlopen('http://127.0.0.1:8089/api/distributed-tests',timeout=3) as response:active=json.load(response)
            except OSError:active={}
            if active.get('running') or active.get('active_reruns'):
                raise RuntimeError('Wait for active distributed jobs before deploying Swarm Control.')
            run('systemctl','--user','stop','swarm-control.target')
            previous=APP/'server/.runtime'
            if not state.exists() and previous.exists():
                state.parent.mkdir(parents=True,exist_ok=True)
                shutil.move(str(previous),str(state))
        state.mkdir(parents=True,exist_ok=True,mode=0o700)
        for template in (source / 'server/systemd').iterdir():
            text = template.read_text().replace('{{WORKSPACE}}', str(ROOT)).replace('{{SOURCE}}',str(source)).replace('{{STATE}}',str(state)).replace('{{PYTHON}}', str(RUNTIME / 'bin/python')).replace('{{HOME}}', str(Path.home()))
            (UNITS / template.name).write_text(text)
        run('systemctl', '--user', 'daemon-reload')
        run('systemctl', '--user', 'enable', 'swarm-control.target')
        if args.action=='deploy':
            run('systemctl','--user','restart','swarm-control.target')
    elif args.action == 'test':
        run(sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-q', cwd=APP / 'server')
        run('npm', 'test', cwd=APP / 'server')
    elif args.action == 'status':
        run('systemctl', '--user', 'status', 'swarm-control.service', 'swarm-control-api.service', 'swarm-control-tunnel.service', '--no-pager')
    else:
        run('systemctl', '--user', args.action, 'swarm-control.target')


if __name__ == '__main__':
    main()
