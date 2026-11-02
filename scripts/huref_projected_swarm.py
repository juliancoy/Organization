"""Run supported common-reference sequence-allele comparisons without all-or-nothing input gates."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import time
from scripts.huref_compare_swarm import ROOT, save, digest
from scripts.huref_followup_swarm import Followup


class ProjectedComparison(Followup):
    def __init__(self,folder,original):
        super().__init__(folder,original)
        self.worker_source=(ROOT/'scripts/huref_compare_worker.py').read_text().split("if __name__=='__main__':")[0]+'\n'+(ROOT/'scripts/huref_evidence.py').read_text()+'\n'+(ROOT/'scripts/huref_projected_worker.py').read_text()

    def compatible_worker(self):
        """Reuse a previous algorithm only under an explicit signed migration."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature
        try:
            value=self.w.verified(self.w.load(self.folder/'worker-compatibility.json'),Ed25519PublicKey.from_public_bytes(bytes.fromhex(self.config['controller_public_key'])))
            if (value.get('schema')=='huref-worker-compatibility.v1' and value.get('run')==self.state['id']
                and value.get('to_worker_sha256')==hashlib.sha256(self.worker_source.encode()).hexdigest()
                and value.get('from_worker_sha256')==digest(self.folder/'previous-worker.py')):
                return value['from_worker_sha256']
        except (OSError,ValueError,KeyError,InvalidSignature):pass
        return None

    def reusable(self,job):
        if super().reusable(job):return True
        previous=self.compatible_worker()
        return bool(previous and super().reusable(job,worker_sha256=previous))

    def prepare(self):
        prepared=self.folder/'prepared';prepared.mkdir(mode=0o700,exist_ok=True)
        original=json.loads((self.original/'prepared/ready.json').read_text())
        jobs=[]
        native=json.loads((self.original/'followup-20261101/prepared/ready.json').read_text())
        originals={job['spec']['chromosome']:job['spec'] for job in original['jobs']}
        for source in native['jobs']:
            if source['spec'].get('method')!='normalize' or source['spec'].get('dataset')!='huref':continue
            spec={**source['spec'],'method':'projected-variants','dataset':'huref-vs-julian','cpus':2}
            spec.update({key:originals[spec['chromosome']][key] for key in ('private_bytes','private_sha256')})
            spec['private_file']=spec['chromosome']+'.julian.vcf.gz'
            private=prepared/(spec['chromosome']+'.julian.vcf.gz')
            if not private.exists():private.symlink_to(self.original/'prepared'/private.name)
            jobs.append({'id':'projected-'+source['id'],'phase':'projected-variants','spec':spec})
        ready={**original,'jobs':jobs};save(prepared/'ready.json',ready);return ready

    def publish(self):
        plan=json.loads(self.registry.read_text());rows=[]
        for job in self.jobs:
            state=self.state['jobs'][job['id']]
            rows.append({'id':job['id'],'name':'Common-reference SNP, MNV and indel comparison — '+job['spec']['chromosome']+':'+str(job['spec']['query_start'])+'–'+str(job['spec']['query_end']),
                'status':state['status'],'worker':state.get('worker','unassigned'),
                'detail':state.get('phase',state.get('detail','Public HuRef input from Latitude; private Julian input through authenticated RAM transport')),
                'attempt_started':state.get('started'),'elapsed_seconds':max(0,state.get('finished',time.time())-state['started']) if state.get('started') else None})
        done=sum(r['status']=='completed' for r in rows);complete=done==len(rows) and bool(rows)
        running=any(r['status']=='running' for r in rows)
        failed=any(r['status']=='failed' for r in rows)
        status='completed' if complete else 'running' if running else 'failed' if failed else 'queued'
        self.state['status']='partial' if complete else status
        def visit(row):
            ident=row['id']
            if ident=='snp-compare':
                row.update(status=status,children=rows,detail=f'{done}/{len(rows)} bounded common-reference reported-sequence-allele comparisons verified; SNPs, MNVs and indels included. Missing HuRef calls remain unknown.')
                return
            if ident=='partition':row.update(status='completed',detail=f'{len(rows)} bounded chromosome regions scheduled from checksum-pinned HuRef and Julian inputs; every authorized node is eligible subject to available CPU, RAM and private-input pinning.')
            if ident in ('reconcile','private-report','aggregate-report','archive'):
                row.update(status='completed' if self.state.get('report_ready') else 'queued',detail='Validated scoped report and provenance archive saved' if self.state.get('report_ready') else 'Scheduled after reported-sequence-allele comparisons finish; full-genome confidence is not a prerequisite for publishing scoped results')
            if ident=='comparison-regions':row.update(status='partial',detail='Explicit reported-allele footprints are projected and validated in runnable shards. Whole-genome diploid callable intersection remains unavailable from the heterozygous-only HuRef VCF.')
            for child in row.get('children',[]):visit(child)
        visit(plan);plan.update(status='partial' if complete else status,updated=time.time())
        self.state['workers']=[{'node':h['host'],'memlock_bytes':h.get('memlock_bytes'),'image_ready':h['_image_ready'].is_set(),'image_error':h.get('_image_error')} for h in self.hosts]
        save(self.registry,plan);save(self.folder/'state.json',self.state)

    def report(self):
        # Exclude every target repeated across shards, regardless of agreement.
        counts=Counter();sites={};duplicates=set();receipts=[];assembly_sites={};assembly_duplicates=set()
        for job in self.jobs:
            ident=job['id']
            if self.state['jobs'][ident]['status']!='completed':continue
            if not self.reusable(job):raise ValueError('Report receipt validation failed')
            path=self.folder/(ident+'.result.json');value=json.loads(path.read_text())
            counts.update({k:v for k,v in value['counts'].items() if not k.endswith(('_agree','_differ','_unknown'))})
            for key,evidence in value['site_evidence'].items():
                if key in sites:duplicates.add(key)
                else:sites[key]=evidence
            for key,label in value.get('assembly_comparison',{}).get('site_evidence',{}).items():
                if key in assembly_sites:assembly_duplicates.add(key)
                else:assembly_sites[key]=label
            receipts.append({'job':ident,'result_sha256':digest(path),'receipt_sha256':digest(self.folder/(ident+'.receipt.json'))})
        for key,evidence in sites.items():
            if key not in duplicates:counts[evidence['kind']+'_'+evidence['label']]+=1
        counts['cross_shard_ambiguous_targets']=len(duplicates)
        for key,label in assembly_sites.items():
            if key not in assembly_duplicates:counts['assembly_'+label]+=1
        counts['assembly_cross_shard_ambiguous_targets']=len(assembly_duplicates)
        limitations=['Assembly allele comparisons and reported diploid variant comparisons are separate evidence. Absent VCF records do not invalidate assembly evidence or establish a second allele.',
            'Intrachromosomal primary MAPQ60 mappings do not establish genome-wide uniqueness.',
            'Noncontiguous or REF-mismatching allele projections are excluded; complex rearrangements and cross-sample phase remain unassessed.']
        report={'schema':'huref-projected-comparison.v1','generated':time.time(),'counts':dict(counts),'completed_shards':len(receipts),'total_shards':len(self.jobs),'limitations':limitations,'receipts':receipts,
            'public_source':'julian-latitude.local','private_source':'compai.local','scope':'Assembly allele membership at confident Julian SNV sites, plus explicitly reported common-reference-normalized diploid sequence alleles; not a genome-wide similarity estimate.'}
        save(self.folder/'owner-report.json',report)
        save(self.folder/'public-summary.json',{'generated':report['generated'],'completed_shards':len(receipts),'total_shards':len(self.jobs),'scope':report['scope'],'limitations':limitations})
        archive={'report_sha256':digest(self.folder/'owner-report.json'),'public_summary_sha256':digest(self.folder/'public-summary.json'),'receipts':receipts,'time':time.time()}
        archive['validation_policies']={name:digest(self.folder/name) for name in ('approved-execution.json','worker-compatibility.json','previous-worker.py') if (self.folder/name).exists()}
        save(self.folder/'archive.receipt.json',__import__('scripts.run_manager',fromlist=['sign']).sign(archive,self.w))
        self.state['report_ready']=len(receipts)==len(self.jobs);self.publish()

    def run(self):
        revision=hashlib.sha256(self.worker_source.encode()).hexdigest()
        if self.state.get('execution_worker_sha256')!=revision and self.compatible_worker():
            for row in self.state['jobs'].values():
                if row['status']=='failed':row.update(status='queued',attempts=0,retry_after=0,detail='Requeued after validated ambiguous-reference normalization fix')
        self.state['execution_worker_sha256']=revision
        super().run()
        if all(row['status']=='completed' for row in self.state['jobs'].values()):self.report()
        elif any(row['status']=='failed' for row in self.state['jobs'].values()):raise RuntimeError('Projected comparison shards failed validation')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--original',type=Path,required=True);parser.add_argument('--folder',type=Path,required=True)
    args=parser.parse_args();os.umask(0o077);ProjectedComparison(args.folder,args.original).run()


if __name__=='__main__':main()
