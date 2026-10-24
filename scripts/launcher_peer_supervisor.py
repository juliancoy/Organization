"""Apply explicit atomic agent releases when idle, with startup rollback."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix',default='lifetech-')
    args=parser.parse_args()
    base=Path(__file__).resolve().parent
    config=Path(os.environ.get('LIFETECH_PEER_CONFIG',str(Path.home()/'.config/lifetech-launcher/peers.json')))
    releases=config.parent/'agent-releases'
    stopped=threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:stopped.set())
    signal.signal(signal.SIGINT,lambda *_:stopped.set())
    child=None;active=None;prior=None;pending=False;started=0;rejected=set()
    marker=Path('/tmp/launcher-agent-ready.json')
    busy=Path('/tmp/launcher-agent-busy.json')
    drain=Path('/tmp/launcher-agent-draining')
    drain.unlink(missing_ok=True)

    def stop_child():
        if child and child.poll() is None:
            child.terminate()
            try:child.wait(timeout=15)
            except subprocess.TimeoutExpired:child.kill();child.wait()

    def desired():
        pointer=releases/'current'
        if not pointer.is_symlink():return base
        folder=pointer.resolve()
        if folder.parent!=releases.resolve():raise ValueError('Agent release escapes release directory')
        manifest=json.loads((folder/'manifest.json').read_text())
        expected={'launcher_peers.py','launcher_discovery.py','launcher_registration.py','launcher_agent.py','launcher_registry.py'}
        if set(manifest)!=expected:raise ValueError('Incomplete agent release')
        for name,digest in manifest.items():
            if hashlib.sha256((folder/name).read_bytes()).hexdigest()!=digest:raise ValueError('Agent release checksum mismatch')
        return folder

    def idle():
        if not busy.exists():return True
        value=json.loads(busy.read_text())
        return not value.get('jobs') and not value.get('browsers') and not value.get('transfers')

    def launch(folder):
        marker.unlink(missing_ok=True)
        drain.unlink(missing_ok=True)
        return subprocess.Popen([sys.executable,'-B',str(folder/'launcher_peers.py'),'--prefix',args.prefix])

    try:
        while not stopped.is_set():
            try:
                wanted=desired()
                if wanted in rejected:wanted=active or base
                if child is None:
                    active=wanted
                    if active!=base:
                        prior=(releases/'previous').resolve() if (releases/'previous').is_symlink() else base
                        pending=True
                    child=launch(active);started=time.monotonic()
                elif pending:
                    ready=json.loads(marker.read_text()) if marker.exists() else {}
                    if ready.get('folder')==str(active) and time.time()-ready.get('time',0)<15:
                        pending=False;prior=None
                    elif child.poll() is not None or time.monotonic()-started>45:
                        rejected.add(active);stop_child();active=prior;prior=None;pending=False
                        pointer=releases/'current'
                        if active==base:pointer.unlink(missing_ok=True)
                        else:
                            temporary=releases/'rollback.tmp';temporary.unlink(missing_ok=True)
                            temporary.symlink_to(active.name);temporary.replace(pointer)
                        child=launch(active);started=time.monotonic()
                        print('Agent startup failed; restored previous process release',flush=True)
                elif wanted!=active:
                    drain.touch()
                    if idle():
                        prior=active;stop_child();active=wanted;pending=True
                        child=launch(active);started=time.monotonic()
                        print('Applying explicit agent release',flush=True)
                elif child.poll() is not None:
                    stopped.wait(5);child=launch(active);started=time.monotonic()
            except (OSError,ValueError,KeyError,TypeError) as error:
                print('Agent update deferred: '+type(error).__name__,flush=True)
            stopped.wait(1)
    finally:
        stop_child();drain.unlink(missing_ok=True)


if __name__=='__main__':main()
