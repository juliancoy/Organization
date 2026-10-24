"""Reuse actual passing suites only when their relevant immutable inputs match."""
import fnmatch
import hashlib
import json
from pathlib import Path
import subprocess

COMMON=('docker_utils/','lan-monitor/distributed_worker.py','lan-monitor/suite_cache.py','lan-monitor/visual_artifacts.py',
        'scripts/launcher_agent.py','scripts/launcher_registry.py','requirements.txt','pyproject.toml')
INPUTS={
 'monitor':{'.':('lan-monitor/*.py','lan-monitor/tests/')},
 'map':{'.':('lan-monitor/*.js','lan-monitor/view/','lan-monitor/package.json','lan-monitor/package-lock.json','lan-monitor/tests/*.js')},
 'workspace':{'.':('*.py','tests/','scripts/','vm/','Dockerfile.launcher')},
 'org-api':{'OrgPortal':('org-worker/','chat-worker/','shared/','docs/','scripts/','package.json','package-lock.json','tsconfig.json')},
 'chat':{'OrgPortal':('chat-worker/','shared/','package.json','package-lock.json','tsconfig.json')},
 'web':{'OrgPortal':('web/','shared/','docs/','scripts/','package.json','package-lock.json','tsconfig.json')},
 'visual':{'OrgPortal':('web/','shared/')},
 'identity':{'PIdP':('serverless/','frontend/','scripts/','shared/','package.json','package-lock.json','tsconfig.json')},
 'identity-python':{'PIdP':('*.py','tests/','scripts/','requirements*.txt','pyproject.toml','Dockerfile')},
 'p2p':{'.':('lan-monitor/p2p_network.py','lan-monitor/p2p_remote.py','peer-worker/','scripts/launcher_peers.py','scripts/launcher_discovery.py','scripts/peer_archive.py')},
}
SUCCESS={'passed','reused'}


def selected(path,patterns):
    return any(path.startswith(pattern) if pattern.endswith('/') else fnmatch.fnmatchcase(path,pattern) for pattern in patterns)


class SuiteCache:
    def __init__(self,root,folder,specs):
        self.root,self.folder,self.specs=Path(root),Path(folder),specs
        self.trees={}

    def tree(self,name,commit):
        key=(name,commit)
        if key not in self.trees:
            output=subprocess.check_output(['git','-C',str(self.root/name),'ls-tree','-r','-z',commit],timeout=30)
            self.trees[key]=[(entry.split(b'\t',1)[1].decode(errors='surrogateescape'),entry) for entry in output.split(b'\0') if entry]
        return self.trees[key]

    def fingerprint(self,suite,host,run,images):
        selectors={name:tuple(paths) for name,paths in INPUTS[suite].items()}
        selectors['.']=selectors.get('.',())+COMMON
        content=[]
        for name,patterns in sorted(selectors.items()):
            repository=run['repositories'][name]
            entries=[entry.hex() for path,entry in self.tree(name,repository['commit']) if selected(path,patterns)]
            if not entries:raise ValueError('No source inputs found for '+suite)
            content.append((name,repository['url'],entries))
        image=images.fingerprint(host,self.specs[suite]['image'])
        if not image:raise ValueError('Image content could not be fingerprinted')
        context={'schema':1,'suite':suite,'sources':content,'spec':self.specs[suite],'image':image,
                 'machine':host['session']['machine'],'platform':host.get('agent_status',{}).get('platform'),
                 'agent':host.get('agent_status',{}).get('source_revision')}
        if suite=='org-api':context['shard']=host.get('org_api_shard')
        if suite=='p2p':
            context['fleet']=sorted((worker['host'],worker.get('session',{}).get('machine'),worker.get('lan_address'),
                worker.get('agent_status',{}).get('source_revision')) for worker in run['hosts'] if not worker.get('excluded'))
        return hashlib.sha256(json.dumps(context,sort_keys=True,separators=(',',':')).encode()).hexdigest()

    def baselines(self,current):
        results={};seen=set()
        for path in sorted(self.folder.glob('*.json'),reverse=True):
            try:previous=json.loads(path.read_text())
            except (OSError,ValueError):continue
            if previous.get('id')==current['id']:continue
            for host in previous.get('hosts',[]):
                if host.get('excluded'):continue
                for suite,job in host.get('jobs',{}).items():
                    fingerprint=job.get('input_fingerprint')
                    if not fingerprint or job.get('status')=='reused':continue
                    key=(host.get('session',{}).get('machine'),suite,fingerprint)
                    if key in seen:continue
                    if job.get('status') not in ('passed','failed','timed_out','interrupted','stopped'):continue
                    if job.get('not_executed'):continue
                    seen.add(key)
                    if job['status']=='passed':results[key]={'run_id':previous['id'],'host':host['host'],'finished':previous.get('finished')}
        return results

    def apply(self,run,images,host_names=None):
        baselines=self.baselines(run)
        reused=0
        for host in run['hosts']:
            if host.get('excluded') or (host_names is not None and host['host'] not in host_names):continue
            for suite,job in host['jobs'].items():
                try:fingerprint=self.fingerprint(suite,host,run,images)
                except (KeyError,OSError,ValueError,subprocess.SubprocessError):
                    job['cache_reason']='Relevant inputs could not be verified; suite will execute'
                    continue
                job['input_fingerprint']=fingerprint
                previous=baselines.get((host['session']['machine'],suite,fingerprint))
                if previous:
                    job.update(status='reused',phase='unchanged',not_executed=True,duration_seconds=0,
                               reused_from=previous,cache_reason='Relevant source, dependencies, image and machine match a recorded passing execution',
                               logs=['Not executed: unchanged inputs. Passing baseline: '+previous['run_id']+' on '+previous['host']])
                    reused+=1
                else:job['cache_reason']='Changed inputs or no matching passing execution'
        # A coordinated P2P run is reusable only if every participating machine matches.
        peers=[host['jobs']['p2p'] for host in run['hosts'] if not host.get('excluded') and 'p2p' in host.get('jobs',{})]
        if host_names is None and peers and not all(job['status']=='reused' for job in peers):
            for job in peers:
                if job['status']=='reused':
                    reused-=1
                    job.update(status='queued',phase=None,not_executed=False,logs=[],duration_seconds=None,
                               cache_reason='A participant requires a fresh coordinated P2P execution')
                    job.pop('reused_from',None)
        run['reuse']={'suites':reused,'policy':'relevant-source+dependencies+image+machine','fresh_passes_only':True}
