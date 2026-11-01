#!/usr/bin/env python3
"""Install one scoped, autonomous QC run from the enrolled controller host.

Uses pinned existing SSH enrollment only for bootstrap. The controller key never
leaves this host. Runtime workers use per-node mTLS and local resource leases.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import secrets
import shlex
import subprocess
import sys
import time

INIT = r'''
import datetime,hashlib,json,os,socket,subprocess,sys
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID
p=json.load(sys.stdin);home=Path.home();enrollment=json.loads((home/'.config/lifetech-launcher/peers.json').read_text())
if enrollment['name']!=p['name']:raise ValueError('Enrolled node name mismatch')
if enrollment['controller_public_key']!=p['authority']:raise ValueError('Different owner authority')
with socket.socket() as s:s.bind(('0.0.0.0',p['port']))
root=home/'.local/state/venter-peer-qc'/p['run'];root.mkdir(parents=True,exist_ok=False,mode=0o700)
key=Ed25519PrivateKey.generate();subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,p['name'])]);now=datetime.datetime.utcnow()
cert=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=5)).not_valid_after(now+datetime.timedelta(days=8)).add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True).sign(key,algorithm=None))
(root/'identity.key').write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));(root/'identity.key').chmod(0o600)
pem=cert.public_bytes(serialization.Encoding.PEM).decode();(root/'identity.pem').write_text(pem)
image=subprocess.check_output(['docker','image','inspect','lifetech/wgs-tools:bookworm-20261026','--format','{{.Id}}'],text=True).strip()
versions=subprocess.check_output(['docker','run','--rm','--pull=never','--network','none','--read-only','--cpus','1','--memory','256m','--memory-swap','256m','--cap-drop','ALL',image,'bash','-c','samtools --version | head -2; bcftools --version | head -2; python3 --version'],text=True)
route=json.loads(subprocess.check_output(['ip','-j','route','get','10.42.0.1'],text=True))[0]
sources={}
if p['name']=='julian-latitude.local':
 data=home/'Downloads/HuRef2.0';receipts=json.loads((data/'verified-receipts.json').read_text())
 for spec in p['inputs']:
  target=(data/spec['path']).resolve();target.relative_to(data.resolve());st=target.stat();r=receipts[spec['path']]
  if not r['verified'] or r['md5']!=spec['source_md5'] or st.st_size!=spec['bytes'] or r['bytes']!=spec['bytes']:raise ValueError('Source verification receipt mismatch')
  if any(r[k]!=v for k,v in [('mtime_ns',st.st_mtime_ns),('ctime_ns',st.st_ctime_ns),('inode',st.st_ino),('device',st.st_dev)]):raise ValueError('Source file changed since verification')
  sources[spec['source_md5']]={'path':str(target),**{k:getattr(st,k) for k in ['st_size','st_mtime_ns','st_ctime_ns','st_ino','st_dev']}}
pool=None
for proc in Path('/proc').iterdir():
 try:
  argv=(proc/'cmdline').read_bytes().decode().split('\0')
  if any(Path(a).name in ('launcher_peers.py','launcher_peer_supervisor.py') for a in argv[1:6]):
   env=dict(x.split('=',1) for x in (proc/'environ').read_bytes().decode().split('\0') if '=' in x)
   if env.get('SWARM_RESOURCE_DIR'):pool=env['SWARM_RESOURCE_DIR'];break
   if env.get('LIFETECH_JOB_ROOT'):pool=str(Path(env['LIFETECH_JOB_ROOT'])/'.resources');break
 except (OSError,ValueError):pass
if pool is None:
 try:
  agent=json.loads(subprocess.check_output(['docker','inspect','lifetech-launcher'],stderr=subprocess.DEVNULL))[0]
  env=dict(x.split('=',1) for x in agent['Config']['Env'] if '=' in x)
  pool=env.get('SWARM_RESOURCE_DIR') or str(Path(env['LIFETECH_JOB_ROOT'])/'.resources')
 except (subprocess.SubprocessError,KeyError):
  fallback=home/'.local/state/lifetech-launcher/jobs/.resources'
  if not fallback.exists():raise RuntimeError('Cannot identify shared native resource pool')
  pool=str(fallback)
config={'name':p['name'],'sources':sources,'resource_directory':pool}
(root/'config.json').write_text(json.dumps(config));(root/'config.json').chmod(0o600)
print(json.dumps({'root':str(root),'certificate':pem,'machine':hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest(),'address':route.get('prefsrc') or route.get('src'),'port':p['port'],'image':image,'tool_versions':versions,'uid':os.getuid(),'free_bytes':__import__('shutil').disk_usage(home).free}))
'''
INSTALL = r'''
import base64,json,subprocess,sys
from pathlib import Path
p=json.load(sys.stdin);root=Path(p['root'])
for name,value in p['files'].items():
 if name not in ['worker.py','lease.py','manifest.json']:raise ValueError('Invalid deployed file')
 (root/name).write_bytes(base64.b64decode(value));(root/name).chmod(0o600)
units=Path.home()/'.config/systemd/user';units.mkdir(parents=True,exist_ok=True)
unit=units/'venter-peer-qc.service'
if unit.exists():raise ValueError('Existing peer QC service requires explicit migration; not overwritten')
unit.write_text('[Unit]\nDescription=Autonomous signed public Venter QC peer\nAfter=network-online.target\n[Service]\nType=simple\nExecStart=/usr/bin/python3 '+str(root/'worker.py')+' --root '+str(root)+'\nWorkingDirectory='+str(root)+'\nUMask=0077\nRestart=on-failure\nRestartSec=5\nTimeoutStopSec=45\nTasksMax=128\n[Install]\nWantedBy=default.target\n')
subprocess.run(['systemctl','--user','daemon-reload'],check=True)
print(json.dumps({'installed':True,'root':str(root)}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', type=Path, required=True)
    parser.add_argument('--label', default='20261031')
    parser.add_argument('--title', required=True, help='Human-readable task title shown in the swarm dashboard')
    parser.add_argument('--input-mode',choices=['auto','stream','cache'],default='auto')
    parser.add_argument('--parallel-streams',type=int,choices=range(1,9),default=2)
    args = parser.parse_args()
    # Run from the controller's existing Organization checkout.
    from docker_utils.policy import inventory, ssh
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    import base64
    key = Ed25519PrivateKey.from_private_bytes((Path.home()/'.config/map-master/controller-signing.key').read_bytes())
    authority = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    enrollment = json.loads((Path.home()/'.config/lifetech-launcher/peers.json').read_text())
    plan = json.loads((args.build/'plan.json').read_text())
    sys.path.insert(0,str(args.build))
    import worker
    sources = [j['spec'] for j in plan['jobs']]
    jobs = []
    for old in plan['jobs']:
        spec = {k: old['spec'][k] for k in ['dataset','path','bytes','source_url','source_md5','kind','scope']}
        spec['recipe_sha256'] = hashlib.sha256(worker.canonical(worker.recipe(spec))).hexdigest()
        jobs.append({'id':hashlib.sha256(worker.canonical(spec)).hexdigest(),'spec':spec,
                     'preferred_worker':old['placement']['preferred_worker'],
                     'data_local_only':spec['kind']=='bam-read-qc'})
        if spec['kind']=='bam-read-qc' and args.input_mode!='cache':
            jobs[-1].update(source_worker='julian-latitude.local',preferred_worker=enrollment['name'],input_access='sequential')
    ident = 'venter-qc-'+args.label+'-'+secrets.token_hex(4)
    hosts = {h['host']:h for h in inventory() if h['host'] in plan['ready_workers'] or h['host']=='julian-latitude.local'}
    hosts[enrollment['name']]={'host':enrollment['name'],'local':True}
    def execute(host, argv, payload=None, timeout=90):
        command=argv if host.get('local') else ssh(host)+[shlex.join(argv)]
        result=subprocess.run(command,input=worker.canonical(payload) if payload is not None else None,capture_output=True,timeout=timeout)
        if result.returncode:raise RuntimeError(result.stderr.decode(errors='replace')[-1000:])
        return result.stdout
    initialized = {}; errors = {}
    def initialize(name):
        try:
            value=json.loads(execute(hosts[name],['python3','-c',INIT],{'name':name,'authority':authority,'port':8796,'run':ident,'inputs':sources}))
            return name,value,None
        except Exception as error:return name,None,str(error)
    with ThreadPoolExecutor(max_workers=5) as pool:
        for name,value,error in pool.map(initialize,hosts):
            if error:errors[name]=error
            else:initialized[name]=value
            print('PREFLIGHT',name,'ready' if value else error,flush=True)
    if 'julian-latitude.local' not in initialized or len(initialized)<3:
        raise RuntimeError('Source owner and at least two other workers required')
    for job in jobs:
        if job['preferred_worker'] not in initialized:
            job['preferred_worker']=worker.ranked(job,initialized)[0]
    manifest={'schema':'venter-peer-qc.v1','id':ident,'title':args.title,'created':time.time()-30,'expires':time.time()+7*86400,
              'worker_sha256':worker.digest(args.build/'worker.py'),'lease_sha256':worker.digest(args.build/'lease.py'),
              'nodes':initialized,'jobs':jobs,'execution_semantics':'at-least-once; signed content-addressed receipts; partition duplicates possible',
              'source_scope':'public verified Venter collection; incomplete HAC FASTQ excluded',
              'transfer':{'encryption':'mutual TLS 1.3','buffer_bytes':worker.BLOCK,'resume':'byte ranges plus full cached input publisher MD5','dma':'ordinary NIC driver DMA; no RDMA device or zero-copy claim'}}
    envelope=worker.signed(manifest,key)
    bundle=Path.home()/'.local/state/venter-peer-qc'/ident
    bundle.mkdir(parents=True,exist_ok=True,mode=0o700)
    worker.atomic(bundle/'deployment.json',{'run':ident,'nodes':initialized,'unavailable':errors})
    worker.atomic(bundle/'manifest.json',envelope)
    files={'worker.py':(args.build/'worker.py').read_bytes(),'lease.py':(args.build/'lease.py').read_bytes(),'manifest.json':worker.canonical(envelope)}
    for name,node in initialized.items():
        payload={'root':node['root'],'files':{n:base64.b64encode(data).decode() for n,data in files.items()}}
        print('INSTALL',name,json.loads(execute(hosts[name],['python3','-c',INSTALL],payload)),flush=True)
    # All authorization and certificates are in place before any daemon starts.
    for name in initialized:
        execute(hosts[name],['systemctl','--user','enable','--now','venter-peer-qc.service'])
        print('STARTED',name,flush=True)
    if args.input_mode!='cache' and any(j['spec']['kind']=='bam-read-qc' for j in jobs):
        from scripts.run_manager import configure
        configure(bundle,args.input_mode,args.parallel_streams)
    print('RUN',ident,flush=True)


if __name__ == '__main__':main()
