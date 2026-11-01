"""Durable bounded retries of already-published application test jobs."""
import hashlib
import json
import time
from task_events import EventStore,encode


class RetryQueue:
    def __init__(self,store=None):
        self.store=store or EventStore()
        with self.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS retry_jobs(id TEXT PRIMARY KEY,parent_run TEXT NOT NULL,task TEXT NOT NULL,attempts INTEGER NOT NULL,state TEXT NOT NULL,next_due REAL,reason TEXT,child_run TEXT,updated REAL NOT NULL)')
    def offer(self,parent,task,attempt=1,blocked=False,reason=None):
        family=parent.get('retry_family',parent['id']);ident=family+':'+task
        with self.store.connect() as db:
            state='blocked' if blocked else 'exhausted' if attempt>=3 else 'backoff'
            delay=30*2**max(0,attempt-1);jitter=.8+int(hashlib.sha256(ident.encode()).hexdigest()[:4],16)/65535*.4
            due=time.time()+delay*jitter if state=='backoff' else None
            db.execute('INSERT OR IGNORE INTO retry_jobs VALUES(?,?,?,?,?,?,?,?,?)',(ident,parent['id'],task,attempt,state,due,reason,None,time.time()))
    def claim(self,now=None):
        now=time.time() if now is None else now
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # Only admission claims expire; an actual child run is reconciled, not redispatched.
            db.execute("UPDATE retry_jobs SET state='backoff',next_due=? WHERE state='claiming' AND updated<?",(now,now-120))
            row=db.execute("SELECT * FROM retry_jobs WHERE state='backoff' AND next_due<=? ORDER BY next_due LIMIT 1",(now,)).fetchone()
            if not row:return None
            db.execute("UPDATE retry_jobs SET state='claiming',updated=? WHERE id=?",(now,row[0]))
            return dict(zip(('id','parent_run','task','attempts','state','next_due','reason','child_run','updated'),row))
    def dispatched(self,ident,child):
        with self.store.connect() as db:
            db.execute("UPDATE retry_jobs SET state='running',child_run=?,attempts=attempts+1,updated=? WHERE id=? AND state='claiming'",(child,time.time(),ident))
    def defer(self,ident,reason):
        with self.store.connect() as db:db.execute("UPDATE retry_jobs SET state='backoff',next_due=?,reason=?,updated=? WHERE id=? AND state='claiming'",(time.time()+30,reason[:300],time.time(),ident))
    def block(self,ident,reason):
        with self.store.connect() as db:db.execute("UPDATE retry_jobs SET state='blocked',next_due=NULL,reason=?,updated=? WHERE id=?",(reason[:300],time.time(),ident))
    def finish(self,ident,success,blocked=False,reason=None,parent_run=None):
        with self.store.connect() as db:
            row=db.execute('SELECT attempts FROM retry_jobs WHERE id=?',(ident,)).fetchone()
            if not row:return
            state='completed' if success else 'blocked' if blocked else 'exhausted' if row[0]>=3 else 'backoff'
            due=time.time()+min(900,30*2**(row[0]-1)) if state=='backoff' else None
            db.execute('UPDATE retry_jobs SET state=?,next_due=?,reason=?,updated=?,parent_run=coalesce(?,parent_run) WHERE id=?',(state,due,reason,time.time(),parent_run,ident))
    def rows(self):
        with self.store.connect() as db:
            return [dict(zip(('id','parent_run','task','attempts','state','next_due','reason','child_run','updated'),row)) for row in db.execute('SELECT * FROM retry_jobs ORDER BY updated DESC')]


def blocked(job):
    return job.get('exit_code') in (126,127,137) or job.get('status') in ('stopped','conflict')


def run(service):
    queue=RetryQueue();store=queue.store
    # Existing historical failures are retained, not silently rerun months later.
    with store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS retry_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        db.execute("INSERT OR IGNORE INTO retry_settings VALUES('enabled_since',?)",(str(time.time()),))
        enabled=float(db.execute("SELECT value FROM retry_settings WHERE key='enabled_since'").fetchone()[0])
    while True:
        try:
            with service.lock:
                available=not service.active() and not (service.runner.folder/'DRAIN').exists()
            records={r['id']:r for r in store.list_runs('application-tests')}
            for row in queue.rows():
                if row['state']=='running' and row['child_run'] in records:
                    child=records[row['child_run']]
                    if child.get('finished'):
                        jobs=[j for h in child.get('hosts',[]) for k,j in h.get('jobs',{}).items() if k==row['task']]
                        success=any(j.get('status') in ('passed','reused') for j in jobs)
                        queue.finish(row['id'],success,any(blocked(j) for j in jobs),None if success else 'Retry failed; history retained',parent_run=child['id'])
                        store.append(row['parent_run'],'retry-system','retry-completed' if success else 'retry-failed',{'task':row['task'],'child_run':child['id'],'attempt':row['attempts']},'retry-result:'+child['id']+':'+row['task'])
            for parent in records.values():
                if not parent.get('finished') or parent.get('retry_family') or not parent.get('repositories') or parent.get('stop_requested'):continue
                from datetime import datetime
                if datetime.fromisoformat(parent['finished']).timestamp()<enabled:continue
                verified={key for host in parent.get('hosts',[]) for key,job in host.get('jobs',{}).items() if job.get('status') in ('passed','reused')}
                for host in parent.get('hosts',[]):
                    for task,job in host.get('jobs',{}).items():
                        if task in verified or task in ('p2p','mobile','gpu') or job.get('status') not in ('failed','timed_out','unavailable','interrupted'):continue
                        queue.offer(parent,task,blocked=blocked(job),reason='Failed on '+host['host'])
            if available:
                ticket=queue.claim()
                if ticket:
                    try:
                        parent=records.get(ticket['parent_run'])
                        if not parent:raise RuntimeError('Pinned parent record unavailable')
                        runner=service.retry_pinned(parent,ticket)
                        queue.dispatched(ticket['id'],runner.run['id'])
                        store.append(parent['id'],'retry-system','retry-dispatched',{'task':ticket['task'],'child_run':runner.run['id'],'attempt':ticket['attempts']+1})
                        __import__('threading').Thread(target=runner.execute,daemon=True).start()
                    except Exception as error:
                        reason=type(error).__name__+': '+str(error)
                        if isinstance(error,ValueError):queue.block(ticket['id'],reason)
                        else:queue.defer(ticket['id'],reason)
        except Exception:__import__('logging').exception('Retry admission deferred')
        time.sleep(5)
