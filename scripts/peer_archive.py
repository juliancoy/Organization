#!/usr/bin/env python3
"""Prepare scoped test fixtures or encrypted DR files and issue device invitations."""
import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
import time
import urllib.parse
import uuid

from recovery import DEFAULT_STORE, digest, private_json, resolve_snapshot
from testing_copy import archive_payload

CHUNK_SIZE=256*1024
DEFAULT_KEY=Path.home()/'.config/organization-peers/signing.key'
DEFAULT_URL='https://organization-archive-peers.jcloiacon.workers.dev'


def encoded(value):
    return base64.urlsafe_b64encode(json.dumps(value,separators=(',',':')).encode()).rstrip(b'=').decode()


def ticket(secret,room,ttl=3600,audience='archive-peers-v1'):
    if len(secret)<32 or not 1<=ttl<=3600:raise RuntimeError('A signing key and a lifetime of 1–3600 seconds are required')
    now=int(time.time());peer=str(uuid.uuid4())
    body={'aud':audience,'room':room,'peer':peer,'iat':now,'exp':now+ttl}
    value=encoded({'alg':'HS256','typ':'JWT'})+'.'+encoded(body)
    signature=base64.urlsafe_b64encode(hmac.new(secret.encode(),value.encode(),hashlib.sha256).digest()).rstrip(b'=').decode()
    return body,value+'.'+signature


def package_file(source,output,kind,expires=None,source_manifest=None):
    if output.exists():raise RuntimeError('Package output must be a new directory')
    if kind not in {'testing','encrypted-dr','public'}:raise RuntimeError('Unknown dataset kind')
    if kind=='public':
        from public_data import MAX_BYTES,validate_snapshot
        if source_manifest is not None:raise RuntimeError('Public packages must not carry DR metadata')
        if source.stat().st_size>MAX_BYTES:raise RuntimeError('Public data exceeds the size limit')
        validate_snapshot(json.loads(source.read_bytes()))
    if kind=='encrypted-dr':
        with source.open('rb') as stream:
            magic=stream.read(64)
        if not magic.startswith((b'age-encryption.org/v1\n',b'-----BEGIN AGE ENCRYPTED FILE-----')):
            raise RuntimeError('DR input must be an age-encrypted archive; plaintext recovery data cannot be shared')
    output.mkdir(parents=True,mode=0o700);chunks=output/'chunks';chunks.mkdir(mode=0o700)
    parts=[];size=0
    with source.open('rb') as stream:
        for chunk in iter(lambda:stream.read(CHUNK_SIZE),b''):
            sha=hashlib.sha256(chunk).hexdigest();path=chunks/sha
            if not path.exists():path.write_bytes(chunk);path.chmod(0o600)
            parts.append({'hash':sha,'size':len(chunk)});size+=len(chunk)
    name={'testing':'testing-copy.json','encrypted-dr':'recovery.tar.age','public':'public-snapshot.json'}[kind]
    manifest={'version':1,'kind':kind,'files':[{'path':name,'size':size,'chunks':parts}]}
    if kind in {'testing','public'}:manifest['expires']=expires or int(time.time())+(86400 if kind=='public' else 7*86400)
    if source_manifest:manifest['sourceManifestSha256']=source_manifest
    raw=json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()
    if len(raw)>8*1024*1024:raise RuntimeError('Dataset manifest exceeds browser limit; split the encrypted archive into smaller packages')
    path=output/'manifest.json';path.write_bytes(raw);path.chmod(0o600)
    return hashlib.sha256(raw).hexdigest()


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    test=sub.add_parser('prepare-test');test.add_argument('--store',type=Path,default=DEFAULT_STORE)
    test.add_argument('--snapshot');test.add_argument('--org',required=True);test.add_argument('--include-private',action='store_true');test.add_argument('--output',type=Path,required=True)
    dr=sub.add_parser('prepare-encrypted');dr.add_argument('--file',type=Path,required=True);dr.add_argument('--output',type=Path,required=True)
    public=sub.add_parser('prepare-public');public.add_argument('--source',default='https://orgportal.cc/api/org/api/network/replication/snapshot');public.add_argument('--output',type=Path,required=True)
    lan=sub.add_parser('serve-public');lan.add_argument('--package',type=Path,required=True);lan.add_argument('--bind',default='127.0.0.1');lan.add_argument('--port',type=int,default=8792)
    invite=sub.add_parser('invite');invite.add_argument('--package',type=Path,required=True);invite.add_argument('--output',type=Path,required=True)
    invite.add_argument('--key-file',type=Path,default=DEFAULT_KEY);invite.add_argument('--url',default=DEFAULT_URL)
    invite.add_argument('--devices',type=int,default=1);invite.add_argument('--ttl',type=int,default=3600)
    launcher=sub.add_parser('launcher-invite');launcher.add_argument('--output',type=Path,required=True)
    launcher.add_argument('--key-file',type=Path,default=DEFAULT_KEY);launcher.add_argument('--url',default=DEFAULT_URL)
    launcher.add_argument('--devices',type=int,default=1);launcher.add_argument('--ttl',type=int,default=3600)
    launcher.add_argument('--fleet-file',type=Path,default=Path.home()/'.config/lifetech-launcher/fleet.json')
    revoke=sub.add_parser('launcher-revoke');revoke.add_argument('--peer',required=True);revoke.add_argument('--fleet-file',type=Path,default=Path.home()/'.config/lifetech-launcher/fleet.json');revoke.add_argument('--key-file',type=Path,default=DEFAULT_KEY);revoke.add_argument('--url',default=DEFAULT_URL)
    args=parser.parse_args()
    if args.action=='prepare-public':
        from public_data import prepare
        print(json.dumps(prepare(args.source,args.output)))
    elif args.action=='serve-public':
        from public_data import serve
        serve(args.package,args.bind,args.port)
    elif args.action=='prepare-test':
        import tempfile
        snapshot=resolve_snapshot(args.store,args.snapshot);manifest=json.loads((snapshot/'manifest.json').read_text())
        if not manifest.get('completedAt'):raise RuntimeError('Use a completed DR snapshot')
        payload=archive_payload(snapshot,manifest,args.org,args.include_private)
        with tempfile.TemporaryDirectory(prefix='peer-test-package-') as folder:
            source=Path(folder)/'testing-copy.json';private_json(source,payload)
            expires=int(dt.datetime.fromisoformat(payload['expiresAt']).timestamp())
            room=package_file(source,args.output,'testing',expires,digest(snapshot/'manifest.json'))
        print(json.dumps({'package':str(args.output),'dataset':room,'private':args.include_private}))
    elif args.action=='prepare-encrypted':
        room=package_file(args.file,args.output,'encrypted-dr')
        print(json.dumps({'package':str(args.output),'dataset':room,'decryptionKeyIncluded':False}))
    elif args.action=='invite':
        if not 1<=args.devices<=24:raise RuntimeError('Choose 1–24 devices per dataset room')
        parsed=urllib.parse.urlsplit(args.url)
        if parsed.scheme!='https' and not (parsed.scheme=='http' and parsed.hostname in {'localhost','127.0.0.1'}):raise RuntimeError('Use HTTPS or host-local testing')
        manifest=args.package/'manifest.json';room=digest(manifest);invitations=[]
        secret=args.key_file.read_text().strip()
        for _ in range(args.devices):
            body,token=ticket(secret,room,args.ttl)
            invitations.append({'peer':body['peer'],'expires':body['exp'],'url':args.url.rstrip('/')+'/#'+urllib.parse.urlencode({'token':token})})
        private_json(args.output,{'dataset':room,'invitations':invitations})
        print(json.dumps({'invitationsFile':str(args.output),'devices':args.devices,'ttlSeconds':args.ttl}))

    elif args.action=='launcher-invite':
        if not 1<=args.devices<=24:raise RuntimeError('Choose 1–24 devices')
        from launcher_registration import service_url
        base=service_url(args.url)
        if args.fleet_file.exists():room=json.loads(args.fleet_file.read_text())['room']
        else:
            room=secrets.token_hex(32);private_json(args.fleet_file,{'room':room})
        secret=args.key_file.read_text().strip();invitations=[]
        for _ in range(args.devices):
            body,token=ticket(secret,room,args.ttl,'launcher-enroll-v1')
            invitations.append({'url':base,'token':token,'peer':body['peer'],'expires':body['exp'],'registrationUrl':base+'/register#'+urllib.parse.urlencode({'token':token})})
        private_json(args.output,{'invitations':invitations})
        print(json.dumps({'invitationsFile':str(args.output),'devices':args.devices,'room':room}))
    else:
        from launcher_registration import post,service_url
        room=json.loads(args.fleet_file.read_text())['room']
        _,token=ticket(args.key_file.read_text().strip(),room,60,'launcher-admin-v1')
        result=post(service_url(args.url),'/launchers/revoke',token,{'peer':args.peer})
        print(json.dumps(result))


if __name__=='__main__':
    try:main()
    except Exception as e:
        print('Peer package operation failed: '+(str(e) if isinstance(e,RuntimeError) else type(e).__name__))
        raise SystemExit(1)
