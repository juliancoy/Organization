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
    import shutil
    name = ('lifetech' if args.prefix == 'bmoremedtech-' else args.prefix.rstrip('-') or 'local') + '-launcher'
    image = 'organization-launcher:webrtc-1'
    existing = subprocess.run(['docker', 'inspect', '--format', '{{.State.Running}}', name], capture_output=True, text=True)
    if existing.returncode == 0 and existing.stdout.strip() == 'true' and not args.recreate_launcher:
        print(f'{name} is already running. View its output with: docker logs -f {name}')
        if args.login:
            container_login(args, name)
        return 0
    with TemporaryDirectory(prefix='organization-launcher-') as folder:
        shutil.copyfile(ROOT / 'Dockerfile.launcher', Path(folder) / 'Dockerfile')
        from docker_utils import build_image
        build_image(image,folder)
    if existing.returncode == 0:
        subprocess.run(['docker', 'rm', '--force', name], check=True)
    # The full launcher takes over the peer port from a test-initialized session.
    subprocess.run(['docker','rm','--force','lifetech-launcher-session'],capture_output=True)
    socket = Path('/var/run/docker.sock')
    docker_cli = Path(shutil.which('docker')).resolve()
    command = ['docker', 'run', '--pull=never', '--detach', '--init', '--name', name, '--restart', 'unless-stopped',
               '--log-opt', 'max-size=5m', '--log-opt', 'max-file=3',
               '--network', 'host', '--user', f'{os.getuid()}:{os.getgid()}', '--group-add', str(socket.stat().st_gid),
               '--label', 'organization.container-class=launcher', '--label', 'organization.color=biotech-gold',
               '--mount', f'type=bind,src={ROOT},dst={ROOT}',
               '--mount', f'type=bind,src={socket},dst=/var/run/docker.sock', '--workdir', str(ROOT),
               '--mount', f'type=bind,src={docker_cli},dst=/usr/local/bin/docker,readonly',
               '--env', 'HOME=/tmp', '--env', 'PYTHONUNBUFFERED=1', '--env', 'ORGANIZATION_LAUNCHER_CONTAINER=1',
               '--env', 'ORGPORTAL_JOURNAL_AUTO_SYNC=false']
    lan_inventory=Path.home()/'.local/state/orgportal/lan-ssh-devices.json'
    ssh_folder=Path.home()/'.ssh'
    if lan_inventory.exists():
        command += ['--mount',f'type=bind,src={lan_inventory},dst=/run/docker-utils-inventory.json,readonly',
                    '--env','DOCKER_UTILS_INVENTORY=/run/docker-utils-inventory.json']
    if ssh_folder.exists():
        command += ['--mount',f'type=bind,src={ssh_folder},dst=/run/lan-ssh,readonly',
                    '--env','DOCKER_UTILS_SSH_KEY=/run/lan-ssh/orgportal_lan_ed25519',
                    '--env','DOCKER_UTILS_KNOWN_HOSTS=/run/lan-ssh/known_hosts']
    peer_folder = Path.home() / '.config/lifetech-launcher'
    peer_folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    command += ['--mount', f'type=bind,src={peer_folder},dst=/run/launcher-config',
                '--env', 'LIFETECH_PEER_CONFIG=/run/launcher-config/peers.json']
    job_root=ROOT/'.runtime/launcher-jobs'
    job_root.mkdir(parents=True,exist_ok=True,mode=0o700)
    command += ['--mount','type=bind,src=/etc/machine-id,dst=/etc/machine-id,readonly',
                '--env','LIFETECH_JOB_ROOT='+str(job_root)]
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
    parser.add_argument('--recreate-launcher', action='store_true', help='Recreate the launcher container to load updated code/configuration; keep service containers and data.')
    parser.add_argument('--worker-mode',choices=['auto','always','paused'],help='Accept jobs on AC power, always, or submit only. Existing jobs finish.')
    parser.add_argument('--swarm',action='store_true',help='Open this launcher’s local swarm controls.')
    parser.add_argument('--peer-register', type=Path, help='Register this machine using a private internet launcher invitation file.')
    parser.add_argument('--peer-enroll', action='store_true', help='Enroll saved LAN machines and this coordinator for launcher WebRTC; does not restart services.')
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
    if args.recreate_launcher and (args.release or args.vm or args.peer_enroll or args.peer_register):
        parser.error('--recreate-launcher cannot be combined with --release, --vm or --peer-enroll')
    if args.peer_enroll and args.peer_register:
        parser.error('Choose either LAN enrollment or internet registration')
    if (args.peer_enroll or args.peer_register) and (args.release or args.vm or args.login):
        parser.error('--peer-enroll cannot be combined with --release, --vm or --login')
    if args.release_checkout and not args.release:
        parser.error('--release-checkout requires --release')
    if args.release and (args.vm or args.login):
        parser.error('--release cannot be combined with --vm or --login')
    if args.port in (8769, 8770):
        parser.error('--port conflicts with the MedTech (8769) or LifeTech (8770) preview')
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


def start_site_previews(args):
    """Build and serve both public sites using the existing local services."""
    site = ROOT / 'BmoreMedTech'
    if not (site / 'run.py').is_file():
        raise RuntimeError('Missing BmoreMedTech checkout. Run ./pull-all first.')
    from service_names import service_name
    spec = importlib.util.spec_from_file_location('local_site_run', site / 'run.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for brand, port, hostname in [('medtech', 8769, 'medtech.social'), ('lifetech', 8770, 'lifetech.fyi')]:
        options = argparse.Namespace(
            site_brand=brand, site_port=port, site_container_name=f'{brand}-website',
            node_image=module.DEFAULT_NODE_IMAGE, orgportal_dir=str(ROOT / 'OrgPortal'),
            tenant_host=hostname, medtech_only=False, system_network=args.network,
            org_api_origin=f'http://{service_name(args.prefix, "org")}:{os.getenv("ORGPORTAL_WORKER_PORT") or "8001"}',
            pidp_origin=f'http://{service_name(args.prefix, "pidp-dev")}:8000',
            portal_origin=f'http://{service_name(args.prefix, "portal-dev")}:5173',
            chat_origin=f'http://{service_name(args.prefix, "chat")}:8003',
        )
        module.build_site(options, validate=False)
        module.start_site(options)
        print(f'Local {brand.title()}: https://localhost:{port}/')


def start_codecollective_preview():
    site = ROOT / 'CodeCollective'
    spec = importlib.util.spec_from_file_location('codecollective_local_run', site / 'run.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ensure_datacenters_image()
    module.start_static_site(8877)
    print('Local USAJOBS visualizer: http://localhost:8877/usa/')


def main(argv=None):
    args = parse_args(argv)
    if args.worker_mode:
        folder=Path.home()/'.config/lifetech-launcher';folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        path=folder/'worker-policy.json';temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'mode':args.worker_mode}));temporary.chmod(0o600);temporary.replace(path)
        print('Worker mode: '+args.worker_mode+'; existing jobs finish, submission remains available')
        return 0
    if args.swarm:
        import webbrowser
        sys.path.insert(0,str(ROOT/'scripts'))
        from launcher_peers import owner_launcher_url
        webbrowser.open(owner_launcher_url())
        print('http://127.0.0.1:8791/launcher')
        return 0
    if args.peer_register:
        sys.path.insert(0, str(ROOT / 'scripts'))
        from launcher_registration import register
        print(json.dumps(register(args.peer_register)))
        return 0
    if args.peer_enroll:
        sys.path.insert(0, str(ROOT / 'scripts'))
        from enroll_launcher_peers import enroll
        enroll(ROOT)
        return 0
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
    sys.path.insert(0, str(ROOT / 'scripts'))
    from launcher_peers import start
    stop_peers = start(args.prefix)
    os.environ.setdefault('ORGPORTAL_LOCAL_PREVIEW_ORIGINS', ','.join(
        f'https://{host}:{port}' for port in (8769, 8770) for host in ('localhost', '127.0.0.1')))
    module.run(args.prefix, args.network)
    start_site_previews(args)
    start_codecollective_preview()
    print(f'\nLocal portal: {base}/')
    print(f'Local administrator: {ROOT / "local-login"} --admin')
    if os.getenv('ORGANIZATION_LAUNCHER_CONTAINER'):
        Path('/tmp/launcher-ready').touch()
        print('Startup complete. Launcher is available in the machine container view.')
        def shutdown(*_):
            stop_peers()
            sys.exit(0)
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        while True:
            signal.pause()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'Local startup failed: {error}', file=sys.stderr)
        raise SystemExit(1)
