"""Copy current source into a disposable Multipass VM; never mount host state."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import uuid

REPOSITORIES = ('.', 'OrgPortal', 'PIdP')
CLOUD_INIT = '''#cloud-config
package_update: true
packages: [docker.io, git, python3-venv, ca-certificates, curl]
write_files:
  - path: /etc/organization-disposable-vm
    content: "Disposable Organization test VM"
runcmd:
  - [systemctl, enable, --now, docker]
  - [usermod, -aG, docker, ubuntu]
'''


def source_files(repository):
    """Include uncommitted source, but exclude ignored state and local secrets."""
    result = subprocess.run(['git', '-C', str(repository), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                            check=True, capture_output=True)
    for name in sorted(set(os.fsdecode(result.stdout).split('\0')) - {''}):
        path = Path(name)
        if path.is_absolute() or '..' in path.parts:
            raise RuntimeError(f'Unsafe source path: {name}')
        if any(part in {'.git', 'node_modules', '__pycache__', '.venv', '.wrangler'} or part.startswith('.local') for part in path.parts):
            continue
        if path.name == 'pidp_editme.py' or (path.name.startswith(('.env', '.dev.vars')) and not path.name.endswith(('.example', '.sample'))):
            continue
        source = repository / path
        # Do not follow symlinks (including parent directories) out of a checkout.
        if any(parent.is_symlink() for parent in [source, *[repository / p for p in path.parents if p != Path('.')]]):
            continue
        if source.is_file():
            yield source, path


def snapshot(root, destination):
    with tarfile.open(destination, 'w:gz') as archive:
        for name in REPOSITORIES:
            repository = root / name
            if not (repository / '.git').exists():
                raise RuntimeError(f'Missing Git checkout: {repository}')
            for source, relative in source_files(repository):
                archive.add(source, arcname=str(Path(name) / relative), recursive=False)


def run_vm(root, args):
    if not shutil.which('multipass'):
        raise RuntimeError('Multipass is required for --vm. Install it first (Ubuntu: sudo snap install multipass).')
    name = 'organization-test-' + uuid.uuid4().hex[:12]
    report = root / '.local-reports' / name
    report.mkdir(parents=True, mode=0o700)
    deadline = time.monotonic() + args.vm_timeout
    launched = False
    status = 1

    def command(*parts, input=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(parts, args.vm_timeout)
        subprocess.run(['multipass', *map(str, parts)], check=True, timeout=remaining, input=input, text=True)

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupted)
    print(f'VM: {name}\nReports: {report}', flush=True)
    try:
        # Snap-packaged Multipass has a private /tmp; keep transfer files in the
        # workspace and stream cloud-init so both clients can access the inputs.
        with tempfile.TemporaryDirectory(prefix='organization-vm-', dir=root) as temporary:
            temporary = Path(temporary)
            bundle = temporary / 'source.tar.gz'
            snapshot(root, bundle)
            # Mark before launch so partially created VMs are cleaned up too.
            launched = True
            command('launch', '24.04', '--name', name, '--cpus', args.vm_cpus,
                    '--memory', args.vm_memory, '--disk', args.vm_disk, '--timeout', args.vm_timeout,
                    '--cloud-init', '-', input=CLOUD_INIT)
            command('exec', name, '--', 'sudo', 'cloud-init', 'status', '--wait')
            image_archive = getattr(args, 'vm_image_archive', None)
            if image_archive:
                # Keep the transfer input visible to snap-packaged Multipass.
                images = temporary / 'runtime-images.tar'
                shutil.copyfile(image_archive, images)
                command('transfer', images, f'{name}:/home/ubuntu/runtime-images.tar')
                command('exec', name, '--', 'bash', '-c',
                        'docker load -i /home/ubuntu/runtime-images.tar && rm /home/ubuntu/runtime-images.tar')
            command('transfer', bundle, f'{name}:/home/ubuntu/source.tar.gz')
            command('exec', name, '--', 'bash', '-c',
                    'mkdir -p /home/ubuntu/Organization && tar --touch -xzf /home/ubuntu/source.tar.gz -C /home/ubuntu/Organization && rm /home/ubuntu/source.tar.gz')
            command('exec', name, '--', 'bash', '/home/ubuntu/Organization/vm/bootstrap.sh')
            command('exec', name, '--', '/home/ubuntu/Organization/.venv/bin/python', '/home/ubuntu/Organization/vm/guest_tests.py')
            status = 0
    except KeyboardInterrupt:
        print('VM run interrupted; collecting results and deleting the VM.', flush=True)
        status = 130
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'VM run failed: {error}', flush=True)
    finally:
        # A second interrupt must not skip deletion while reports are copied.
        previous_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        # Each cleanup operation is bounded independently of the test deadline.
        if launched:
            try:
                collected = subprocess.run(['multipass', 'transfer', '--recursive', f'{name}:/home/ubuntu/Organization/.vm-results', str(report)], timeout=60)
                if collected.returncode:
                    if status == 0:
                        status = 1
                    print('Guest reports unavailable (the VM may have failed before tests started).', flush=True)
            except (OSError, subprocess.SubprocessError) as error:
                if status == 0:
                    status = 1
                print(f'Could not collect reports: {error}', flush=True)
            try:
                deleted = subprocess.run(['multipass', 'delete', '--purge', name], timeout=180)
                if deleted.returncode:
                    status = 1
                    print(f'Cleanup failed. Remove this VM with: multipass delete --purge {name}', flush=True)
                else:
                    print(f'Deleted and purged VM: {name}', flush=True)
            except (OSError, subprocess.SubprocessError) as error:
                status = 1
                print(f'Cleanup failed: {error}. Run: multipass delete --purge {name}', flush=True)
        signal.signal(signal.SIGTERM, previous)
        signal.signal(signal.SIGINT, previous_int)
        (report / 'host-result.json').write_text(json.dumps({'vm': name, 'exitCode': status}, indent=2) + '\n')
    return status
