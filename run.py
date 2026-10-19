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
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', default='bmoremedtech-', help='Existing local volume namespace (default: bmoremedtech-).')
    parser.add_argument('--network', default='bmoremedtech', help='Docker network (default: bmoremedtech).')
    parser.add_argument('--port', type=int, default=os.getenv('ORGPORTAL_LOCAL_GATEWAY_PORT', '8443'), help='Local HTTPS port (default: 8443).')
    parser.add_argument('--login', choices=['member', 'admin'], help='Sign your browser in after startup.')
    parser.add_argument('--no-open', action='store_true', help='Print the one-use login link instead of opening a browser.')
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error('--port must be between 1024 and 65535')
    if args.no_open and not args.login:
        parser.error('--no-open requires --login')
    return args


def main(argv=None):
    args = parse_args(argv)
    if not (ROOT / 'OrgPortal' / 'run.py').is_file() or not (ROOT / 'PIdP' / 'run.py').is_file():
        raise RuntimeError('Missing application repositories. Run ./pull-all first.')
    sys.stdout.reconfigure(line_buffering=True)
    context = subprocess.run(['docker','context','inspect','--format','{{json .Endpoints.docker.Host}}'], capture_output=True, text=True, check=True)
    if not (os.getenv('DOCKER_HOST') or json.loads(context.stdout)).startswith('unix://'):
        raise RuntimeError('Use a local Docker context for this local deployment.')
    base = (os.getenv('ORGPORTAL_LOCAL_PUBLIC_BASE_URL') or f'https://192.168.50.141:{args.port}').rstrip('/')
    host = urlsplit(base).hostname
    if host != 'localhost' and not ipaddress.ip_address(host).is_private:
        raise RuntimeError('Use a private network address for this deployment.')
    os.environ.setdefault('ORGPORTAL_LOCAL_BIND_ADDRESS', '192.168.50.141')
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
    if args.login:
        from service_names import service_name
        command = [str(ROOT / 'local-login'), '--base', base, '--container', service_name(args.prefix, 'pidp-dev')]
        if args.login == 'admin': command.append('--admin')
        if args.no_open: command.append('--no-open')
        subprocess.run(command, check=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'Local startup failed: {error}', file=sys.stderr)
        raise SystemExit(1)
