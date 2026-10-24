"""Public-only replication snapshots over LAN and the existing peer package format."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import time
import urllib.request
import urllib.parse

DEFAULT_SOURCE = 'https://orgportal.cc/api/org/api/network/replication/snapshot'
MAX_BYTES = 32 * 1024 * 1024
# Exact projections from OrgPortal's public organizationSnapshot, not DB exports.
FIELDS = {
    'organizations': 'id name slug description source_url image_url tags city created_at updated_at media_json links_json',
    'support': 'id from_organization_id to_organization_id from_label to_label support_kind amount currency amount_label quantity unit description occurred_at source_url evidence notes provenance_json status void_reason created_at',
    'financingRecipients': 'id name organization_id metadata_json updated_at',
    'financingAgencies': 'id agency_id recipient_id research_json audit_json reviewed_at updated_at',
    'financingEvents': 'id recipient_id agency_id event_type amount currency amount_qualifier occurred_at label investors_json sources_json notes included_in_event_id updated_at tags_json',
}


def validate_snapshot(value):
    if not isinstance(value, dict) or value.get('version') != 3 or set(value) != {'version', *FIELDS}:
        raise ValueError('Only the public version-3 replication projection may be shared')
    for table, names in FIELDS.items():
        rows = value[table]
        if not isinstance(rows, list) or len(rows) > 100000:
            raise ValueError('Invalid public collection')
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != set(names.split()):
                raise ValueError('Unexpected fields in public projection')
            ident = row['id']
            if not isinstance(ident, str) or not ident or ident in seen:
                raise ValueError('Invalid public record identity')
            seen.add(ident)
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Public data redirects are not allowed')


def fetch(url, maximum=MAX_BYTES):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Use a public HTTP(S) URL without credentials or query parameters')
    request = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'OrgPortal-Public-Peers/1.0'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=30) as response:
        body = response.read(maximum + 1)
    if len(body) > maximum:
        raise ValueError('Public data exceeds the size limit')
    return body


def prepare(url, output):
    import tempfile
    from peer_archive import package_file
    value = validate_snapshot(json.loads(fetch(url)))
    with tempfile.TemporaryDirectory(prefix='organization-public-') as folder:
        source = Path(folder) / 'public-snapshot.json'
        source.write_text(json.dumps(value, sort_keys=True, separators=(',', ':')))
        room = package_file(source, output, 'public', int(time.time()) + 86400)
    return {'package': str(output), 'dataset': room, 'visibility': 'public',
            'collections': {name: len(value[name]) for name in FIELDS}}


def load_package(folder):
    folder = folder.resolve()
    path = folder / 'manifest.json'
    if path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise ValueError('Invalid public manifest')
    raw = path.read_bytes()
    manifest = json.loads(raw)
    if (set(manifest) != {'version', 'kind', 'files', 'expires'} or
            manifest.get('version') != 1 or manifest.get('kind') != 'public' or
            not isinstance(manifest.get('expires'), int) or manifest['expires'] <= time.time() or
            len(manifest.get('files', [])) != 1):
        raise ValueError('Only an unexpired public package may be served')
    file = manifest['files'][0]
    if set(file) != {'path', 'size', 'chunks'} or file.get('path') != 'public-snapshot.json' or not isinstance(file.get('size'), int) or not 0 <= file['size'] <= MAX_BYTES:
        raise ValueError('Invalid public snapshot file')
    chunks = {}
    parts = []
    total = 0
    if not isinstance(file.get('chunks'), list) or len(file['chunks']) > 256:
        raise ValueError('Invalid public chunk list')
    chunk_root = folder / 'chunks'
    if chunk_root.is_symlink():
        raise ValueError('Public chunks must not be symlinked')
    for part in file['chunks']:
        if not isinstance(part, dict) or set(part) != {'hash', 'size'}:
            raise ValueError('Unexpected public chunk metadata')
        sha = part.get('hash')
        if not isinstance(sha, str) or not re.fullmatch('[a-f0-9]{64}', sha):
            raise ValueError('Invalid public chunk hash')
        if not isinstance(part.get('size'), int) or not 1 <= part['size'] <= 256 * 1024:
            raise ValueError('Invalid public chunk size')
        path = chunk_root / sha
        if path.is_symlink() or path.stat().st_size != part['size']:
            raise ValueError('Invalid public chunk')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            raise ValueError('Corrupted public chunk')
        total += len(data)
        if total > MAX_BYTES:
            raise ValueError('Public snapshot exceeds the size limit')
        chunks[sha] = data
        parts.append(data)
    snapshot = b''.join(parts)
    if total != file['size']:
        raise ValueError('Incomplete public snapshot')
    validate_snapshot(json.loads(snapshot))
    return manifest, raw, snapshot, chunks


def serve(folder, bind, port):
    # Validate and pin bytes before opening the socket. Never serve a filesystem
    # tree: invitations, keys, working copies and DR archives have no route here.
    manifest, raw, snapshot, chunks = load_package(folder)
    dataset = hashlib.sha256(raw).hexdigest()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_HEAD(self):
            self.do_GET(head=True)

        def do_GET(self, head=False):
            path = urllib.parse.urlsplit(self.path).path
            if time.time() >= manifest['expires']:
                self.send_error(410, 'Public snapshot expired; prepare a fresh package')
                return
            if path == '/manifest.json':
                body, kind = raw, 'application/json'
            elif path == '/snapshot.json':
                body, kind = snapshot, 'application/json'
            elif path.startswith('/chunks/') and path[8:] in chunks:
                body, kind = chunks[path[8:]], 'application/octet-stream'
            elif path == '/health':
                body = json.dumps({'service': 'organization-public-data', 'visibility': 'public',
                                   'dataset': dataset, 'expires': manifest['expires']}).encode()
                kind = 'application/json'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            if not head:
                self.wfile.write(body)

    print(json.dumps({'listening': f'http://{bind}:{port}', 'dataset': dataset, 'visibility': 'public', 'expires': manifest['expires']}), flush=True)
    ThreadingHTTPServer((bind, port), Handler).serve_forever()
