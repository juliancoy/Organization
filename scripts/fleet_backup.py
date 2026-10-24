#!/usr/bin/env python3
"""Encrypted, independently restorable fleet copies of a verified DR generation."""
import base64
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import tarfile

PART_SIZE = 32 * 1024 * 1024
DEFAULT_CACHE = Path('/mnt/PILEDRIVE/organization-fleet-backups')
KEY_FILE = Path.home()/'.config/organization-recovery/private.pem'


def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def write_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temporary=path.with_suffix('.pending');temporary.write_text(json.dumps(value,indent=2)+'\n');temporary.chmod(0o600)
    with temporary.open('rb') as stream:os.fsync(stream.fileno())
    temporary.replace(path)


def contents(snapshot,store):
    manifest=json.loads((snapshot/'manifest.json').read_text())
    if manifest.get('status')!='complete' or not manifest.get('completedAt') or manifest.get('gaps'):
        raise RuntimeError('Fleet replication requires a complete verified DR snapshot')
    expected={snapshot.name+'/manifest.json':sha(snapshot/'manifest.json')}
    for name,digest in manifest['files'].items():
        path=(snapshot/name).resolve()
        if not path.is_relative_to(snapshot.resolve()) or not path.is_file():raise RuntimeError('Invalid snapshot file')
        expected[snapshot.name+'/'+name]=digest
    for records in manifest['objects'].values():
        for record in records:expected['objects/'+record['sha256']]=record['sha256']
    return expected


def current_snapshot(snapshot,store,cache):
    """Keep every private record; remove only superseded public dataset versions."""
    contents(snapshot,store)
    source_digest=sha(snapshot/'manifest.json')
    destination=Path(cache)/'current-snapshots'/snapshot.name
    if (destination/'manifest.json').exists():
        saved=json.loads((destination/'manifest.json').read_text())
        if saved['fleetPolicy']['sourceManifestSha256']!=source_digest:raise RuntimeError('Source recovery generation changed')
        return destination
    manifest=json.loads((snapshot/'manifest.json').read_text());excluded=[]
    bucket='codecollective-usajobs';records=manifest['objects'].get(bucket,[]);versions={}
    for name in ('jobs','vacants','vacants_parcels'):
        pointer=next((r for r in records if r['key']==name+'/latest.json'),None)
        if pointer:
            value=json.loads((store/'objects'/pointer['sha256']).read_text())
            version=value['version']
            if not re.fullmatch(r'v\d{8}T\d{6}Z',version) or not any(r['key']==name+'/'+version+'/manifest.json' for r in records):raise RuntimeError('Current public dataset manifest missing')
            versions[name]=version
    if records:
        retained=[]
        for record in records:
            path=record['key'].split('/')
            historical=len(path)>1 and path[0] in versions and re.fullmatch(r'v\d{8}T\d{6}Z',path[1]) and path[1]!=versions[path[0]]
            (excluded if historical else retained).append(record)
        manifest['objects'][bucket]=retained
    destination.mkdir(parents=True,exist_ok=True,mode=0o700)
    for name in manifest['files']:
        source=snapshot/name;target=destination/name;target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if target.exists():continue
        try:os.link(source,target)
        except OSError:shutil.copyfile(source,target);target.chmod(0o600)
    manifest['fleetPolicy']={'profile':'current-production-data','sourceSnapshot':snapshot.name,'sourceManifestSha256':source_digest,
        'fullHistoryPath':str(snapshot),'currentPublicVersions':versions,'excludedHistoricalPublicObjects':len(excluded),'excludedHistoricalPublicBytes':sum(r['size'] for r in excluded)}
    manifest['coverage']={**manifest.get('coverage',{}),'historicalPublicObjectsIncluded':False,'currentPublicDatasetVersionsCaptured':True}
    manifest['consistency']=manifest.get('consistency','')+'; fleet replicas retain current public dataset versions; historical public versions remain on the recovery host'
    write_json(destination/'manifest.json',manifest)
    return destination


class EncryptedWriter:
    def __init__(self,folder,key,snapshot,part_size):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self.folder,self.key,self.snapshot,self.limit=folder,AESGCM(key),snapshot,part_size
        self.buffer=bytearray();self.parts=[]
    def write(self,data):
        size=len(data);self.buffer.extend(data)
        while len(self.buffer)>=self.limit:
            self.part(bytes(self.buffer[:self.limit]));del self.buffer[:self.limit]
        return size
    def flush(self):pass
    def part(self,data):
        index=len(self.parts);nonce=os.urandom(12);aad=(self.snapshot+':'+str(index)).encode()
        raw=nonce+self.key.encrypt(nonce,data,aad);digest=hashlib.sha256(raw).hexdigest()
        path=self.folder/(digest+'.enc');path.write_bytes(raw);path.chmod(0o600)
        self.parts.append({'sha256':digest,'size':len(raw)})
    def finish(self):
        if self.buffer:self.part(bytes(self.buffer));self.buffer.clear()


class DecryptedReader:
    def __init__(self,folder,manifest,key):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self.folder,self.manifest,self.key=folder,manifest,AESGCM(key)
        self.index=0;self.buffer=io.BytesIO()
    def read(self,size):
        blocks=[];remaining=size
        while remaining>0:
            value=self.buffer.read(remaining)
            if value:blocks.append(value);remaining-=len(value)
            if remaining==0 or self.index>=len(self.manifest['parts']):break
            item=self.manifest['parts'][self.index];raw=(self.folder/(item['sha256']+'.enc')).read_bytes()
            if len(raw)!=item['size'] or hashlib.sha256(raw).hexdigest()!=item['sha256']:raise RuntimeError('Encrypted backup part corrupt')
            aad=(self.manifest['snapshot']+':'+str(self.index)).encode()
            self.buffer=io.BytesIO(self.key.decrypt(raw[:12],raw[12:],aad));self.index+=1
        return b''.join(blocks)


def verify(folder,private_key,expected=None,target=None):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    manifest=json.loads((folder/'manifest.json').read_text())
    key=private_key.decrypt(base64.b64decode(manifest['wrappedKey']),padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=None))
    seen={};reader=DecryptedReader(folder,manifest,key)
    if target:
        target=Path(target).resolve()
        if target.exists() and any(target.iterdir()):raise RuntimeError('Restore target must be empty')
        target.mkdir(parents=True,exist_ok=True,mode=0o700)
    with tarfile.open(fileobj=reader,mode='r|gz') as archive:
        for member in archive:
            path=Path(member.name)
            if not member.isfile() or path.is_absolute() or '..' in path.parts or member.name in seen:raise RuntimeError('Unsafe backup member')
            digest=hashlib.sha256();destination=None
            if target:
                destination=target/path;destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                destination=destination.open('xb');os.chmod(destination.name,0o600)
            try:
                with archive.extractfile(member) as stream:
                    for block in iter(lambda:stream.read(1024*1024),b''):
                        digest.update(block)
                        if destination:destination.write(block)
            finally:
                if destination:destination.close()
            seen[member.name]=digest.hexdigest()
    # Authenticate the remaining encrypted frames as well as all archive members.
    while reader.read(1024*1024):pass
    if expected is None:
        # The archived DR manifest is the authoritative plaintext file inventory.
        # Verification without an external inventory is supported after restoration.
        if not target:raise ValueError('Supply the expected inventory or an empty restore target')
        snapshot=target/manifest['snapshot'];expected=contents(snapshot,target)
    if seen!=expected:raise RuntimeError('Restored backup file inventory or hash mismatch')
    return {'verified':True,'files':len(seen),'parts':len(manifest['parts']),'bytes':sum(x['size'] for x in manifest['parts'])}


def package(snapshot,store,cache=DEFAULT_CACHE,key_file=KEY_FILE,part_size=PART_SIZE,progress=lambda message:None):
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    snapshot=current_snapshot(snapshot,store,cache)
    cache=Path(cache)/'current'
    expected=contents(snapshot,store)
    private_key=serialization.load_pem_private_key(Path(key_file).read_bytes(),password=None)
    folder=Path(cache)/snapshot.name;folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    if (folder/'manifest.json').exists():
        info=json.loads((folder/'manifest.json').read_text())
        if info['sourceManifestSha256']!=expected[snapshot.name+'/manifest.json']:raise RuntimeError('Fleet snapshot identity changed')
        if (folder/'verified.json').exists() and all((folder/(p['sha256']+'.enc')).is_file() and sha(folder/(p['sha256']+'.enc'))==p['sha256'] for p in info['parts']):return folder
        progress('Verifying existing encrypted generation before resuming replication')
        write_json(folder/'verified.json',verify(folder,private_key,expected))
        return folder
    key=os.urandom(32);writer=EncryptedWriter(folder,key,snapshot.name,part_size)
    with gzip.GzipFile(fileobj=writer,mode='wb',compresslevel=1,mtime=0) as compressed:
        with tarfile.open(fileobj=compressed,mode='w|') as archive:
            for index,name in enumerate(sorted(expected)):
                source=store/name if name.startswith('objects/') else snapshot/name.split('/',1)[1]
                archive.add(source,arcname=name,recursive=False)
                if index%5000==0:progress('Packing '+str(index)+' / '+str(len(expected))+' files')
    writer.finish()
    public=private_key.public_key()
    wrapped=public.encrypt(key,padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=None))
    fingerprint=hashlib.sha256(public.public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()
    write_json(folder/'manifest.json',{'version':1,'snapshot':snapshot.name,'profile':'current-production-data','sourceManifestSha256':expected[snapshot.name+'/manifest.json'],'encryption':'AES-256-GCM/RSA-OAEP-SHA256','recipientSha256':fingerprint,'wrappedKey':base64.b64encode(wrapped).decode(),'parts':writer.parts})
    progress('Verifying encrypted archive by decrypting and hashing every member')
    write_json(folder/'verified.json',verify(folder,private_key,expected))
    return folder


if __name__=='__main__':
    import argparse
    from cryptography.hazmat.primitives import serialization
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('folder',type=Path);parser.add_argument('--restore',type=Path,required=True);parser.add_argument('--key',type=Path,default=KEY_FILE)
    args=parser.parse_args();key=serialization.load_pem_private_key(args.key.read_bytes(),password=None)
    print(json.dumps(verify(args.folder,key,target=args.restore)))
