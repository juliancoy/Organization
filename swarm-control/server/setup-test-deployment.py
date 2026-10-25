#!/usr/bin/env python3
"""Install the optional P2P runtime and publish Swarm Control on the owner console."""
import os
from pathlib import Path
import subprocess
import venv

folder=Path(__file__).resolve().parent
runtime=Path.home()/'.local/share/lifetech-p2p-venv'
if not (runtime/'bin/python').exists():venv.EnvBuilder(with_pip=True).create(runtime)
python=runtime/'bin/python'
subprocess.run([str(python),'-m','pip','install','-r',str(folder/'requirements-p2p.txt')],check=True)
subprocess.run(['python3',str(folder.parent/'build-test.py')],check=True)
# Keep the existing owner-console environment, pairing state, and tunnel intact.
subprocess.run([str(python),'-c','import aiortc, qrcode'],env={**os.environ,'PYTHONPATH':str(Path.home()/'.local/share/compai-codex/python')},check=True)
dropin=Path.home()/'.config/systemd/user/swarm-control-api.service.d/lifetech-p2p.conf'
dropin.parent.mkdir(parents=True,exist_ok=True)
quote=lambda value:'"'+str(value).replace('\\','\\\\').replace('"','\\"')+'"'
dropin.write_text('[Service]\nExecStart=\nExecStart='+quote(python)+' '+quote(folder/'api.py')+'\n')
subprocess.run(['systemctl','--user','daemon-reload'],check=True)
subprocess.run(['systemctl','--user','restart','swarm-control-api.service'],check=True)
subprocess.run(['systemctl','--user','is-active','swarm-control-api.service'],check=True)
print('Published: https://compai.lifetech.fyi/swarm-control')
print('The paired phone must receive the APK and approve installation on Android.')
