#!/usr/bin/env python3
"""Register CompAI recovery with PIdP; keep all credentials owner-private."""
import json
import os
from pathlib import Path
import codex_pidp


def main():
    origin = os.getenv('CODEX_PUBLIC_ORIGIN', 'https://compai.lifetech.fyi')
    issuer, resource = 'https://orgportal.cc', 'https://lifetech.fyi/api/org/mcp'
    # Use the existing explicitly authenticated working-copy identity, never email.
    owners = set()
    for path in (Path.home() / '.orgportal-working-copy').glob('*.json'):
        binding = json.loads(path.read_text()).get('binding', {})
        if binding.get('issuer') == issuer and binding.get('resource') == resource and binding.get('subject'):
            owners.add(binding['subject'])
    if len(owners) != 1:
        raise RuntimeError('Exactly one previously authenticated working-copy owner is required.')
    owner = owners.pop()
    if codex_pidp.CONFIG.exists():
        cfg = codex_pidp.config(origin)
        if cfg['owner_subject'] != owner:
            raise RuntimeError('Existing CompAI owner differs; configuration was preserved.')
        print('Existing owner-restricted PIdP registration is ready.')
        return
    metadata = codex_pidp.fetch(issuer + '/.well-known/oauth-authorization-server')
    if metadata['issuer'] != issuer or metadata['registration_endpoint'] != issuer + '/oauth/mcp/register':
        raise RuntimeError('Unexpected PIdP authorization server.')
    redirect = origin + '/auth/callback'
    registration = codex_pidp.fetch(metadata['registration_endpoint'], json.dumps({
        'client_name': 'CompAI remote control sign-in', 'redirect_uris': [redirect],
        'token_endpoint_auth_method': 'client_secret_basic', 'grant_types': ['authorization_code'],
        'response_types': ['code'], 'scope': 'org:portal.read'}).encode(), {'Content-Type': 'application/json'})
    cfg = {key: metadata[key] for key in ('issuer', 'authorization_endpoint', 'token_endpoint', 'jwks_uri')}
    cfg.update({key: registration[key] for key in ('client_id', 'client_secret')})
    cfg.update(redirect_uri=redirect, resource=resource, owner_subject=owner)
    codex_pidp.CONFIG.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(codex_pidp.CONFIG, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(cfg, stream)
    codex_pidp.config(origin)
    print('Registered owner-restricted PIdP recovery for ' + origin + '/auth/login')


if __name__ == '__main__':
    main()
