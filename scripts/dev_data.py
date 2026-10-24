#!/usr/bin/env python3
"""Full local database/media baselines and independent writable dev checkouts."""
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import uuid

from recovery import DEFAULT_STORE, digest, inspect_sqlite, now, private_json, resolve_snapshot, restore_sql, safe_child

DEFAULT_DEV = Path('/mnt/PILEDRIVE/organization-dev')


def fresh(manifest):
    if dt.datetime.fromisoformat(manifest['expiresAt']) <= dt.datetime.now(dt.timezone.utc):
        raise RuntimeError('Dev data expired. Refresh and create a new checkout.')


def load(root, pointer):
    record = json.loads((root / pointer).read_text())
    folder = safe_child(root, record['path'])
    manifest = json.loads((folder / 'manifest.json').read_text())
    fresh(manifest)
    return folder, manifest


def refresh(store, root):
    source = resolve_snapshot(store, None)
    manifest = json.loads((source / 'manifest.json').read_text())
    if not manifest.get('completedAt') or set(manifest['databases']) != set(manifest['inventory']['databases']) or set(manifest['objects']) != set(manifest['inventory']['buckets']):
        raise RuntimeError('Source archive does not cover its declared data inventory')
    baselines = root / 'baselines'; baselines.mkdir(exist_ok=True, mode=0o700)
    final = baselines / source.name
    if final.exists():
        existing = json.loads((final / 'manifest.json').read_text()); fresh(existing)
        if existing['sourceManifestSha256'] != digest(source / 'manifest.json'):
            raise RuntimeError('Immutable source archive changed')
        private_json(root / 'latest.json', {'path': str(final.relative_to(root))})
        return final
    pending = Path(tempfile.mkdtemp(prefix='.pending-', dir=baselines))
    objects = root / 'objects'; objects.mkdir(exist_ok=True, mode=0o700)
    try:
        databases = {}; buckets = {}; (pending / 'databases').mkdir(mode=0o700)
        (pending / 'buckets').mkdir(mode=0o700)
        for ident, entry in manifest['databases'].items():
            name = entry['database_name']
            if not re.fullmatch(r'[a-zA-Z0-9_-]+', name) or name in databases:
                raise RuntimeError('Database names must be unique and filesystem safe')
            relative = 'databases/' + ident + '/export.sql.gz'
            sql = safe_child(source, relative)
            if digest(sql) != manifest['files'][relative]: raise RuntimeError('Database archive hash mismatch')
            target = pending / 'databases' / (name + '.sqlite')
            checked = restore_sql(sql, target)
            if checked['tables'] != entry['tables'] or checked['foreignKeyViolations'] != entry['foreignKeyViolations']:
                raise RuntimeError('Dev database differs from verified archive')
            target.chmod(0o400)
            databases[name] = {'tables': checked['tables'], 'sha256': digest(target)}
            print('Prepared dev database:', name, flush=True)
        for bucket, records in manifest['objects'].items():
            if not re.fullmatch(r'[a-zA-Z0-9_.-]+', bucket): raise RuntimeError('Invalid bucket name')
            for record in records:
                sha = record['sha256']
                if not re.fullmatch(r'[a-f0-9]{64}', sha): raise RuntimeError('Invalid object digest')
                target = objects / sha
                if not target.exists():
                    original = safe_child(store / 'objects', sha)
                    temporary = objects / ('.pending-' + uuid.uuid4().hex)
                    try:
                        # Independent inodes: dev writes can never corrupt the DR archive.
                        shutil.copyfile(original, temporary)
                        if temporary.stat().st_size != record['size'] or digest(temporary) != sha:
                            raise RuntimeError('Dev object hash mismatch')
                        temporary.chmod(0o400); temporary.replace(target)
                    finally: temporary.unlink(missing_ok=True)
                elif target.stat().st_size != record['size'] or digest(target) != sha:
                    raise RuntimeError('Existing dev object is corrupt')
            private_json(pending / 'buckets' / (bucket + '.json'), records)
            buckets[bucket] = {'objects': len(records), 'bytes': sum(r['size'] for r in records)}
            print('Prepared dev bucket:', bucket, len(records), flush=True)
        if (source/'durable-objects').exists():
            for path in (source/'durable-objects').rglob('*'):
                if path.is_file() and digest(path)!=manifest['files'][str(path.relative_to(source))]:raise RuntimeError('Durable registration archive hash mismatch')
            shutil.copytree(source/'durable-objects',pending/'durable-objects')
            for path in (pending/'durable-objects').rglob('*'):
                if path.is_file():path.chmod(0o400)
        private_json(pending / 'manifest.json', {
            'version': 1, 'createdAt': now(), 'sourceSnapshot': source.name,
            'sourceManifestSha256': digest(source / 'manifest.json'),
            'expiresAt': (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7)).isoformat(),
            'databases': databases, 'buckets': buckets, 'dataComplete': True,
            'serviceRecoveryComplete': not bool(manifest['gaps']), 'recoveryGaps': manifest['gaps'],
            'productionWrites': False, 'productionRuntimeConfigurationIncluded': False,
            'launcherRegistrations':manifest.get('launcherRegistrations'),
        })
        pending.rename(final)
        private_json(root / 'latest.json', {'path': str(final.relative_to(root))})
        return final
    except BaseException:
        shutil.rmtree(pending); raise


def checkout(root):
    baseline, manifest = load(root, 'latest.json')
    folder = root / 'workspaces' / (dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, mode=0o700); (folder / 'databases').mkdir(mode=0o700)
    for name, entry in manifest['databases'].items():
        source = baseline / 'databases' / (name + '.sqlite')
        if digest(source) != entry['sha256']: raise RuntimeError('Baseline database hash mismatch')
        target = folder / 'databases' / source.name
        shutil.copyfile(source, target); target.chmod(0o600)
        if inspect_sqlite(target)['tables'] != entry['tables']: raise RuntimeError('Checkout table counts changed')
    private_json(folder / 'manifest.json', {**manifest, 'baseline': str(baseline.relative_to(root)), 'createdAt': now()})
    private_json(root / 'current.json', {'path': str(folder.relative_to(root))})
    return folder


def purge(root):
    removed = 0
    for kind in ('baselines', 'workspaces'):
        for folder in (root / kind).glob('*'):
            if folder.is_symlink() or not folder.is_dir(): continue
            if folder.name.startswith('.pending-'):
                shutil.rmtree(folder); removed += 1; continue
            path = folder / 'manifest.json'
            if not path.exists(): continue
            value = json.loads(path.read_text())
            if dt.datetime.fromisoformat(value['expiresAt']) <= dt.datetime.now(dt.timezone.utc):
                shutil.rmtree(folder); removed += 1
    # Keep only media referenced by an unexpired baseline/workspace.
    wanted = set()
    for index in (root / 'baselines').glob('*/buckets/*.json'):
        wanted.update(row['sha256'] for row in json.loads(index.read_text()))
    for blob in (root / 'objects').glob('*'):
        if blob.name.startswith('.pending-'): blob.unlink(); continue
        if re.fullmatch(r'[a-f0-9]{64}', blob.name) and blob.name not in wanted: blob.unlink()
    print(json.dumps({'expiredCopiesRemoved': removed}))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path, default=DEFAULT_STORE)
    parser.add_argument('--directory', type=Path, default=DEFAULT_DEV)
    commands = parser.add_subparsers(dest='action', required=True)
    for name in ('refresh', 'checkout', 'status', 'purge'): commands.add_parser(name)
    shell = commands.add_parser('shell'); shell.add_argument('database')
    args = parser.parse_args(); root = args.directory.resolve()
    if root == args.store.resolve() or root.is_relative_to(args.store.resolve()) or args.store.resolve().is_relative_to(root):
        raise RuntimeError('Dev storage must be separate from DR storage')
    root.mkdir(parents=True, exist_ok=True, mode=0o700); root.chmod(0o700)
    with (root / '.lock').open('a') as lock:
        if args.action != 'status': fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == 'refresh': print(json.dumps({'baseline': str(refresh(args.store, root))}))
        elif args.action == 'checkout': print(json.dumps({'workspace': str(checkout(root))}))
        elif args.action == 'purge': purge(root)
        elif args.action == 'status':
            result = {'directory': str(root)}
            for key, pointer in [('baseline', 'latest.json'), ('workspace', 'current.json')]:
                try:
                    folder, value = load(root, pointer)
                    result[key] = {'path': str(folder), **{k: value[k] for k in ('sourceSnapshot', 'expiresAt', 'dataComplete', 'serviceRecoveryComplete', 'databases', 'buckets')}}
                except (FileNotFoundError, RuntimeError) as error: result[key] = {'available': False, 'reason': str(error)}
            print(json.dumps(result, indent=2))
        elif args.action == 'shell':
            folder, value = load(root, 'current.json')
            if args.database not in value['databases']: raise RuntimeError('Unknown database')
            # No production env, host credentials, network, or published ports.
            command = ['docker', 'run', '--rm', '-it', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                       '--security-opt', 'no-new-privileges', '--user', str(os.getuid()) + ':' + str(os.getgid()),
                       '--tmpfs', '/tmp:rw,nosuid,noexec,size=32m', '-e', 'HOME=/tmp',
                       '--mount', 'type=bind,src=' + str(folder / 'databases') + ',dst=/data',
                       '--mount', 'type=bind,src=' + str(root / 'objects') + ',dst=/objects,readonly',
                       '--mount', 'type=bind,src=' + str(root / value['baseline'] / 'buckets') + ',dst=/buckets,readonly',
                       'python:3.12-slim', 'python', '-m', 'sqlite3', '/data/' + args.database + '.sqlite']
            fcntl.flock(lock, fcntl.LOCK_UN)
            raise SystemExit(subprocess.call(command))


if __name__ == '__main__': main()
