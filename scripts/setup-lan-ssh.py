#!/usr/bin/env python3
"""Install a dedicated public SSH key on LAN peers using interactive SSH login."""
import argparse
import concurrent.futures
import datetime
import getpass
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys


def run(command, **kwargs):
    return subprocess.run(command, check=False, **kwargs)


def discover():
    """Find mDNS service hosts and reverse-resolve directly observed LAN neighbors."""
    names = set()
    named_addresses = set()
    result = run(['avahi-browse', '--all', '--resolve', '--terminate', '--parsable'],
                 capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise ValueError(result.stderr.strip() or 'mDNS discovery failed; check Avahi.')
    local = socket.gethostname().split('.')[0].lower() + '.local'
    links = json.loads(subprocess.check_output(['ip', '-j', 'addr'], text=True))
    interfaces = {link['ifname'] for link in links
                  if link['ifname'].startswith(('en', 'eth', 'wl')) and link.get('operstate') == 'UP'}
    own = {address['local'] for link in links for address in link.get('addr_info', [])}
    for line in result.stdout.splitlines():
        fields = line.split(';')
        if len(fields) >= 9 and fields[0] == '=' and fields[1] in interfaces and fields[7] not in own:
            name = fields[6].rstrip('.').lower()
            if name.endswith('.local') and name != local:
                names.add(name)
                named_addresses.add(fields[7])
    neighbors = json.loads(subprocess.check_output(['ip', '-j', 'neigh'], text=True))
    peers = [row for row in neighbors if row.get('lladdr') and row.get('dev') in interfaces and row['dst'] not in own]
    # A single device may publish only IPv6 mDNS. Resolve both address families,
    # then group by interface and MAC so an unanswered IPv4 PTR is not a second device.
    addresses = sorted({row['dst'] for row in peers if row['dst'] not in named_addresses})

    def reverse(address):
        try:
            response = run(['avahi-resolve-address', address], capture_output=True, text=True, timeout=5)
            fields = response.stdout.split()
            name = fields[-1].rstrip('.').lower() if response.returncode == 0 and len(fields) > 1 else ''
            return address, name if name.endswith('.local') else None
        except subprocess.TimeoutExpired:
            return address, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        resolved = dict(pool.map(reverse, addresses))
    devices = {}
    for row in peers:
        device = devices.setdefault((row['dev'], row['lladdr']), [])
        device.append(row['dst'])
    missing = []
    for (interface, mac), device_addresses in devices.items():
        device_names = {resolved[address] for address in device_addresses if resolved.get(address)}
        names.update(name for name in device_names if name != local)
        if device_names or any(address in named_addresses for address in device_addresses):
            continue
        address = next((address for address in device_addresses if ':' not in address), device_addresses[0])
        missing.append({'address': address, 'addresses': device_addresses, 'interface': interface,
                        'mac': mac, 'status': 'No mDNS name discovered; skipped'})
    return sorted(names), missing


def ssh_port(host):
    try:
        with socket.create_connection((host, 22), timeout=0.7):
            return 'open'
    except ConnectionRefusedError:
        return 'SSH refused'
    except OSError:
        return 'unreachable or SSH filtered'


def destination(value, default_user):
    user, host = value.rsplit('@', 1) if '@' in value else (default_user, value)
    host = host.rstrip('.').lower()
    if user is None:
        user = 'codecollective' if host == 'codecollective.local' else getpass.getuser()
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*', user):
        raise ValueError(f'Invalid username: {user}')
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.local', host):
        raise ValueError(f'Use an mDNS hostname such as laptop.local, not an IP address: {host}')
    return user, host


def save_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Replace atomically; never leave a partially written configuration/report.
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, indent=2)
        stream.write('\n')
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def resolved_addresses(host):
    try:
        return {entry[4][0].split('%')[0] for entry in socket.getaddrinfo(host, 22, type=socket.SOCK_STREAM)}
    except OSError:
        return set()


def prepare_key(key):
    public = Path(str(key) + '.pub')
    key.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if public.exists() and not key.exists():
        raise ValueError(f'Public key exists without private key: {public}; choose another --key path.')
    if not key.exists():
        if run(['ssh-keygen', '-t', 'ed25519', '-f', str(key), '-C', 'OrgPortal LAN test access']).returncode:
            return False
    elif not public.exists():
        result = run(['ssh-keygen', '-y', '-f', str(key)], text=True, stdout=subprocess.PIPE)
        if result.returncode:
            return False
        public.write_text(result.stdout)
    os.chmod(key, 0o600)
    if os.environ.get('SSH_AUTH_SOCK') and shutil.which('ssh-add'):
        run(['ssh-add', str(key)])
    return True


def main():
    state = Path.home()/'.local/state/orgportal'
    parser = argparse.ArgumentParser(description=__doc__, epilog='Verifies existing keys first, then asks about remaining mDNS hosts. Devices without mDNS names are skipped automatically. All SSH uses .local mDNS names. Passwords go directly to SSH and are never stored. Does not install or enable SSH servers.')
    parser.add_argument('hosts', nargs='*', help='hostname.local or username@hostname.local')
    parser.add_argument('--user', help='Override saved/default remote usernames')
    parser.add_argument('--key', type=Path, default=Path.home()/'.ssh/orgportal_lan_ed25519')
    parser.add_argument('--check', action='store_true', help='Check existing access without prompting or installing keys')
    parser.add_argument('--config', type=Path, default=state/'lan-ssh-devices.json')
    parser.add_argument('--report', type=Path, default=state/'lan-ssh-report.json')
    args = parser.parse_args()
    required = ['ssh'] + ([] if args.check else ['ssh-keygen', 'ssh-copy-id']) + ([] if args.hosts else ['ip', 'avahi-browse', 'avahi-resolve-address'])
    for command in required:
        if not shutil.which(command):
            parser.error(f'Required program missing: {command}')
    if not args.check and not sys.stdin.isatty():
        parser.error('Run in an interactive terminal, or use --check.')
    try:
        config = json.loads(args.config.read_text()) if args.config.exists() else {}
        accounts = config.setdefault('accounts', {})
        names = config.setdefault('names_by_mac', {})
        config.pop('ignored', None)
        if not isinstance(accounts, dict) or not isinstance(names, dict):
            raise ValueError('Invalid device configuration')
        hosts, missing = (args.hosts, []) if args.hosts else discover()
        # Saved hostnames are retried even when no service is advertised today.
        if not args.hosts:
            hosts = sorted(set(hosts) | set(accounts) | set(names.values()))
        targets = list(dict.fromkeys(destination(h, args.user or accounts.get(h)) for h in hosts))
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        parser.error(str(exc))
    report = []
    # Forward mDNS can succeed even when reverse mDNS has no PTR record.
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        known_addresses = set().union(set(), *pool.map(resolved_addresses, [host for _, host in targets]))
    key = args.key.expanduser().resolve()
    def verify_account(target):
        user, host = target
        status = ssh_port(host)
        if status != 'open':
            return status
        options = ['-o', f'Hostname={host}', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=accept-new', '-o', 'IdentitiesOnly=yes']
        if key.exists():
            options += ['-i', str(key)]
        probe = run(['ssh', *options, '-o', 'BatchMode=yes', f'{user}@{host}', 'true'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return 'key access verified' if probe.returncode == 0 else 'key access unavailable'

    # Finish the read-only verification pass before asking about any device.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        checked = dict(zip(targets, pool.map(verify_account, targets)))
    for (user, host), status in checked.items():
        print(f'{user}@{host}: {status}', flush=True)
    for device in missing:
        if any(address in known_addresses for address in device.get('addresses', [device['address']])):
            continue
        print(f"{device['address']}: no mDNS name discovered; skipped")
        report.append({**device, 'status': 'skipped: no mDNS name'})
    prepared = False
    selected = list(dict.fromkeys(targets))
    for user, host in selected:
        target = f'{user}@{host}'
        status = checked.get((user, host)) or verify_account((user, host))
        port = 'open' if status in ('key access verified', 'key access unavailable') else status
        options = ['-o', f'Hostname={host}', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=accept-new', '-o', 'IdentitiesOnly=yes']
        if key.exists():
            options += ['-i', str(key)]
        if status == 'key access verified' and not args.check:
            accounts[host] = user
            save_json(args.config, config)
        if status != 'key access verified' and not args.check:
            while True:
                print(f'\n{target}: {status}')
                action = input('[i] Install/verify key, [c] change username, [s] skip once [s]: ').strip().lower()
                if action in ('', 's'):
                    status = 'skipped'
                    break
                if action == 'c':
                    try:
                        user, _ = destination(input('Remote username: ').strip() + '@' + host, None)
                        target = f'{user}@{host}'
                    except ValueError as exc:
                        print(exc)
                        continue
                    if port == 'open' and run(['ssh', *options, '-o', 'BatchMode=yes', target, 'true'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                        status = 'key access verified'
                        accounts[host] = user
                        save_json(args.config, config)
                        break
                    continue
                if action != 'i':
                    continue
                if ssh_port(host) != 'open':
                    print('SSH is unavailable. Enable SSH/mDNS on this device, then retry.')
                    continue
                if not prepared:
                    try:
                        prepared = prepare_key(key)
                    except ValueError as exc:
                        print(exc)
                    if not prepared:
                        status = 'key preparation failed'
                        break
                print(f"Installing public key on {target}; SSH may ask for that account's password.")
                public = Path(str(key) + '.pub')
                copied = run(['ssh-copy-id', '-o', f'Hostname={host}', '-i', str(public), '-o', f'IdentityFile={key}', '-o', 'IdentitiesOnly=yes', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=accept-new', target])
                if copied.returncode:
                    status = 'key installation failed'
                    continue
                verified = run(['ssh', '-i', str(key), '-o', f'Hostname={host}', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=accept-new', '-o', 'IdentitiesOnly=yes', '-o', 'PasswordAuthentication=no', '-o', 'KbdInteractiveAuthentication=no', target, 'true'])
                status = 'key access verified' if verified.returncode == 0 else 'installed but verification failed'
                if verified.returncode == 0:
                    accounts[host] = user
                    save_json(args.config, config)
                    break
        report.append({'target': target, 'status': status})
        if checked.get((user, host)) != status:
            print(f'{target}: {status}')
    save_json(args.report, {'observed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'key': str(key), 'check_only': args.check, 'devices': report})
    print(f'\nReport: {args.report}')
    print(f'Use this key for test jobs: ssh -i {key} USER@HOST.local')
    return 0 if report and all(r['status'] in ('key access verified', 'skipped', 'skipped: no mDNS name') for r in report) else 1


if __name__ == '__main__':
    sys.exit(main())
