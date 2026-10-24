"""Dispatch file jobs to whichever persistent worker has CPU and memory free."""
import concurrent.futures
import time
import uuid

from agent_client import AgentClient, wait_for_events
from distributed_worker import SUITES, PYTHON
from suite_cache import SuiteCache
from work_queue import catalog, pending, claim
from worker_preparation import execution_capacity
from early_p2p import EarlyP2P


def execute(service, hosts, images):
    run = service.run
    tasks = catalog(service.root, SUITES, run['repositories'])
    from test_scope import select
    tasks = select(tasks, SUITES, run.get('selection'))
    include_p2p = not run.get('selection') or 'p2p' in run['selection']['suites']
    if run.get('selection') and run['selection']['suites'] == ['p2p'] and not run['selection']['paths']:
        tasks = []
    with service.lock:
        run.update(granular=True, task_catalog=tasks, pending_tasks=pending(service.folder, tasks))
        for host in hosts:
            host['suites'] = ['p2p'] if include_p2p else []
            host['jobs'] = {'p2p': {'name': SUITES['p2p']['name'], 'status': 'queued', 'phase': None, 'logs': []}} if include_p2p else {}
            host.pop('org_api_shard', None)
            host['active_tasks'] = []
        service.save()

    network=EarlyP2P(service,hosts,images,include_p2p)

    if not tasks:
        network.join()
        with service.lock:
            for host in hosts:host['status']='passed'
            service.save()
        return

    if run.get('selection') and not include_p2p and len(hosts) > len(tasks):
        for host in hosts[len(tasks):]:
            host.update(status='passed',not_executed=True,preparation='No additional worker needed for this focused request')
        hosts=hosts[:len(tasks)]

    def worker(index, host):
        client = AgentClient(host)
        ident = run['id'].split('-')[0] + '-' + uuid.uuid4().hex[:8]
        active = set()
        cursor = 0
        closed = False
        close_sent = False
        ready = False
        ready_bases=set()
        preparing=True
        p2p_released=False
        started = time.monotonic()
        cache = SuiteCache(service.root, service.folder, SUITES)
        baselines = cache.baselines(run)
        try:
            with service.lock:
                host.update(status='preparing', preparation='Preparing persistent file worker')
                service.save()
            # Machines initialize independently. No fleet-wide image/setup barrier.
            bases = list(dict.fromkeys(SUITES[t].get('base', t) for t in tasks))
            for image in dict.fromkeys([PYTHON] + [SUITES[b]['image'] for b in bases]):
                if run.get('stop_requested'):return
                images.ensure(image, [host])
            status=client.call('status')
            if not status.get('ready') or status.get('machine')!=host['session']['machine'] or status.get('source_revision')!=host['agent_status']['source_revision']:
                raise RuntimeError('Worker readiness or identity changed during preparation')
            with service.lock:
                host['images'] = dict(images.host_images.get(host['host'], {}))
                run['images'].update(images.images)
                host.update(agent_job_id=ident, status='running')
                host.pop('preparation', None)
                # Keep the agent request bounded even if another worker already
                # produced thousands of log lines while this machine initialized.
                manifest = {'id':ident,'granular':True,'task_catalog':tasks,'p2p_parallel':not network.done.is_set(),
                            'source_sha256':run['source_sha256'],
                            'repositories':{name:{key:repo[key] for key in ('url','commit')} for name,repo in run['repositories'].items()},
                            'hosts':[{'host':h['host'],'suites':bases if h is host else [],
                                      'images':host['images'] if h is host else {},'jobs':{}} for h in hosts]}
                service.save()
            # Inspect once per image outside the global queue lock, not once per file.
            for image in dict.fromkeys(SUITES[b]['image'] for b in bases):
                cache.image_fingerprints[(host['session']['machine'],image,host['images'].get(image))] = images.fingerprint(host,image)
            if run.get('stop_requested'):return
            client.call('job.start', {'id': ident, 'manifest': manifest, 'index': index}, timeout=180)
            deadline = time.monotonic() + 7300
            while time.monotonic() < deadline:
                if run.get('stop_requested'):
                    client.call('job.stop', {'id': ident}, timeout=40)
                    break
                result = wait_for_events(client,ident,cursor,lambda:bool(run.get('stop_requested')))
                changed = False
                if result.get('events_lost'):
                    raise RuntimeError('Worker event history has a gap')
                for item in result['events']:
                    event = item['event']
                    if event.get('kind') == 'capacity':
                        with service.lock:
                            host['capacity'] = {k: int(event[k]) for k in ('cpus', 'memory_mb')}
                            ready = True
                            changed = True
                    else:
                        if event.get('kind')=='dependencies':
                            if event.get('status') in ('ready','failed'):ready_bases.add(event['base'])
                            if event.get('status')=='complete':preparing=False
                            changed=True
                        service.update(host, event)
                    if event.get('kind') == 'suite' and event.get('status') in ('passed', 'failed', 'timed_out'):
                        active.discard(event['suite'])
                previous, cursor = cursor, result['cursor']
                if result['status'] not in ('running', 'starting'):
                    if cursor != previous:continue  # Drain final events without feeding an exited worker.
                    if result['status'] != 'completed' or active:
                        raise RuntimeError(result.get('error') or ('Worker ended before its tasks completed: '+''.join(host.get('logs',[])[-3:])))
                    break
                outgoing = []
                release_p2p=network.done.is_set() and not p2p_released
                with service.lock:
                    if ready and not closed:
                        budget=execution_capacity(host['capacity'],preparing=preparing,p2p=not network.done.is_set())
                        host['execution_capacity']=budget
                        while len(outgoing)<128:
                            task = claim(run['pending_tasks'], SUITES, budget, active, ready_bases=ready_bases)
                            if task is None:break
                            changed = True
                            job = {'name': SUITES[task]['name'], 'suite': SUITES[task].get('base', task),
                                   'file': SUITES[task].get('file'), 'status': 'queued', 'phase': None, 'logs': []}
                            job.update(cpus=SUITES[task].get('cpus',1),memory_mb=SUITES[task].get('memory_mb',1536))
                            host['suites'].append(task)
                            host['jobs'][task] = job
                            candidate = {**run, 'hosts': [{**host, 'jobs': {task: job}}]}
                            cache.apply(candidate, images, baselines=baselines, force=run.get('force',False))
                            if job['status'] == 'reused':continue
                            active.add(task)
                            outgoing.append(task)
                        changed = changed or host['active_tasks'] != sorted(active)
                        host['active_tasks'] = sorted(active)
                        # Once all work is claimed, slow workers finish their own files;
                        # faster workers are never forced to repeat a suite or wait idle.
                        closed = not run['pending_tasks'] and not active
                        available = [h for h in hosts if h.get('capacity') and h['status'] != 'unavailable']
                        if (run['pending_tasks'] and len(available) == len([h for h in hosts if h['status'] != 'unavailable'])
                                and not any(claim(list(run['pending_tasks']),SUITES,h['capacity'],set()) is not None for h in available)):
                            raise RuntimeError('Remaining tasks exceed every available worker CPU/memory budget')
                        if changed or (closed and not close_sent):service.save()
                if outgoing or (closed and not close_sent) or release_p2p:
                    p2p_finished=network.done.is_set()
                    client.call('job.feed', {'id': ident, 'tasks': outgoing, 'close': closed,'p2p_finished':p2p_finished})
                    p2p_released=p2p_finished
                    close_sent = closed
            else:
                raise RuntimeError('Persistent worker exceeded its deadline')
            with service.lock:
                host['status'] = 'stopped' if run.get('stop_requested') else 'passed' if all(j['status'] in ('passed', 'reused') for t, j in host['jobs'].items() if t != 'p2p') else 'failed'
        except Exception as error:
            try:client.call('job.stop', {'id': ident}, timeout=40)
            except Exception:pass
            with service.lock:
                host.update(status='unavailable', error=type(error).__name__ + ': ' + str(error)[:500])
                for task in active:
                    host['jobs'][task].update(status='unavailable', phase='worker_lost')
        finally:
            client.close()
            with service.lock:
                from distributed import now
                host.update(active_tasks=[], finished=now(), duration_seconds=round(time.monotonic() - started, 2))
                service.save()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(hosts)) as pool:
            futures = [pool.submit(worker, index, host) for index, host in enumerate(hosts)]
            for future in concurrent.futures.as_completed(futures):future.result()
    finally:
        network.join()
    if run['pending_tasks'] and not run.get('stop_requested'):
        raise RuntimeError('No worker could execute the remaining ' + str(len(run['pending_tasks'])) + ' test jobs')
