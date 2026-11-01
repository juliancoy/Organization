import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from input_runtime_release import InputRuntimeRelease,INSTALL

class InputReleaseTests(unittest.TestCase):
    def test_atomic_install_verifies_bytes_and_preserves_previous(self):
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'peers.json';config.write_text(json.dumps({'name':'fixture'}))
            env={**os.environ,'LIFETECH_PEER_CONFIG':str(config)}
            release=InputRuntimeRelease(Path(__file__).resolve().parents[3])
            def deploy(files,revision=None,machine=None):
                manifest={name:hashlib.sha256(text.encode()).hexdigest() for name,text in files.items()}
                payload={'files':files,'revision':revision or hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest(),
                         'node':'fixture','machine':machine or hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest()}
                return subprocess.run([sys.executable,'-c',INSTALL],input=json.dumps(payload),capture_output=True,text=True,env=env)
            result=deploy(release.files);self.assertEqual(result.returncode,0,result.stderr)
            current=config.parent/'input-releases/current';old=current.resolve()
            self.assertEqual(json.loads(result.stdout)['revision'],release.revision)
            changed={**release.files,'run_inputs.py':release.files['run_inputs.py']+'\n# release fixture\n'}
            self.assertEqual(deploy(changed).returncode,0)
            self.assertEqual((current.parent/'previous').resolve(),old)
            active=current.resolve()
            for result in [deploy(changed,revision='wrong'),deploy(changed,machine='wrong'),deploy({'run_inputs.py':'pass'})]:
                self.assertNotEqual(result.returncode,0)
                self.assertEqual(current.resolve(),active)
            (active/'scripts/run_inputs.py').write_text('tampered')
            self.assertNotEqual(deploy(changed).returncode,0)
