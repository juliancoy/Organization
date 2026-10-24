#!/usr/bin/env python3
"""Escrow active Worker secrets through encrypted, expiring, zero-traffic versions."""
import argparse
import base64
import datetime as dt
import email.parser
import email.policy
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import time
import urllib.error
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from recovery import cf_token, environment, inventory, now, private_json

HOME = Path.home()/'.config/organization-recovery'


def api(account, path, body=None, content_type='application/json',method=None):
    request = urllib.request.Request('https://api.cloudflare.com/client/v4/accounts/'+account+path,
        data=body, headers={'Authorization':'Bearer '+cf_token(),'Content-Type':content_type},method=method or ('POST' if body is not None else 'GET'))
    try: response=urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=90)
    except urllib.error.HTTPError as error:
        value=json.loads(error.read(1024*1024))
        private_json(HOME/'last-api-error.json',value)
        raise RuntimeError('Cloudflare API rejected operation: '+', '.join(str(item.get('code'))+': '+item.get('message','')[:500] for item in value.get('errors',[]))) from None
    with response:
        raw=response.read(32*1024*1024)
        if not response.headers.get('Content-Type','').startswith('application/json'):
            return raw,response.headers
        result=json.loads(raw)
    if result.get('success') is not True: raise RuntimeError('Cloudflare operation failed; private response withheld')
    return result['result']


def deployed(account,worker):
    record=api(account,'/workers/scripts/'+worker+'/deployments')['deployments'][0]
    versions=record['versions']
    if len(versions)!=1 or versions[0]['percentage']!=100:
        raise RuntimeError(worker+': finish the active canary before recovering secrets')
    return versions[0]['version_id']


def multipart(metadata,files):
    boundary='escrow-'+secrets.token_hex(24); parts=[]
    for name,kind,data in [('metadata','application/json',json.dumps(metadata).encode()),*files]:
        if any(c in name for c in '\r\n"'): raise RuntimeError('Invalid Worker module name')
        parts.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="'+name+'"; filename="'+name+'"\r\nContent-Type: '+kind+'\r\n\r\n').encode()+data+b'\r\n')
    return b''.join(parts)+('--'+boundary+'--\r\n').encode(),'multipart/form-data; boundary='+boundary


def upload(account,worker,metadata,files):
    body,kind=multipart(metadata,files)
    return api(account,'/workers/scripts/'+worker+'/versions?bindings_inherit=strict',body,kind)


def wrapper(entry,worker,names,public,token_hash,deadline,nonce):
    configuration=json.dumps(dict(worker=worker,names=names,publicKey=public,tokenHash=token_hash,deadline=deadline,nonce=nonce))
    return f'''import original from {json.dumps('./'+entry)};
export * from {json.dumps('./'+entry)};
const config={configuration};
const b64=bytes=>{{const value=new Uint8Array(bytes);let text='';for(let i=0;i<value.length;i+=32768)text+=String.fromCharCode(...value.subarray(i,i+32768));return btoa(text);}};
export default {{...original,async fetch(request,env,ctx){{
 if(new URL(request.url).pathname!=='/__operator_encrypted_escrow')return original.fetch(request,env,ctx);
 const headers={{'Cache-Control':'no-store','Content-Type':'application/json'}};
 if(request.method!=='POST'||Date.now()>config.deadline)return new Response(null,{{status:404,headers}});
 const actual=new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(request.headers.get('Authorization')||'')));
 let mismatch=0;for(let i=0;i<32;i++)mismatch|=actual[i]^config.tokenHash[i];
 if(mismatch)return new Response(null,{{status:404,headers}});
 const values={{}};for(const name of config.names){{if(typeof env[name]!=='string')return new Response(null,{{status:503,headers}});values[name]=env[name];}}
 const identity={{worker:config.worker,version:env.ESCROW_VERSION_METADATA.id,nonce:config.nonce}};
 const aad=new TextEncoder().encode(JSON.stringify(identity));
 const key=await crypto.subtle.generateKey({{name:'AES-GCM',length:256}},true,['encrypt']);
 const iv=crypto.getRandomValues(new Uint8Array(12));
 const payload=await crypto.subtle.encrypt({{name:'AES-GCM',iv,additionalData:aad}},key,new TextEncoder().encode(JSON.stringify(values)));
 const recipient=await crypto.subtle.importKey('jwk',config.publicKey,{{name:'RSA-OAEP',hash:'SHA-256'}},false,['wrapKey']);
 const wrapped=await crypto.subtle.wrapKey('raw',key,recipient,{{name:'RSA-OAEP'}});
 return new Response(JSON.stringify({{identity,iv:b64(iv),wrappedKey:b64(wrapped),ciphertext:b64(payload)}}),{{headers}});
}}}};'''.encode()


def recover(account,worker,key,existing,proof,if_needed):
    base='/workers/scripts/'+worker
    current=deployed(account,worker)
    settings=api(account,base+'/settings')
    names=sorted(b['name'] for b in settings.get('bindings',[]) if b['type']=='secret_text')
    if not names:return
    old=proof.get(worker,{})
    if if_needed and old.get('sourceVersion')==current and old.get('fingerprints')=={name:hashlib.sha256(existing.get(worker,{}).get(name,'').encode()).hexdigest() for name in names}:
        print(worker+': active secrets already escrowed',flush=True);return
    if worker=='organization-archive-peers' and names==['PEER_SIGNING_KEY']:
        from peer_archive import ticket
        candidates=[]
        signing=Path.home()/'.config/organization-peers/signing.key'
        if signing.exists(): candidates.append(signing.read_text().strip())
        saved=Path.home()/'.config/organization-peers/worker-secrets.json'
        if saved.exists(): candidates.append(json.loads(saved.read_text()).get('PEER_SIGNING_KEY',''))
        domain=api(account,'/workers/subdomain')['subdomain']
        url='https://'+worker+'.'+domain+'.workers.dev'
        def signature_status(secret):
            _,token=ticket(secret,'e'*64,60)
            req=urllib.request.Request(url+'/ice',data=b'',headers={'Authorization':'Bearer '+token,'User-Agent':'OrgPortal-CLI/0.1'})
            try:
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=20) as response:return response.status
            except urllib.error.HTTPError as error:return error.code
        if signature_status(secrets.token_hex(32))!=401:raise RuntimeError('Peer signature validation control failed')
        values=next(({'PEER_SIGNING_KEY':value} for value in dict.fromkeys(candidates) if value and signature_status(value)==405),None)
        if values is None:raise RuntimeError('No local peer key matches production')
        with urllib.request.urlopen(urllib.request.Request(url+'/health',headers={'User-Agent':'OrgPortal-CLI/0.1'}),timeout=20) as response:health=json.load(response)
        if health.get('version')!=current or deployed(account,worker)!=current:raise RuntimeError('Peer deployment changed during validation')
        raw=secrets.token_bytes(32);iv=secrets.token_bytes(12);identity={'worker':worker,'version':current,'nonce':secrets.token_hex(32)}
        ciphertext=AESGCM(raw).encrypt(iv,json.dumps(values).encode(),json.dumps(identity,separators=(',',':')).encode())
        envelope={'identity':identity,'iv':base64.b64encode(iv).decode(),'wrappedKey':base64.b64encode(key.public_key().encrypt(raw,padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=None))).decode(),'ciphertext':base64.b64encode(ciphertext).decode()}
        existing[worker]=values;proof[worker]={'sourceVersion':current,'recoveredAt':now(),'validation':'read-only signed invitation acceptance',
                                            'fingerprints':{name:hashlib.sha256(value.encode()).hexdigest() for name,value in values.items()}}
        private_json(HOME/'production.json',existing);private_json(HOME/'proof.json',proof);private_json(HOME/(worker+'.encrypted.json'),envelope)
        print(worker+': validated and encrypted its existing production signing key',flush=True);return
    latest=api(account,base+'/versions')['items'][0]['id']
    cleanup_file=HOME/(worker+'.cleanup.json')
    cleanup=json.loads(cleanup_file.read_text()) if cleanup_file.exists() else {}
    known_original=cleanup.get('originalSourceVersion')==latest and cleanup.get('sourceDeployment')==current and cleanup.get('productionDeploymentUnchanged') is True
    if latest!=current and not known_original:
        raise RuntimeError(worker+': an unpromoted version exists; refusing ambiguous secret inheritance')
    if api(account,base+'/subdomain').get('previews_enabled') is not True:
        raise RuntimeError(worker+': isolated version URLs are disabled')
    raw,headers=api(account,base)
    entry=headers.get('cf-entrypoint')
    if not entry: raise RuntimeError(worker+': main module not identified')
    message=email.parser.BytesParser(policy=email.policy.default).parsebytes(('Content-Type: '+headers['Content-Type']+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+raw)
    files=[]
    for part in message.iter_parts():
        name=part.get_param('name',header='content-disposition') or part.get_filename()
        kind='application/javascript+module' if name.endswith(('.js','.mjs')) else part.get_content_type()
        files.append((name,kind,part.get_payload(decode=True)))
    if entry not in [name for name,_,_ in files]:raise RuntimeError('Worker source does not contain its entry point')
    bindings=[{'type':'inherit','name':b['name']} for b in settings.get('bindings',[])]
    metadata={'main_module':entry,'bindings':bindings,'compatibility_date':settings['compatibility_date'],
              'compatibility_flags':settings.get('compatibility_flags',[]),'observability':settings.get('observability',{'enabled':False}),
              'annotations':{'workers/message':'Restore original source after encrypted operator escrow'}}
    if any(binding['type']=='assets' for binding in settings.get('bindings',[])):
        metadata['keep_assets']=True
    public=key.public_key().public_numbers()
    encode_int=lambda value:base64.urlsafe_b64encode(value.to_bytes((value.bit_length()+7)//8,'big')).decode().rstrip('=')
    jwk={'kty':'RSA','n':encode_int(public.n),'e':encode_int(public.e),'alg':'RSA-OAEP-256','ext':True}
    token='Bearer '+secrets.token_urlsafe(48); nonce=secrets.token_hex(32)
    deadline=int((time.time()+600)*1000)
    code=wrapper(entry,worker,names,jwk,list(hashlib.sha256(token.encode()).digest()),deadline,nonce)
    candidate_meta={**metadata,'main_module':'operator-escrow.mjs','observability':{'enabled':False},
                    'bindings':[*bindings,{'type':'version_metadata','name':'ESCROW_VERSION_METADATA'}],
                    'annotations':{'workers/message':'Expiring encrypted operator escrow; zero production traffic'}}
    candidate=None
    try:
        if deployed(account,worker)!=current:raise RuntimeError('Production deployment changed during preparation')
        candidate=upload(account,worker,candidate_meta,[*files,('operator-escrow.mjs','application/javascript+module',code)])
        version=candidate['id']
        worker_record=api(account,'/workers/workers/'+worker)
        suffix=worker_record['subdomain']['preview_url_suffix']
        url='https://'+version[:8]+suffix+'/__operator_encrypted_escrow'
        envelope=None
        for attempt in range(12):
            try:
                request=urllib.request.Request(url,data=b'',headers={'Authorization':token,'User-Agent':'OrgPortal-CLI/0.1'})
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=20) as response:envelope=json.loads(response.read(4*1024*1024))
                break
            except urllib.error.HTTPError as error:
                if error.code not in (404,503) or attempt==11:raise RuntimeError(worker+': version endpoint unavailable') from None
                time.sleep(2)
        expected={'worker':worker,'version':version,'nonce':nonce}
        if envelope is None or envelope.get('identity')!=expected:raise RuntimeError('Escrow response version/challenge mismatch')
        unwrapped=key.decrypt(base64.b64decode(envelope['wrappedKey']),padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=None))
        aad=json.dumps(expected,separators=(',',':')).encode()
        values=json.loads(AESGCM(unwrapped).decrypt(base64.b64decode(envelope['iv']),base64.b64decode(envelope['ciphertext']),aad))
        if sorted(values)!=names or any(not isinstance(value,str) for value in values.values()):raise RuntimeError('Escrow does not cover every deployed secret')
        if deployed(account,worker)!=current:raise RuntimeError('Production deployment changed during recovery')
        existing[worker]=values
        proof[worker]={'sourceVersion':current,'recoveryVersion':version,'recoveredAt':now(),
                       'fingerprints':{name:hashlib.sha256(value.encode()).hexdigest() for name,value in values.items()}}
        private_json(HOME/'production.json',existing);private_json(HOME/'proof.json',proof)
        private_json(HOME/(worker+'.encrypted.json'),envelope)
        print(worker+': recovered and encrypted '+str(len(values))+' active secrets; production traffic unchanged',flush=True)
    finally:
        if candidate is not None:
            # Leave the latest uploaded version with original source/bindings, never the recovery handler.
            restored=upload(account,worker,metadata,files)
            private_json(HOME/(worker+'.cleanup.json'),{'candidateVersion':candidate['id'],'originalSourceVersion':restored['id'],
                         'sourceDeployment':current,'productionDeploymentUnchanged':deployed(account,worker)==current,
                         'recoveryExpiresAt':dt.datetime.fromtimestamp(deadline/1000,dt.timezone.utc).isoformat()})
            try:
                record=api(account,'/workers/workers/'+worker)
                api(account,'/workers/workers/'+record['id']+'/versions/'+candidate['id'],method='DELETE')
            except (RuntimeError,KeyError):
                # Expiry is enforced in the immutable version even if deletion is unavailable.
                print(worker+': recovery version is time-limited; version deletion unavailable',flush=True)


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--worker',action='append');parser.add_argument('--if-needed',action='store_true')
    args=parser.parse_args();HOME.mkdir(parents=True,exist_ok=True,mode=0o700);HOME.chmod(0o700)
    with (HOME/'.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        recover_all(args)


def recover_all(args):
    path=HOME/'private.pem'
    if path.exists():key=serialization.load_pem_private_key(path.read_bytes(),password=None)
    else:
        key=rsa.generate_private_key(public_exponent=65537,key_size=4096)
        with open(path,'wb',opener=lambda p,f:os.open(p,f,0o600)) as output:output.write(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    existing=json.loads((HOME/'production.json').read_text()) if (HOME/'production.json').exists() else {}
    proof=json.loads((HOME/'proof.json').read_text()) if (HOME/'proof.json').exists() else {}
    env=environment();account=env.get('CLOUDFLARE_ACCOUNT_ID') or env['R2_ACCOUNT_ID']
    workers=args.worker or list(inventory()['workers'])
    for worker in workers:
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,63}',worker):raise RuntimeError('Invalid Worker name')
        recover(account,worker,key,existing,proof,args.if_needed)


if __name__=='__main__':main()
