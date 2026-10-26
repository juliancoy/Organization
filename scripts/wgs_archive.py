#!/usr/bin/env python3
"""Maintain verified public sources and authorized private backups on Inspiron."""
import argparse
import json
from pathlib import Path
import subprocess
import shlex
import sys
import ipaddress
from urllib.parse import urlsplit
import time
from scripts import genomics_distributed as distributed
from scripts.wgs_swarm_download import AgentClient, atomic_json, stamp, validate_manifest


INSPECT = r'''import hashlib,json,os,pathlib,shutil,sys
root=pathlib.Path(sys.argv[1]);item=json.loads(sys.argv[2]);machine=sys.argv[3]
if hashlib.sha256(pathlib.Path('/etc/machine-id').read_bytes()).hexdigest()!=machine:raise RuntimeError('Archive machine changed')
if any(p.is_symlink() for p in [root,*root.parents]):raise RuntimeError('Symlink archive root')
root.mkdir(mode=0o700,parents=True,exist_ok=True);root.chmod(0o700)
path=root/item['sample']/item['filename'];path.parent.mkdir(mode=0o700,exist_ok=True)
partial=path.with_name(path.name+'.partial');meta=path.with_name(path.name+'.partial.json')
if path.exists():
 h=hashlib.sha256();m=hashlib.md5()
 with path.open('rb') as s:
  for b in iter(lambda:s.read(8388608),b''):h.update(b);m.update(b)
 if path.stat().st_size==item['bytes'] and h.hexdigest()==item['sha256'] and m.hexdigest()==item['md5']:
  print(json.dumps({'verified':True,'path':str(path),'bytes':item['bytes'],'sha256':h.hexdigest(),'md5':m.hexdigest()}));sys.exit(0)
 raise RuntimeError('Existing archive file failed verification; preserve for investigation')
same=False
if meta.exists():same=json.loads(meta.read_text()).get('sha256')==item['sha256']
if partial.exists() and (not same or partial.stat().st_size>item['bytes']):raise RuntimeError('Partial archive identity mismatch')
meta.write_text(json.dumps(item));meta.chmod(0o600)
offset=partial.stat().st_size if partial.exists() else 0
free=shutil.disk_usage(root).free
if free<item['bytes']-offset+10*1024**3:raise RuntimeError('Archive disk reserve would be exhausted')
print(json.dumps({'verified':False,'offset':offset,'path':str(path),'free_bytes':free}))
'''

READ = r'''import pathlib,sys
p=pathlib.Path(sys.argv[1]);offset=int(sys.argv[2]);size=int(sys.argv[3])
if p.stat().st_size!=size:raise RuntimeError('Source size changed')
with p.open('rb') as s:
 s.seek(offset)
 while b:=s.read(8388608):sys.stdout.buffer.write(b)
'''

WRITE = r'''import fcntl,hashlib,json,os,pathlib,sys,time
path=pathlib.Path(sys.argv[1]);item=json.loads(sys.argv[2]);offset=int(sys.argv[3]);partial=path.with_name(path.name+'.partial')
lock=path.with_name(path.name+'.archive.lock').open('a');os.fchmod(lock.fileno(),0o600);fcntl.flock(lock,fcntl.LOCK_EX)
if (partial.stat().st_size if partial.exists() else 0)!=offset:raise RuntimeError('Archive offset changed')
n=offset
with partial.open('ab') as s:
 partial.chmod(0o600)
 while b:=sys.stdin.buffer.read(8388608):
  n+=len(b)
  if n>item['bytes']:raise RuntimeError('Archive transfer exceeded size')
  s.write(b)
 s.flush();os.fsync(s.fileno())
if n!=item['bytes']:raise RuntimeError('Archive transfer interrupted; partial retained')
h=hashlib.sha256();m=hashlib.md5()
with partial.open('rb') as s:
 for b in iter(lambda:s.read(8388608),b''):h.update(b);m.update(b)
if h.hexdigest()!=item['sha256'] or m.hexdigest()!=item['md5']:
 partial.rename(partial.with_name(partial.name+'.checksum-failed-'+str(time.time_ns())))
 raise RuntimeError('Archive checksum mismatch; quarantined')
partial.replace(path);receipt={**item,'path':str(path),'archive_verified_at':time.time()}
record=path.with_name(path.name+'.receipt.json');tmp=record.with_name(record.name+'.tmp')
tmp.write_text(json.dumps(receipt));tmp.chmod(0o600);tmp.replace(record)
print(json.dumps({'verified':True,'path':str(path),'bytes':n,'sha256':h.hexdigest(),'md5':m.hexdigest()}))
'''


def remote(host, code, *args):
    if host.get('archive_mptcp'):
        host={**host,'ssh':host['ssh'][:-1]+['-o','ProxyCommand='+shlex.join([sys.executable,str(Path(__file__).with_name('ssh_multipath.py')),'connect','--socket',str(Path.home()/'.config/swarm-control/archive-mptcp/socket')])]+host['ssh'][-1:]}
    return distributed.shell_command(host, ['nice', '-n', '15', 'ionice', '-c', '3', 'python3', '-c', code, *map(str, args)])


def authenticate(host):
    client=AgentClient(host)
    try:
        if client.call('status').get('machine')!=host['machine']:
            raise RuntimeError('Enrolled machine identity changed')
    finally:
        client.close()


def copy(source, destination, root, item):
    authenticate(destination)
    observed=json.loads(subprocess.check_output(remote(destination, INSPECT, root, json.dumps(item), destination['machine']), text=True, timeout=1800))
    if observed['verified']:
        return observed
    authenticate(source)
    producer=subprocess.Popen(remote(source, READ, item['path'], observed['offset'], item['bytes']), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    consumer=None
    try:
        consumer=subprocess.Popen(remote(destination, WRITE, observed['path'], json.dumps(item), observed['offset']), stdin=producer.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        producer.stdout.close()
        output,errors=consumer.communicate(timeout=24*3600)
        producer.wait(timeout=30)
        if consumer.returncode or producer.returncode:
            raise RuntimeError('Archive transfer: '+errors.decode(errors='replace')[-500:])
        return json.loads(output)
    finally:
        for process in (consumer,producer):
            if process is not None and process.poll() is None:
                process.kill();process.wait()


def main(root, supplement=None):
    root=Path(root);folder=root/'archive';folder.mkdir(mode=0o700,exist_ok=True)
    hosts=json.loads((root/'recall-hosts.json').read_text());by_name={h['host']:h for h in hosts}
    destination=by_name['julian-inspiron-3793.local']
    transport=folder/'transport.json'
    if transport.exists():
        config=json.loads(transport.read_text())
        address=str(ipaddress.IPv4Address(config['address']));port=config['port']
        if address!=(destination.get('lan_address') or urlsplit(destination['agent_endpoint']).hostname) or type(port) is not int or not 1024<=port<=65535:raise ValueError('Archive transport must use the enrolled destination')
        destination={**destination,'archive_mptcp':{'address':address,'port':port}}
    target=str(Path(destination['cache']).parent/'wgs-archive')
    manifest=json.loads((root/'catalog.json').read_text());files=validate_manifest(manifest)
    if any(f.get('sample') not in ('Watson','Venter') for f in files):raise ValueError('Unexpected archive sample')
    state_path=folder/'state.json'
    state=json.loads(state_path.read_text()) if state_path.exists() else {'files':{}}
    while True:
        try:
            extras=json.loads(Path(supplement).read_text())['files'] if supplement else []
            import re
            for item in extras:
                if item.get('sample')!='Julian' or not re.fullmatch(r'[A-Za-z0-9_.-]+',item['filename']) or item['filename'] in ('.','..'):
                    raise ValueError('Invalid private archive filename')
            all_files=files+extras
            downloads=json.loads((root/'downloads/state.json').read_text()).get('jobs',{})
            downloads={**{'private-'+str(i):{'status':'verified','receipt':item} for i,item in enumerate(extras)},**downloads}
            authenticate(destination)
            install="import pathlib,sys;p=pathlib.Path(sys.argv[1]);p.mkdir(mode=0o700,parents=True,exist_ok=True);f=p/'catalog.json';f.write_bytes(sys.stdin.buffer.read());f.chmod(0o600)"
            subprocess.run(remote(destination,install,target),input=json.dumps({**manifest,'authorized_private_supplement':extras}).encode(),check=True,timeout=30)
            state.update(host=destination['host'],path=target,files_total=len(all_files),bytes_total=sum(f['bytes'] for f in all_files),updated=stamp(),phase='waiting_for_verified_sources')
            for job in downloads.values():
                if job.get('status')!='verified':continue
                item=job['receipt'];key=item['sample']+'/'+item['filename']
                if state['files'].get(key,{}).get('sha256')==item['sha256']:continue
                state.update(phase='copying_verified_source',current=key,updated=stamp());atomic_json(state_path,state)
                try:
                    result=copy(by_name[item['host']],destination,target,item)
                except Exception as error:
                    state.setdefault('pending_errors',{})[key]=str(error)[-500:]
                    state.update(last_error=str(error)[-500:],updated=stamp());atomic_json(state_path,state)
                    continue
                state.setdefault('pending_errors',{}).pop(key,None)
                state['files'][key]=result;state.update(updated=stamp(),last_error=None);atomic_json(state_path,state)
            if len(state['files'])==len(all_files):state['phase']='complete_verified_source_archive'
            state.update(files_verified=len(state['files']),bytes_verified=sum(x['bytes'] for x in state['files'].values()),updated=stamp())
            atomic_json(state_path,state)
        except Exception as error:
            state.update(phase='waiting_for_archive_or_source_connection',last_error=str(error)[-700:],updated=stamp());atomic_json(state_path,state)
        time.sleep(30)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',required=True);parser.add_argument('--private-manifest')
    args=parser.parse_args();main(args.root,args.private_manifest)
