"""Bounded, user-triggered application shards over authenticated launcher agents."""
import concurrent.futures
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid
from distributed_worker import SUITES
from distributed_images import ImageDistributor

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(__file__).with_name('.runtime') / 'distributed'
HISTORY_LIMIT = 10
FINAL = {'partial', 'passed', 'failed', 'unavailable', 'timed_out', 'interrupted', 'stopped'}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def targets():
    from agent_client import CONFIG
    config=json.loads(CONFIG.read_text())
    peers=config.get('peers',{})
    if not 1<=len(peers)<=24:raise ValueError('Enroll between one and 24 launcher agents')
    return [{'host':name,'local':name==config['name']} for name in sorted(peers)]


def no_remote_shell(target):
    raise RuntimeError('SSH is not an execution transport; provision an authenticated launcher agent')


def plan(hosts):
    hosts = copy.deepcopy(hosts)
    for host in hosts:
        host.update(status='queued', suites=['monitor', 'map', 'p2p'], jobs={})
    for index, suite in enumerate(['org-api', 'web', 'identity', 'chat', 'workspace', 'identity-python']):
        # Browser/identity work goes to the last host; frontend to the middle.
        slot = {'org-api': 0, 'chat': 0, 'web': 1, 'workspace': 3, 'identity': 2, 'identity-python': 2}[suite] % len(hosts)
        hosts[slot]['suites'].append(suite)
    for host in hosts:
        host['jobs'] = {suite: {'name': SUITES[suite]['name'], 'status': 'queued', 'phase': None, 'logs': []} for suite in host['suites']}
    return hosts




class DistributedTests:
    def __init__(self, root=ROOT, state=STATE):
        self.root, self.folder = Path(root), Path(state)
        self.lock = threading.RLock()
        self.running = False
        self.run = None
        self.last_save = 0
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.prune()
        files = sorted(self.folder.glob('*.json'))
        if files:
            try:
                self.run = json.loads(files[-1].read_text())
                if self.run.get('status') in ('running', 'preparing', 'stopping'):
                    self.run.update(status='interrupted', finished=now())
                    for host in self.run['hosts']:
                        if host['status'] not in FINAL:
                            host['status'] = 'interrupted'
                        for job in host.get('jobs', {}).values():
                            if job.get('status') not in FINAL:
                                job['status'] = 'interrupted'
                    self.save()
            except (OSError, ValueError):
                self.run = None

    def prune(self):
        cutoff = time.time() - 30 * 86400
        files = sorted(self.folder.glob('*.json'), reverse=True)
        for index, path in enumerate(files):
            if index >= 100 or path.stat().st_mtime < cutoff:
                path.unlink()

    def save(self):
        if not self.run:
            return
        path = self.folder / (self.run['id'] + '.json')
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.run))
        temporary.chmod(0o600)
        temporary.replace(path)
        self.last_save = time.monotonic()

    def view(self, ident=None):
        with self.lock:
            current = copy.deepcopy(self.run)
            if ident and (not current or ident != current['id']):
                if not re.fullmatch(r'[0-9TZ-]+-[a-f0-9]{8}', ident):
                    raise ValueError('Invalid run ID')
                path = self.folder / (ident + '.json')
                if not path.is_file():
                    raise ValueError('Run not found')
                current = json.loads(path.read_text())
            history = []
            for path in sorted(self.folder.glob('*.json'), reverse=True)[:100]:
                try:
                    data = json.loads(path.read_text())
                    item={key: data.get(key) for key in ('id', 'started', 'finished', 'status', 'source_sha256')}
                    jobs=[job for host in data.get('hosts',[]) for job in host.get('jobs',{}).values()]
                    item.update(suites=len(jobs),passed=sum(job.get('status')=='passed' for job in jobs),
                                failed=sum(job.get('status') in ('failed','timed_out','unavailable','interrupted') for job in jobs))
                    history.append(item)
                    if len(history)>=HISTORY_LIMIT:break
                except (OSError, ValueError):
                    pass
            return {'running': self.running, 'run': current, 'history': history, 'history_limit': HISTORY_LIMIT, 'retention_days': 30}

    def start(self):
        with self.lock:
            if self.running:
                return False
            hosts = plan(targets())
            self.prune()
            self.run = {'id': dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8],
                        'status': 'preparing', 'started': now(), 'finished': None,
                        'hosts': hosts, 'repositories': {}, 'source_sha256': None,
                        'scope': 'Start commits and pushes Organization, OrgPortal and PIdP on their current branches; verified local agents fetch exact GitHub commits into fresh directories. Application suites run in isolated containers without network or deployment credentials. The dedicated P2P suite uses separate browser containers on authenticated agent machines, synthetic fixtures and short-lived dataset invitations; its network access is required for WebRTC.'}
            self.running = True
            self.save()
            threading.Thread(target=self.execute, daemon=True).start()
            return True

    def stop(self):
        with self.lock:
            if not self.running or self.run.get('stop_requested'):
                return False
            self.run.update(stop_requested=True, status='stopping')
            self.save()
            threading.Thread(target=self.stop_hosts, args=(self.run,), daemon=True).start()
            return True

    def stop_hosts(self, run):
        from agent_client import AgentClient
        def stop_host(host):
            if host.get('excluded') or not host.get('agent_endpoint'):return
            try:
                result=AgentClient(host).call('job.stop',{'id':run['id']},timeout=40)
                host['stop_acknowledged']=result.get('acknowledged') is True
            except Exception as error:host['stop_error']=str(error)[:400]
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1,len(run['hosts']))) as pool:
            list(pool.map(stop_host,run['hosts']))

    def update(self, host, event):
        with self.lock:
            kind = event.get('kind')
            if kind == 'host':
                host.update(observed_hostname=event.get('hostname'), cpus=event.get('cpus'), python=event.get('python'))
            if kind=='source':
                host.setdefault('source_sync',{})[event['repository']]={key:event[key] for key in ('commit','status') if key in event}
                host['status']='running' if len(host['source_sync'])==len(self.run['repositories']) and all(row.get('status')=='verified' for row in host['source_sync'].values()) else 'preparing'
            suite = event.get('suite')
            if suite in host['jobs']:
                job = host['jobs'][suite]
                if kind == 'suite':
                    job.update({key: event[key] for key in ('status', 'exit_code', 'duration_seconds') if key in event})
                    if job['status'] == 'running':
                        job['started'] = now()
                elif kind == 'phase':
                    job['phase'] = event.get('phase')
                elif kind == 'failure':
                    job.setdefault('failure_details', []).append(str(event.get('line', ''))[:2000])
                    job['failure_details'] = job['failure_details'][:240]
                elif kind == 'counts':
                    job['test_counts'] = {key: value for key, value in event.get('counts', {}).items() if key in ('tests', 'passed', 'failed', 'skipped') and isinstance(value, int) and value >= 0}
                elif kind == 'log':
                    job['logs'].append(str(event.get('line', ''))[:2000])
                    job['logs'] = job['logs'][-80:]
            self.run['updated'] = now()
            if kind != 'log' or time.monotonic() - self.last_save > 1:
                self.save()

    def host(self, index, host, manifest, ident):
        from agent_client import AgentClient
        started=time.monotonic();client=AgentClient(host);cursor=0
        try:
            with self.lock:
                host.update(status='preparing',started=now());self.save()
            if self.run.get('stop_requested'):return
            client.call('job.start',{'id':ident,'manifest':manifest,'index':index},timeout=180)
            with self.lock:host['status']='running';self.save()
            deadline=time.monotonic()+7300
            while time.monotonic()<deadline:
                if self.run.get('stop_requested'):
                    client.call('job.stop',{'id':ident},timeout=40)
                result=client.call('job.poll',{'id':ident,'cursor':cursor})
                if result.get('events_lost'):raise RuntimeError('Agent event history has a gap; result cannot be verified')
                for item in result['events']:self.update(host,item['event'])
                previous=cursor;cursor=result['cursor']
                if result['status'] not in ('running','starting') and cursor==previous:
                    jobs=[j for suite,j in host['jobs'].items() if not SUITES[suite].get('coordinated')]
                    host['status']='stopped' if result['status']=='stopped' else 'passed' if result['status']=='completed' and all(j['status']=='passed' for j in jobs) else 'failed'
                    if result.get('error'):host['error']=result['error']
                    break
                time.sleep(.5)
            else:
                client.call('job.stop',{'id':ident},timeout=40)
                raise RuntimeError('Agent job exceeded coordinator deadline')
        except InterruptedError:
            host['status']='stopped'
        except Exception as error:
            host.update(status='unavailable',error=type(error).__name__+': '+str(error)[:500])
            try:client.call('job.stop',{'id':ident},timeout=40)
            except Exception:pass
        finally:
            with self.lock:
                host.update(finished=now(),duration_seconds=round(time.monotonic()-started,2))
                for suite,job in host['jobs'].items():
                    if SUITES[suite].get('coordinated') and not self.run.get('stop_requested'):continue
                    if job['status'] not in FINAL:job['status']='stopped' if self.run.get('stop_requested') else 'unavailable'
                self.save()

    def image_progress(self, image, phase, source, destination):
        with self.lock:
            self.run['image_progress'] = {'image': image, 'phase': phase, 'source': source, 'destination': destination}
            self.run['updated'] = now()
            self.save()

    def execute(self):
        try:
            with self.lock:
                run = self.run
                hosts = run['hosts']
            images = ImageDistributor(hosts, no_remote_shell, lambda: bool(run.get('stop_requested')), self.image_progress)
            from agent_client import AgentSessions
            def session_progress(host, phase, detail):
                with self.lock:
                    host['session'] = {'status':phase, **(detail or {})}
                    run['preparation'] = 'Launcher session: '+host['host']+' · '+phase
                    self.save()
            sessions = AgentSessions(self.root, hosts,
                lambda:bool(run.get('stop_requested')),session_progress)
            excluded = {}
            def exclude(candidates, survivors, stage):
                names = {host['host'] for host in survivors}
                for host in candidates:
                    if host['host'] in names:continue
                    reason = host.get('error') or images.failures.get(host['host'],{}).get('reason') or 'Worker verification failed'
                    host.update(status='unavailable',excluded=True,exclusion_stage=stage,error=reason,finished=now())
                    for job in host.get('jobs',{}).values():
                        job.update(status='unavailable',not_executed=True,phase=stage)
                        job.setdefault('logs',[]).append('Not executed: '+reason)
                    excluded[host['host']] = host
            def publish():
                with self.lock:
                    run['hosts'] = hosts + list(excluded.values())
                    run['verified_hosts'] = [host['host'] for host in hosts if host.get('session',{}).get('verified')]
                    run['unavailable_hosts'] = list(excluded)
                    run['excluded_hosts'] = [{'host':h['host'],'stage':h['exclusion_stage'],'reason':h['error']} for h in excluded.values()]
                    self.save()
            reachable = sessions.accessible()
            exclude(hosts,reachable,'preflight')
            hosts = plan(reachable) if reachable else []
            images.hosts = hosts
            publish()
            initialized = sessions.verify(hosts) if hosts else []
            exclude(hosts,initialized,'launcher_verification')
            hosts = initialized
            while hosts:
                hosts = plan(hosts)
                images.hosts = hosts
                publish()
                image_ids = images.prepare()
                prepared = [host for host in hosts if host['host'] not in images.failures]
                exclude(hosts,prepared,'image_preparation')
                verified = sessions.verify(prepared)
                exclude(prepared,verified,'readiness_verification')
                if len(verified)==len(hosts):break
                hosts = verified
            publish()
            if not hosts:
                raise RuntimeError('No verified connected workers; see excluded machine details')
            from github_sources import publish as publish_github,source_digest
            def publication_progress(name,record):
                with self.lock:
                    run['repositories'][name]=record
                    run['preparation']='GitHub: '+name+' · '+record['stage']
                    self.save()
            repositories=publish_github(self.root,run['id'],lambda:bool(run.get('stop_requested')),publication_progress)
            # Publishing can take time: verify workers again and reassign if one disappeared.
            while hosts:
                verified=sessions.verify(hosts)
                exclude(hosts,verified,'post_publication_readiness')
                if len(verified)==len(hosts):break
                hosts=plan(verified) if verified else []
                while hosts:
                    images.hosts=hosts
                    publish()
                    image_ids=images.prepare()
                    prepared=[host for host in hosts if host['host'] not in images.failures]
                    exclude(hosts,prepared,'image_preparation')
                    if len(prepared)==len(hosts):break
                    hosts=plan(prepared) if prepared else []
            publish()
            if not hosts:raise RuntimeError('Commits were published, but no verified workers remain; see excluded machines')
            with self.lock:
                run.pop('preparation',None)
                run['repositories']=repositories
                run['source_sha256']=source_digest(repositories)
                run['source_transport']='github'
                run['images']=image_ids
                for host in hosts:host['images']=images.host_images[host['host']]
                run['status']='stopping' if run.get('stop_requested') else 'running'
                manifest=copy.deepcopy(run)
                self.save()
            if run.get('stop_requested'):
                return
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(hosts)) as pool:
                futures = [pool.submit(self.host, index, host, manifest, run['id']) for index, host in enumerate(hosts)]
                for future in futures:
                    future.result()
            if not run.get('stop_requested'):
                self.network(run)
            with self.lock:
                run['status'] = ('failed' if any(host['status'] not in ('passed','partial') for host in hosts) else
                                 'partial' if excluded or any(host['status']=='partial' for host in hosts) else 'passed')
        except Exception as exc:
            with self.lock:
                self.run.update(status='failed', error=type(exc).__name__ + ': ' + str(exc)[:500])
        finally:
            with self.lock:
                if self.run.get('stop_requested'):
                    self.run['status'] = 'stopped'
                    for host in self.run['hosts']:
                        if host['status'] not in FINAL:
                            host['status'] = 'stopped'
                        for job in host['jobs'].values():
                            if job['status'] not in FINAL:
                                job['status'] = 'stopped'
                elif self.run.get('status') == 'failed':
                    for host in self.run['hosts']:
                        if host['status'] not in FINAL:
                            host['status'] = 'failed'
                        for job in host['jobs'].values():
                            if job['status'] not in FINAL:
                                job.update(status='failed',phase='infrastructure')
                self.running = False
                self.run['finished'] = now()
                self.save()

    def network(self,run):
        from p2p_network import NetworkP2P
        hosts=[host for host in run['hosts'] if not host.get('excluded') and host.get('session',{}).get('verified')
               and host.get('status')!='unavailable' and 'p2p' in host.get('jobs',{})]
        if len(hosts)<2:
            with self.lock:
                reason='At least two verified connected machines required; network coverage not measured'
                run['p2p']={'status':'unavailable','error':reason,'checks':[]}
                for host in hosts:
                    host['jobs']['p2p'].update(status='unavailable',not_executed=True,phase='coverage')
                    host['jobs']['p2p']['logs'].append(reason)
                    host['status']='partial' if all(j['status']=='passed' for key,j in host['jobs'].items() if key!='p2p') else 'failed'
                self.save()
            return
        started=time.monotonic()
        with self.lock:
            for host in hosts:
                host['jobs']['p2p'].update(status='running',phase='network',started=now())
                host['status']='running'
            self.save()
        result=NetworkP2P(hosts,run['id'],emit=self.update,
                          stopped=lambda:bool(run.get('stop_requested')),source_commit=run['repositories']['.']['commit']).run()
        with self.lock:
            run['p2p']=result
            for host in hosts:
                job=host['jobs']['p2p'];job.update(status=result['status'],duration_seconds=round(time.monotonic()-started,2),
                    test_counts={'tests':4,'passed':sum(c['status']=='passed' for c in result['checks']),
                                 'failed':0 if result['status']=='passed' else 1,'skipped':0})
                job['logs'].append('Direct network coverage: '+result['coverage']['direct']+'; cross-NAT: '+result['coverage']['cross_nat']+'; TURN: '+result['coverage']['turn'])
                if result.get('error'):job.setdefault('failure_details',[]).append(result['error'])
                for check in result['checks']:
                    routes=list(check.get('routes',[]))
                    for receiver in check.get('receivers',[]):routes.extend(receiver['routes'])
                    for route in routes:
                        pair=route['pair'];job['logs'].append(check['name']+': '+json.dumps(pair,separators=(',',':')))
                host['status']='passed' if all(j['status']=='passed' for j in host['jobs'].values()) else 'failed'
                host['finished']=now()
            self.save()
