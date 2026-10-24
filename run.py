#!/usr/bin/env python3
"""Start the shared local portal stack from the Organization directory."""
import argparse
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import signal
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent


def launch_container(args):
    """Keep the local launcher visible alongside the services it starts."""
    from tempfile import TemporaryDirectory
    from importlib.metadata import distribution
    import shutil
    name = ('lifetech' if args.prefix == 'bmoremedtech-' else args.prefix.rstrip('-') or 'local') + '-launcher'
    image = 'organization-launcher:local'
    existing = subprocess.run(['docker', 'inspect', '--format', '{{.State.Running}}', name], capture_output=True, text=True)
    if existing.returncode == 0 and existing.stdout.strip() == 'true':
        print(f'{name} is already running. View its output with: docker logs -f {name}')
        if args.login:
            container_login(args, name)
        return 0
    if existing.returncode == 0:
        subprocess.run(['docker', 'rm', name], check=True)
    with TemporaryDirectory(prefix='organization-launcher-') as folder:
        folder = Path(folder)
        shutil.copyfile(ROOT / 'Dockerfile.launcher', folder / 'Dockerfile.launcher')
        # Package the installed launcher dependencies; startup needs no package downloads.
        if sys.version_info[:2] != (3, 12):
            raise RuntimeError('Use Python 3.12 for the launcher image.')
        for package in ('docker', 'cryptography', 'cffi', 'pycparser', 'requests', 'urllib3', 'charset_normalizer', 'idna', 'certifi'):
            installed = distribution(package)
            for relative in installed.files or []:
                if '..' in relative.parts or '__pycache__' in relative.parts:
                    continue
                source = Path(installed.locate_file(relative))
                if source.is_file():
                    target = folder / 'python' / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
        subprocess.run(['docker', 'build', '-f', str(folder / 'Dockerfile.launcher'), '-t', image, str(folder)], check=True)
    socket = Path('/var/run/docker.sock')
    docker_cli = Path(shutil.which('docker')).resolve()
    command = ['docker', 'run', '--detach', '--init', '--name', name, '--restart', 'unless-stopped',
               '--log-opt', 'max-size=5m', '--log-opt', 'max-file=3',
               '--network', 'host', '--user', f'{os.getuid()}:{os.getgid()}', '--group-add', str(socket.stat().st_gid),
               '--label', 'organization.container-class=launcher', '--label', 'organization.color=biotech-gold',
               '--mount', f'type=bind,src={ROOT},dst={ROOT}',
               '--mount', f'type=bind,src={socket},dst=/var/run/docker.sock', '--workdir', str(ROOT),
               '--mount', f'type=bind,src={docker_cli},dst=/usr/local/bin/docker,readonly',
               '--env', 'HOME=/tmp', '--env', 'PYTHONUNBUFFERED=1', '--env', 'ORGANIZATION_LAUNCHER_CONTAINER=1',
               '--env', 'ORGPORTAL_JOURNAL_AUTO_SYNC=false']
    for key in os.environ:
        if key.startswith(('ORGPORTAL_', 'PIDP_', 'CHAT_')) and key != 'ORGPORTAL_JOURNAL_AUTO_SYNC':
            command += ['--env', key]
    command += [image, 'python', str(ROOT / 'run.py'), '--prefix', args.prefix, '--network', args.network, '--port', str(args.port)]
    subprocess.run(command, check=True)
    print(f'Launcher: {name}\nConsole: docker logs -f {name}')
    if args.login:
        container_login(args, name)
    return 0


def container_login(args, name):
    import time
    print('Waiting for launcher startup before opening local sign-in…', flush=True)
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        state = json.loads(subprocess.check_output(['docker', 'inspect', '--format', '{{json .State}}', name], text=True))
        if state.get('Health', {}).get('Status') == 'healthy':
            base = os.getenv('ORGPORTAL_LOCAL_PUBLIC_BASE_URL') or f'https://localhost:{args.port}'
            sys.path.insert(0, str(ROOT / 'OrgPortal'))
            from service_names import service_name
            command = [str(ROOT / 'local-login'), '--base', base, '--container', service_name(args.prefix, 'pidp-dev')]
            if args.login == 'admin': command.append('--admin')
            if args.no_open: command.append('--no-open')
            subprocess.run(command, check=True)
            return
        if not state.get('Running') or state.get('Restarting'):
            raise RuntimeError(f'Launcher startup failed. Inspect: docker logs {name}')
        time.sleep(2)
    raise RuntimeError(f'Launcher startup timed out. Inspect: docker logs {name}')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', choices=['runtime', 'check', 'build', 'deploy', 'verify'], help='Run a release action in the pinned Bookworm container.')
    parser.add_argument('--target', choices=['all', 'web', 'api'], default='all')
    parser.add_argument('--release-checkout', type=Path, help='Clean OrgPortal checkout to release.')
    parser.add_argument('--prefix', default='bmoremedtech-', help='Existing local volume namespace (default: bmoremedtech-).')
    parser.add_argument('--network', default='bmoremedtech', help='Docker network (default: bmoremedtech).')
    parser.add_argument('--port', type=int, default=os.getenv('ORGPORTAL_LOCAL_GATEWAY_PORT', '8443'), help='Local HTTPS port (default: 8443).')
    parser.add_argument('--login', choices=['member', 'admin'], help='Sign your browser in after startup.')
    parser.add_argument('--no-open', action='store_true', help='Print the one-use login link instead of opening a browser.')
    parser.add_argument('--vm', action='store_true', help='Run the stack and test suite in a disposable Multipass VM.')
    parser.add_argument('--vm-cpus', type=int, default=4, help='Multipass virtual CPUs (default: 4).')
    parser.add_argument('--vm-memory', default='8G', help='Multipass RAM (default: 8G).')
    parser.add_argument('--vm-disk', default='40G', help='Multipass disk (default: 40G).')
    parser.add_argument('--vm-timeout', type=int, default=5400, help='Total VM run timeout in seconds (default: 5400).')
    parser.add_argument('--vm-image-archive', type=Path, help='Optional docker-save archive to preload inside the VM.')
    args = parser.parse_args(argv)
    if args.release_checkout and not args.release:
        parser.error('--release-checkout requires --release')
    if args.release and (args.vm or args.login):
        parser.error('--release cannot be combined with --vm or --login')
    if not 1024 <= args.port <= 65535:
        parser.error('--port must be between 1024 and 65535')
    if args.no_open and not args.login:
        parser.error('--no-open requires --login')
    if args.vm_cpus < 1 or args.vm_timeout < 1:
        parser.error('--vm-cpus and --vm-timeout must be positive')
    if args.vm and (args.login or args.port != 8443 or args.prefix != 'bmoremedtech-' or args.network != 'bmoremedtech'):
        parser.error('--vm uses its own fixed test namespace and port; omit --login, --port, --prefix and --network overrides')
    if args.vm_image_archive and (not args.vm or not args.vm_image_archive.is_file()):
        parser.error('--vm-image-archive requires --vm and an existing docker-save archive')
    return args


def main(argv=None):
    args = parse_args(argv)
    if args.release:
        portal = ROOT / 'OrgPortal'
        sys.path.insert(0, str(portal))
        spec = importlib.util.spec_from_file_location('orgportal_release_run', portal / 'run.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.run_release(args.release, args.target, args.release_checkout)
    if args.vm:
        from vm_runner import run_vm
        return run_vm(ROOT, args)
    if not os.getenv('ORGANIZATION_LAUNCHER_CONTAINER'):
        return launch_container(args)
    Path('/tmp/launcher-ready').unlink(missing_ok=True)
    if not (ROOT / 'OrgPortal' / 'run.py').is_file() or not (ROOT / 'PIdP' / 'run.py').is_file():
        raise RuntimeError('Missing application repositories. Run ./pull-all first.')
    sys.stdout.reconfigure(line_buffering=True)
    context = subprocess.run(['docker','context','inspect','--format','{{json .Endpoints.docker.Host}}'], capture_output=True, text=True, check=True)
    if not (os.getenv('DOCKER_HOST') or json.loads(context.stdout)).startswith('unix://'):
        raise RuntimeError('Use a local Docker context for this local deployment.')
    base = (os.getenv('ORGPORTAL_LOCAL_PUBLIC_BASE_URL') or f'https://localhost:{args.port}').rstrip('/')
    host = urlsplit(base).hostname
    if host != 'localhost' and not ipaddress.ip_address(host).is_private:
        raise RuntimeError('Use a private network address for this deployment.')
    os.environ.setdefault('ORGPORTAL_LOCAL_BIND_ADDRESS', '127.0.0.1')
    os.environ['ORGPORTAL_LOCAL_GATEWAY_PORT'] = str(args.port)
    os.environ.setdefault('ORGPORTAL_LOCAL_PUBLIC_BASE_URL', base)
    portal = ROOT / 'OrgPortal'
    sys.path.insert(0, str(portal))
    spec = importlib.util.spec_from_file_location('orgportal_local_run', portal / 'run.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Keep lifecycle/configuration logic in its owning repository. The historical
    # volume namespace preserves existing accounts, local newsletters and D1 data.
    module.run(args.prefix, args.network)
    print(f'\nLocal portal: {base}/')
    print(f'Local administrator: {ROOT / "local-login"} --admin')
    if os.getenv('ORGANIZATION_LAUNCHER_CONTAINER'):
        Path('/tmp/launcher-ready').touch()
        print('Startup complete. Launcher is available in the machine container view.')
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
        while True:
            signal.pause()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'Local startup failed: {error}', file=sys.stderr)
        raise SystemExit(1)
