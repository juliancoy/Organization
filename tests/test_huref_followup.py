import base64
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import threading
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from scripts.huref_followup_swarm import runnable, worker_source, transient
from scripts.huref_compare_swarm import Comparison, cpu_reservation


def test_fractional_capacity_uses_integral_cpu_admission():
    assert type(cpu_reservation({}, {'available_cpus':1.0})) is int
    assert cpu_reservation({}, {'available_cpus':1.8})==1
    assert cpu_reservation({}, {'available_cpus':0.8})==0
    assert cpu_reservation({'method':'normalize','cpus':2},{'available_cpus':8})==1
    assert cpu_reservation({'method':'compare','cpus':4},{'available_cpus':8})==4


def test_transport_outages_do_not_consume_scientific_attempts():
    assert transient(ConnectionRefusedError('offline'))
    assert transient(RuntimeError('Agent is applying an explicit update'))
    assert transient(RuntimeError('Owner maintenance: draining existing work; renewals remain available'))
    assert not transient(ValueError('Result identity mismatch'))


def test_resume_accepts_verified_engine_specific_image_identity(tmp_path):
    c=Comparison.__new__(Comparison);c.folder=tmp_path;c.config={'controller_public_key':'00'*32}
    c.state={'id':'run'};c.auth={'comparison_worker_sha256':'code','comparison_image_ids':{'node':'controller-image'}}
    gate=threading.Event();gate.set()
    c.hosts=[{'host':'node','compare_image':'verified-native-image','_image_ready':gate}]
    result=tmp_path/'job.result.json';result.write_text('{}')
    value={'run':'run','job':'job','worker':'node','worker_sha256':'code','image':'verified-native-image',
           'public_sha256':'public','private_sha256':'private','query_start':0,'query_end':1,
           'output_sha256':hashlib.sha256(result.read_bytes()).hexdigest()}
    c.w=SimpleNamespace(load=lambda path:None,verified=lambda envelope,key:value)
    job={'id':'job','spec':{'sha256':'public','private_sha256':'private','query_start':0,'query_end':1}}
    assert c.reusable(job)
    value['image']='unapproved'
    assert not c.reusable(job)


def run_recipe(method,private,dataset='julian',public_key=None):
    bundle=io.BytesIO()
    with tarfile.open(fileobj=bundle,mode='w:gz') as tar:
        for name,data in [('reference.fa',b'>ref\nACGTACGT\n'),('query.fa',b'>query\nACGTACGT\n'),('huref.vcf.gz',private)]:
            member=tarfile.TarInfo(name);member.size=len(data);tar.addfile(member,io.BytesIO(data))
    public=bundle.getvalue()
    header={'method':method,'dataset':dataset,'chromosome':'1','query_start':0,'query_end':8,'cpus':1,
            'public_bytes':len(public),'public_sha256':hashlib.sha256(public).hexdigest(),
            'private_bytes':len(private),'private_sha256':hashlib.sha256(private).hexdigest(),
            'job_spec_sha256':'fixture','controller_public_key':public_key}
    command=['docker','run','--rm','--network','none','--read-only','--memory-reservation','128m','--cpus','1',
             '--ulimit','memlock=-1:-1','--tmpfs','/work:rw,nosuid,nodev,size=128m','-i',
             'lifetech/genome-compare:20261031','python3','-c',worker_source()]
    return subprocess.run(command,input=(json.dumps(header)+'\n').encode()+public+private,capture_output=True,timeout=180)


def test_dependency_and_machine_affinity():
    job={'id':'b','node':'latitude','depends_on':['a']}
    states={'a':{'status':'running'},'b':{'status':'queued'}}
    assert not runnable(job,states,'latitude')
    states['a']['status']='completed'
    assert runnable(job,states,'latitude')
    assert not runnable(job,states,'compai')
    states['b']['status']='claimed'
    assert not runnable(job,states,'latitude')


@pytest.mark.parametrize('method',['normalize','representation'])
def test_native_normalization_preserves_variant_with_nonref_and_filter(method):
    private=gzip.compress(b'1\t2\t.\tC\tT,<NON_REF>\t.\tLowQual\t.\tGT\t0/1\n')
    completed=run_recipe(method,private)
    assert completed.returncode==0,completed.stderr.decode()
    result=json.loads(completed.stdout)
    assert result['idempotent'] and result['counts']['haplotype_equivalence_checks']==1
    if method=='normalize':
        body=gzip.decompress(base64.b64decode(result['normalized_vcf_gz_b64'])).decode()
        assert '\tC\tT\t.\tLowQual\t' in body
        assert '<NON_REF>' not in body


def test_missing_quality_and_heterozygous_only_do_not_create_reference_confidence():
    private=gzip.compress(b'1\t2\t.\tC\tT\t.\tPASS\t.\tGT\t0/1\n')
    completed=run_recipe('callability',private,dataset='huref')
    assert completed.returncode==0,completed.stderr.decode()
    result=json.loads(completed.stdout)
    assert result['supported_reference_bases']==0
    assert result['reference_confidence_intervals']==[]
    assert result['full_reference_confidence_available'] is False


def test_signed_receipt_audit_detects_tampered_result():
    key=Ed25519PrivateKey.generate()
    public=key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex()
    raw=json.dumps({'genotype_agree':1,'site_evidence':{'hashed-site':'genotype_agree'},'query_start':0,'query_end':8,'public_sha256':'public','private_sha256':'private'}).encode()
    value={'output_sha256':hashlib.sha256(raw).hexdigest(),'query_start':0,'query_end':8,'public_sha256':'public','private_sha256':'private','job':'original'}
    signature=key.sign(json.dumps(value,sort_keys=True,separators=(',',':')).encode())
    def payload(result):return gzip.compress(json.dumps({'receipt':{'value':value,'signature':base64.b64encode(signature).decode()},'result_b64':base64.b64encode(result).decode()}).encode())
    completed=run_recipe('receipt-audit',payload(raw),public_key=public)
    assert completed.returncode==0,completed.stderr.decode()
    assert json.loads(completed.stdout)['signature_verified']
    assert run_recipe('receipt-audit',payload(raw+b' '),public_key=public).returncode!=0
