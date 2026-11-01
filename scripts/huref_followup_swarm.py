"""Durable native-coordinate follow-up queue; missing scientific inputs stay explicit."""
import argparse
import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import gzip
import io
import json
import os
import subprocess
from pathlib import Path
import tarfile
import time

from scripts.huref_compare_swarm import Comparison, ROOT, digest, save, distributed


def worker_source():
    base=(ROOT/'scripts/huref_compare_worker.py').read_text().split("if __name__=='__main__':")[0]
    return base+'\n'+(ROOT/'scripts/huref_followup_worker.py').read_text()


def runnable(job,states,node):
    return (states[job['id']]['status'] in ('queued','waiting_capacity','retrying')
            and states[job['id']].get('retry_after',0)<=time.time()
            and (not job.get('node') or job['node']==node)
            and all(states[d]['status']=='completed' for d in job.get('depends_on',[])))


def transient(error):
    return isinstance(error,(OSError,ConnectionError,TimeoutError)) or any(text in str(error) for text in ('Agent HTTP status','Agent is applying an explicit update','Agent is draining','Launcher is busy','Owner maintenance:','Executor shutdown has been called'))


class Followup(Comparison):
    def __init__(self,folder,original):
        super().__init__(folder)
        self.original=original
        self.worker_source=worker_source()

    def enroll(self):
        super().enroll()
        probe="import json,resource,subprocess;options=json.loads(subprocess.check_output(['docker','info','--format','{{json .SecurityOptions}}'],text=True));print(resource.getrlimit(resource.RLIMIT_MEMLOCK)[1] if any('rootless' in x for x in options) else -1)"
        for host in self.hosts:
            host['memlock_bytes']=int(subprocess.check_output(self.updater.command(host,probe),text=True,timeout=30).strip())

    def prepare(self):
        prepared=self.folder/'prepared';prepared.mkdir(mode=0o700,exist_ok=True)
        if (prepared/'ready.json').exists():
            return json.loads((prepared/'ready.json').read_text())
        original=json.loads((self.original/'prepared/ready.json').read_text())
        empty=prepared/'empty.public.tar.gz'
        with tarfile.open(empty,'w:gz') as tar:
            for name,data in [('reference.fa',b'>reference\nN\n'),('query.fa',b'>query\nN\n'),('huref.vcf.gz',gzip.compress(b'',mtime=0))]:
                member=tarfile.TarInfo(name);member.size=len(data);tar.addfile(member,io.BytesIO(data))
        empty_private=prepared/'empty.vcf.gz';empty_private.write_bytes(gzip.compress(b'',mtime=0))
        st=empty.stat();md5=digest(empty,'md5')
        sources={**original['source_files'],md5:{'path':str(empty),**{key:getattr(st,key) for key in ('st_size','st_mtime_ns','st_ctime_ns','st_ino','st_dev')}}}
        tiny={'source_md5':md5,'bytes':st.st_size,'sha256':digest(empty),'reference_bases':1,'query_bases':1,'runtime_overhead_mb':128,'cpus':1}
        jobs=[]
        nodes=sorted(self.config['peers'])
        for index,receipt in enumerate(sorted(self.original.glob('chr*.receipt.json'))):
            ident=receipt.name.removesuffix('.receipt.json');result=self.original/(ident+'.result.json')
            private=prepared/('audit-'+ident+'.gz')
            private.write_bytes(gzip.compress(json.dumps({'receipt':json.loads(receipt.read_text()),'result_b64':base64.b64encode(result.read_bytes()).decode()}).encode(),mtime=0))
            spec={**tiny,'method':'receipt-audit','dataset':'comparison','chromosome':json.loads(result.read_text())['chromosome'],'query_start':0,'query_end':1,'private_file':private.name,'private_bytes':private.stat().st_size,'private_sha256':digest(private)}
            job={'id':'audit-'+ident,'phase':'cross-check','spec':spec}
            if index<len(nodes):job['node']=nodes[index]
            jobs.append(job)
        reference=Path('/mnt/PILEDRIVE/genomics-workbench/vep-parallel/run-nv30w0nr/public-reference')
        for original_job in original['jobs']:
            s=original_job['spec'];chrom=s['chromosome']
            private=prepared/(chrom+'.julian.vcf.gz')
            if not private.exists():private.symlink_to(self.original/'prepared'/private.name)
            ref_length=int((reference/(chrom+'.fa.fai')).read_text().split('\t')[1])
            with tarfile.open(self.original/'prepared'/(chrom+'.public.tar.gz')) as tar:
                query_length=sum(len(line.strip()) for line in tar.extractfile('query.fa') if not line.startswith(b'>'))
            for dataset,length in [('julian',ref_length),('huref',query_length)]:
                source=private if dataset=='julian' else empty_private
                for start in range(0,length,40*1024**2):
                    end=min(length,start+40*1024**2)
                    common={**s,'dataset':dataset,'query_start':start,'query_end':end,'reference_bases':ref_length,'query_bases':query_length,'private_file':source.name,'private_bytes':source.stat().st_size,'private_sha256':digest(source),'cpus':2}
                    prefix=dataset+'-chr'+chrom+'-'+str(start)+'-'+str(end)
                    for method in ('normalize','representation','callability'):
                        jobs.append({'id':method+'-'+prefix,'phase':method,'spec':{**common,'method':method},'depends_on':['normalize-'+prefix] if method=='representation' else []})
        for kind in ('sv','cnv'):
            source=next(Path('/mnt/PILEDRIVE/JulianDNA').glob('*.'+kind+'.vcf.gz'))
            private=prepared/(kind+'.vcf.gz')
            if not private.exists():private.symlink_to(source)
            for chrom in [str(n) for n in range(1,23)]+['X','Y']:
                jobs.append({'id':kind+'-scope-chr'+chrom,'phase':'sv-scope','spec':{**tiny,'method':'sv-scope','dataset':'julian-'+kind,'chromosome':chrom,'query_start':0,'query_end':1,'private_file':private.name,'private_bytes':private.stat().st_size,'private_sha256':digest(private)}})
        ready={'jobs':jobs,'source_files':sources,'julian_input_sha256':original['julian_input_sha256']}
        save(prepared/'ready.json',ready)
        return ready

    def publish(self):
        # Reload other completed evidence; never replace the original 86 mapping children.
        plan=json.loads(self.registry.read_text())
        grouped={}
        for job in self.jobs:
            row=self.state['jobs'][job['id']]
            grouped.setdefault(job['phase'],[]).append({'id':job['id'],'name':job['id'],'status':row['status'],'worker':row.get('worker','unassigned'),'detail':row.get('detail',row.get('phase','Native-coordinate evidence; not full common-reference agreement'))})
        gates={
            'comparison-regions':'Requires HuRef complete genotype/reference-confidence evidence and validated common-reference allele projection.',
            'partition':'Requires validated comparable-region masks and allele projection.',
            'snp-compare':'Reported-SNP subset is complete; full comparison requires complete common-reference callsets and callable masks.',
            'reference-agreement':'HuRef heterozygous-only VCF cannot establish homozygous-reference confidence.',
            'phase-compare':'Requires validated common-reference phase evidence; native phase availability is audited with callability.',
            'reconcile':'Requires full comparable-region SNP/indel comparison, reference agreement, phase and structural scope.',
            'reproduce':'Requires a reconciled full comparison for independent reproduction.',
            'private-report':'Requires validated full comparison; partial results remain available in the original aggregate.',
            'aggregate-report':'Requires validated full comparison; no genome-wide agreement inferred from missing calls.',
            'archive':'Final archive waits for validated reports; queue and completed receipts are saved continuously.'}
        def visit(row):
            ident=row['id']
            if ident in grouped:
                children=grouped[ident];done=sum(c['status']=='completed' for c in children)
                status='completed' if done==len(children) else 'running' if any(c['status']=='running' for c in children) else 'failed' if any(c['status']=='failed' for c in children) else 'queued'
                row.update(children=children,status=status,detail=f'{done}/{len(children)} native-coordinate evidence jobs complete')
                if ident=='callability':row.update(status='partial' if done==len(children) else status,detail=row['detail']+'; full HuRef reference-confidence input is missing')
                if ident=='cross-check':row['detail']+='; signed-result integrity and per-site count audit only, not independent biological reproduction'
                return
            if ident in gates:row.update(status='waiting_inputs',detail=gates[ident])
            for child in row.get('children',[]):visit(child)
        visit(plan)
        self.state['workers']=[{'node':h['host'],'memlock_bytes':h.get('memlock_bytes'),
            'image_ready':h['_image_ready'].is_set(),'image_error':h.get('_image_error')} for h in self.hosts]
        plan.update(status='running' if any(r['status']=='running' for r in self.state['jobs'].values()) else 'partial',updated=time.time())
        save(self.registry,plan);save(self.folder/'state.json',self.state)

    def run(self):
        ready=self.prepare();self.jobs=ready['jobs']
        for job in self.jobs:
            row=self.state['jobs'].setdefault(job['id'],{'status':'queued'})
            if row['status'] in ('running','claimed'):row.update(status='queued',detail='Recovered interrupted controller; result receipt will be checked')
            diagnostic=self.folder/(job['id']+'.controller-error.txt')
            if row['status']=='failed' and diagnostic.exists() and transient(RuntimeError(diagnostic.read_text())):
                row.update(status='waiting_capacity',attempts=0,retry_after=0,detail='Infrastructure wait corrected; evidence execution requeued')
        self.publish();self.enroll();self.serving(ready)
        for job in self.jobs:
            row=self.state['jobs'][job['id']]
            if self.reusable(job):row.update(status='completed')
            elif row['status']=='completed':row.update(status='queued',detail='Receipt identity changed; revalidation queued')
        self.state['status']='running';self.publish()
        def worker(host):
            while True:
                with self.lock:
                    for candidate in self.jobs:
                        if any(self.state['jobs'][d]['status'] in ('failed','waiting_dependency') for d in candidate.get('depends_on',[])):
                            self.state['jobs'][candidate['id']].update(status='waiting_dependency',detail='Required evidence job failed validation')
                    job=next((j for j in self.jobs if runnable(j,self.state['jobs'],host['host']) and (host['memlock_bytes']<0 or j['spec']['private_bytes']<=host['memlock_bytes'])),None) if host.get('_retry_after',0)<=time.time() else None
                    attempts=self.state['jobs'][job['id']].get('attempts',0) if job else 0
                    if job:self.state['jobs'][job['id']]['status']='claimed';self.publish()
                    pending=any(r['status'] in ('queued','claimed','running','waiting_capacity','retrying') for r in self.state['jobs'].values())
                if job is None:
                    if not pending:return
                    time.sleep(3);continue
                try:self.execute(host,job)
                except distributed.CapacityWait as error:
                    with self.lock:
                        self.state['jobs'][job['id']].update(status='waiting_capacity',retry_after=time.time()+10,detail=str(error));self.publish()
                except Exception as error:
                    with self.lock:
                        diagnostic=self.folder/(job['id']+'.controller-error.txt')
                        diagnostic.write_text(type(error).__name__+': '+str(error));diagnostic.chmod(0o600)
                        row=self.state['jobs'][job['id']];attempts+=1
                        if transient(error):
                            host['_retry_after']=time.time()+15
                            row.update(status='waiting_capacity',attempts=attempts-1,retry_after=host['_retry_after'],detail='Worker connection unavailable; retrying without charging an execution attempt')
                        else:row.update(status='failed' if attempts>=3 else 'retrying',attempts=attempts,retry_after=time.time()+30,detail='Worker validation/transport failed: '+type(error).__name__)
                        self.publish()
                if host.get('_image_error'):return
        # At least one independent worker per enrolled machine; CPU/RAM admission governs actual work.
        slots=[host for host in self.hosts for _ in range(max(1,int(host.get('cpus',2))))]
        with ThreadPoolExecutor(max_workers=len(slots)) as pool:list(pool.map(worker,slots))
        self.state['status']='partial';self.publish();self.image_pool.shutdown(wait=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original',type=Path,required=True);parser.add_argument('--folder',type=Path,required=True)
    args=parser.parse_args();os.umask(0o077)
    Followup(args.folder,args.original).run()


if __name__=='__main__':main()
