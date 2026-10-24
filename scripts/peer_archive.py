#!/usr/bin/env python3
"""Prepare scoped test fixtures or encrypted DR files and issue device invitations."""
import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
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


def ticket(secret,room,ttl=3600):
    if len(secret)<32 or not 1<=ttl<=3600:raise RuntimeError('A signing key and a lifetime of 1–3600 seconds are required')
    now=int(time.time());peer=str(uuid.uuid4())
    body={'aud':'archive-peers-v1','room':room,'peer':peer,'iat':now,'exp':now+ttl}
    value=encoded({'alg':'HS256','typ':'JWT'})+'.'+encoded(body)
    signature=base64.urlsafe_b64encode(hmac.new(secret.encode(),value.encode(),hashlib.sha256).digest()).rstrip(b'=').decode()
    return body,value+'.'+signature


def package_file(source,output,kind,expires=None,source_manifest=None):
    if output.exists():raise RuntimeError('Package output must be a new directory')
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
    manifest={'version':1,'kind':kind,'files':[{'path':'testing-copy.json' if kind=='testing' else 'recovery.tar.age','size':size,'chunks':parts}]}
    if kind=='testing':manifest['expires']=expires or int(time.time())+7*86400
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
    invite=sub.add_parser('invite');invite.add_argument('--package',type=Path,required=True);invite.add_argument('--output',type=Path,required=True)
    invite.add_argument('--key-file',type=Path,default=DEFAULT_KEY);invite.add_argument('--url',default=DEFAULT_URL)
    invite.add_argument('--devices',type=int,default=1);invite.add_argument('--ttl',type=int,default=3600)
    args=parser.parse_args()
    if args.action=='prepare-test':
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
    else:
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


if __name__=='__main__':
    try:main()
    except Exception as e:
        print('Peer package operation failed: '+(str(e) if isinstance(e,RuntimeError) else type(e).__name__))
        raise SystemExit(1)
