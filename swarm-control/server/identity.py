"""PIdP authorization-code recovery, bound to the configured CompAI owner."""
import base64
import hashlib
import hmac
import json
import math
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
import devices

CONFIG = Path.home() / '.config/swarm-control/identity.json'
PENDING = {}
RECOVERIES = {}
LOCK = threading.Lock()
LOGIN_COOKIE = '__Host-compai_login'


class Denied(ValueError):
    pass

class ReplacementRequired(Exception):
    def __init__(self,receipt):
        self.receipt=receipt
        super().__init__('Explicit controller replacement required.')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise Denied('PIdP unexpectedly redirected a server request.')


def fetch(url, data=None, headers=None):
    request = Request(url, data=data, headers={'User-Agent': 'CompAI-PIdP/1.0', **(headers or {})})
    with build_opener(NoRedirect()).open(request, timeout=10) as response:
        raw = response.read(131073)
    if len(raw) > 131072:
        raise Denied('PIdP response exceeded the size limit.')
    return json.loads(raw)


def config(origin):
    value = json.loads(CONFIG.read_text())
    if CONFIG.stat().st_mode & 0o077:
        raise Denied('PIdP configuration must be owner-private.')
    issuer = value['issuer']
    parsed = urlsplit(issuer)
    if parsed.scheme != 'https' or parsed.netloc != parsed.hostname or parsed.path or parsed.query or parsed.fragment:
        raise Denied('Invalid PIdP issuer.')
    for key in ('authorization_endpoint', 'token_endpoint', 'jwks_uri','introspection_endpoint'):
        endpoint = urlsplit(value[key])
        if endpoint.scheme != 'https' or endpoint.netloc != parsed.netloc or endpoint.fragment or endpoint.query:
            raise Denied('PIdP endpoints must belong to the configured issuer.')
    if value['redirect_uri'] != origin + '/auth/callback' or not value.get('owner_subject') or not value.get('client_secret') or not value.get('client_id'):
        raise Denied('PIdP owner or callback is not configured.')
    if value['resource'] != origin + '/api/swarm' or value.get('scope') != 'swarm:control':
        raise Denied('Swarm Control requires its own resource and permission.')
    if not value.get('owner_canonical_user_id') or len(value.get('resource_secret',''))<32:
        raise Denied('Configure the verified PIdP owner and resource credential.')
    return value


def start(origin,current_device=None,label='Galaxy'):
    cfg = config(origin)
    if not isinstance(label,str) or not 1<=len(label.strip())<=64 or any(ord(c)<32 for c in label):
        raise Denied('Enter a device name of 1–64 characters.')
    label=label.strip()
    state, browser, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    with LOCK:
        for key in list(PENDING):
            if PENDING[key]['expires'] <= time.time():
                del PENDING[key]
        if len(PENDING) >= 128:
            raise Denied('Too many pending sign-ins. Try again shortly.')
        PENDING[state] = {'browser': hashlib.sha256(browser.encode()).digest(), 'verifier': verifier,
                          'expires': time.time() + 600,
                          'label':label,
                          'device':devices.digest(current_device) if devices.valid(current_device) else None}
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    url = cfg['authorization_endpoint'] + '?' + urlencode({
        'response_type': 'code', 'client_id': cfg['client_id'], 'redirect_uri': cfg['redirect_uri'],
        'resource': cfg['resource'], 'scope': cfg['scope'], 'state': state,
        'code_challenge': challenge, 'code_challenge_method': 'S256'})
    return url, f'{LOGIN_COOKIE}={browser}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=600'


def decode(value):
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def verify(token, cfg):
    header_part, payload_part, signature_part = token.split('.')
    header, claims = json.loads(decode(header_part)), json.loads(decode(payload_part))
    if header.get('alg') != 'ES256' or not isinstance(header.get('kid'), str):
        raise Denied('PIdP signing algorithm is invalid.')
    keys = fetch(cfg['jwks_uri'])['keys']
    matching = [key for key in keys if key.get('kid') == header['kid'] and key.get('kty') == 'EC' and key.get('crv') == 'P-256']
    if len(matching) != 1:
        raise Denied('PIdP signing key is unavailable.')
    key = matching[0]
    public = ec.EllipticCurvePublicNumbers(int.from_bytes(decode(key['x']), 'big'),
                                          int.from_bytes(decode(key['y']), 'big'), ec.SECP256R1()).public_key()
    signature = decode(signature_part)
    if len(signature) != 64:
        raise Denied('Invalid PIdP signature.')
    public.verify(utils.encode_dss_signature(int.from_bytes(signature[:32], 'big'), int.from_bytes(signature[32:], 'big')),
                  (header_part + '.' + payload_part).encode(), ec.ECDSA(hashes.SHA256()))
    now = time.time()
    for name in ('exp', 'iat'):
        if type(claims.get(name)) not in (int, float) or not math.isfinite(claims[name]):
            raise Denied('Invalid PIdP token lifetime.')
    if not claims['iat'] <= now + 30 or not now < claims['exp'] <= claims['iat'] + 600:
        raise Denied('PIdP token expired or is not yet valid.')
    if claims.get('iss') != cfg['issuer'] or claims.get('aud') != cfg['resource'] or claims.get('client_id') != cfg['client_id'] or str(claims.get('scope', '')).split() != [cfg['scope']]:
        raise Denied('PIdP token is for a different resource.')
    identity=fetch(cfg['introspection_endpoint'],urlencode({'token':token,'resource':cfg['resource']}).encode(),
                   {'Content-Type':'application/x-www-form-urlencoded','Authorization':'Bearer '+cfg['resource_secret']})
    if identity.get('active') is not True or identity.get('sub')!=claims.get('sub') or identity.get('iss')!=cfg['issuer'] or identity.get('aud')!=cfg['resource'] or identity.get('scope')!=cfg['scope']:
        raise Denied('PIdP did not confirm an active Swarm Control grant.')
    canonical=identity.get('canonical_user_id')
    if not isinstance(canonical,str) or not hmac.compare_digest(canonical,cfg['owner_canonical_user_id']):
        raise Denied('This PIdP account is not the CompAI owner.')


def complete(origin, query, browser):
    cfg = config(origin)
    if set(query) != {'state', 'code', 'iss'} or any(len(v) != 1 or not 1 <= len(v[0]) <= 4096 for v in query.values()):
        raise Denied('Invalid PIdP callback.')
    if query['iss'][0] != cfg['issuer']:
        raise Denied('PIdP callback issuer does not match.')
    if not isinstance(browser, str) or not 20 <= len(browser) <= 128:
        raise Denied('Restart sign-in in this browser.')
    with LOCK:
        pending = PENDING.get(query['state'][0])
        if not pending or pending['expires'] <= time.time() or not hmac.compare_digest(pending['browser'], hashlib.sha256(browser.encode()).digest()):
            raise Denied('Sign-in expired or belongs to another browser.')
        del PENDING[query['state'][0]]  # Never replay an ambiguous token exchange.
    basic = base64.b64encode((cfg['client_id'] + ':' + cfg['client_secret']).encode()).decode()
    tokens = fetch(cfg['token_endpoint'], urlencode({
        'grant_type': 'authorization_code', 'code': query['code'][0], 'redirect_uri': cfg['redirect_uri'],
        'code_verifier': pending['verifier'], 'resource': cfg['resource']}).encode(),
        {'Content-Type': 'application/x-www-form-urlencoded', 'Authorization': 'Basic ' + basic})
    verify(tokens['access_token'], cfg)
    # No PIdP token is sent to the browser or retained as a remote credential.
    with devices.state() as value:
        if value['sessions'] and pending['device'] not in value['sessions']:
            receipt=secrets.token_urlsafe(32)
            with LOCK:
                for key in list(RECOVERIES):
                    if RECOVERIES[key]['expires']<=time.time():del RECOVERIES[key]
                if len(RECOVERIES)>=128:raise Denied('Too many pending replacements.')
                RECOVERIES[devices.digest(receipt)]={'expires':time.time()+120,'label':pending['label']}
            raise ReplacementRequired(receipt)
        value['pairs'] = {}
        return devices.new_session(value,pending['label'])


def replace_controller(receipt):
    if not isinstance(receipt,str) or not 20<=len(receipt)<=128:
        raise Denied('Replacement requires a fresh owner sign-in.')
    with LOCK:
        record=RECOVERIES.pop(devices.digest(receipt),None)
    if not record or record['expires']<=time.time():raise Denied('Replacement expired. Sign in again.')
    with devices.state() as value:
        value['pairs']={}
        return devices.new_session(value,record['label'])
