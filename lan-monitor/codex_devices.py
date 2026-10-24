"""Owner-issued, one-use phone pairing and revocable device sessions."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import time

FILE=Path(os.getenv('MAP_CODEX_DEVICE_FILE',str(Path.home()/'.config/map-master/codex-devices.json')))
LIFETIME=30*86400

def digest(token):return hashlib.sha256(token.encode()).hexdigest()

@contextmanager
def state():
    FILE.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(FILE.with_suffix('.lock'),os.O_CREAT|os.O_RDWR,0o600)
    with os.fdopen(fd,'r+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        value=json.loads(FILE.read_text()) if FILE.exists() else {'pairs':{},'sessions':{}}
        now=time.time()
        for group in value:value[group]={k:v for k,v in value[group].items() if v>now}
        yield value

def save(value):
    p=FILE.with_suffix('.tmp')
    fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as stream:json.dump(value,stream)
    p.chmod(0o600);p.replace(FILE)

def issue():
    token=secrets.token_urlsafe(32)
    with state() as value:
        # Only the newest setup link remains valid.
        value['pairs']={digest(token):time.time()+600};save(value)
    return token

def new_session(value):
    if len(value['sessions'])>=100:raise ValueError('Device limit reached; revoke an old session first')
    token=secrets.token_urlsafe(32)
    value['sessions'][digest(token)]=time.time()+LIFETIME;save(value)
    return token

def session(pair):
    if not isinstance(pair,str) or not 20<=len(pair)<=128:raise ValueError('Invalid pairing token')
    with state() as value:
        if digest(pair) not in value['pairs']:raise ValueError('Pairing link expired or already used. Request a fresh link from CompAI.')
        del value['pairs'][digest(pair)]
        return new_session(value)

def owner_session():
    # Call only after owner authentication; never use for public pairing input.
    with state() as value:return new_session(value)

def valid(token):
    if not isinstance(token,str) or not 20<=len(token)<=128:return False
    with state() as value:return digest(token) in value['sessions']

def revoke(token=None):
    with state() as value:
        if token is None:value={'pairs':{},'sessions':{}}
        else:value['sessions'].pop(digest(token),None)
        save(value)
