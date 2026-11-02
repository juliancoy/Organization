"""Synthetic biological edge cases in the same Docker tools used by workers."""
import json
import subprocess
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]


def compare(huref,julian,strand='+'):
    worker=(ROOT/'scripts/huref_compare_worker.py').read_text().split("if __name__=='__main__':")[0]
    worker+='\n'+(ROOT/'scripts/huref_evidence.py').read_text()
    worker+='\n'+(ROOT/'scripts/huref_projected_worker.py').read_text().split("if __name__=='__main__':")[0]
    setup="""
fixture=json.loads(sys.stdin.read());work=Path('/work');sequence='ACGT'*20
for name in ('reference.fa','query.fa'):(work/name).write_text('>reference\\n'+sequence+'\\n')
for name,key in [('huref.vcf.gz','h'),('julian.vcf.gz','j')]:
 (work/name).write_bytes(gzip.compress(('\\n'.join(fixture[key])+'\\n').encode()))
(work/'alignment.paf').write_text('query\\t80\\t0\\t80\\t'+fixture['strand']+'\\treference\\t80\\t0\\t80\\t80\\t80\\t60\\ttp:A:P\\tcg:Z:80=\\n')
print(json.dumps(projected_compare(work,{'query_start':0,'query_end':80,'public_sha256':'fixture'})))
"""
    result=subprocess.run(['docker','run','--rm','--network','none','--read-only','--tmpfs','/work:rw,size=32m','-i','lifetech/genome-compare:20261031','python3','-c',worker+'\n'+setup],input=json.dumps({'h':huref,'j':julian,'strand':strand}).encode(),capture_output=True,timeout=180)
    assert result.returncode==0,result.stderr.decode()
    return json.loads(result.stdout)


def row(pos,ref,alt,gt='0/1',quality=True,info='.'):
    return '\t'.join(['1',str(pos),'.',ref,alt,'.','PASS',info,'GT:DP:GQ' if quality else 'GT',gt+':30:40' if quality else gt])


def test_indel_and_mnv_genotypes_compared_on_validated_common_reference():
    result=compare([row(2,'C','CA',quality=False),row(9,'AC','TT',quality=False)], [row(2,'C','CA'),row(9,'AC','TT')])
    assert result['counts']['indel_agree']==1
    assert result['counts']['MNV_agree']==1


def test_missing_and_low_quality_calls_stay_unknown():
    result=compare([row(2,'C','CA',quality=False),row(5,'A','T',quality=False)], [row(2,'C','CA',quality=False)])
    assert result['counts']['indel_unknown']==1
    assert result['counts']['SNV_unknown']==1


def test_reference_block_supports_difference_without_missing_as_reference():
    result=compare([row(2,'C','CA',quality=False)], ['1\t1\t.\tA\t<NON_REF>\t.\tPASS\tEND=80;MinDP=30\tGT:GQ\t0/0:40'])
    assert result['counts']['indel_differ']==1


def test_reverse_complemented_indel_genotype():
    # Query position 2 maps to target 79, whose reference base is G.
    result=compare([row(2,'C','CA',quality=False)], [row(79,'G','TG')],strand='-')
    assert result['counts']['indel_agree']==1


def test_reference_span_crossing_alignment_gap_is_not_contiguous():
    from scripts.huref_compare_worker import map_snps
    paf=['query\t80\t0\t80\t+\treference\t80\t0\t80\t79\t81\t60\ttp:A:P\tcg:Z:2=1I1D77=']
    mapped,_=map_snps(paf,{1:True,2:True,3:True},bytearray(b'A'*80),bytearray(b'A'*80))
    assert 1 in mapped and 2 not in mapped and 3 in mapped


def test_scoped_report_excludes_cross_shard_ambiguous_targets(tmp_path,monkeypatch):
    from scripts.huref_projected_swarm import ProjectedComparison
    from scripts import run_manager
    c=ProjectedComparison.__new__(ProjectedComparison)
    c.folder=tmp_path;c.jobs=[{'id':'a'},{'id':'b'}]
    c.state={'jobs':{'a':{'status':'completed'},'b':{'status':'completed'}}}
    c.reusable=lambda job:True;c.publish=lambda:None;c.w=None
    monkeypatch.setattr(run_manager,'sign',lambda value,w:{'value':value,'signature':'fixture'})
    for ident,label in [('a','agree'),('b','differ')]:
        (tmp_path/(ident+'.result.json')).write_text(json.dumps({'counts':{'indel_'+label:1},'site_evidence':{'collision':{'kind':'indel','label':label}},'assembly_comparison':{'site_evidence':{'assembly-collision':label}}}))
        (tmp_path/(ident+'.receipt.json')).write_text('{}')
    c.report()
    owner=json.loads((tmp_path/'owner-report.json').read_text())
    assert owner['counts']['cross_shard_ambiguous_targets']==1
    assert owner['counts']['assembly_cross_shard_ambiguous_targets']==1
    assert owner['counts'].get('assembly_agree',0)==owner['counts'].get('assembly_differ',0)==0
    assert owner['counts'].get('indel_agree',0)==owner['counts'].get('indel_differ',0)==0
    public=json.loads((tmp_path/'public-summary.json').read_text())
    assert 'counts' not in public and 'receipts' not in public
    assert c.state['report_ready'] is True


def test_worker_uses_assembly_even_when_huref_vcf_is_empty():
    result=compare([], [row(2,'C','T'),row(5,'A','C',gt='1/1')])
    assert result['assembly_comparison']['counts']=={'supported':1,'different':1}
    assert result['site_evidence']=={}


def test_left_normalized_unknown_reference_anchor_is_excluded_explicitly():
    worker=(ROOT/'scripts/huref_compare_worker.py').read_text().split("if __name__=='__main__':")[0]
    worker+='\n'+(ROOT/'scripts/huref_projected_worker.py').read_text().split("if __name__=='__main__':")[0]
    fixture="""
work=Path('/work');excluded=Counter()
result=normalized_rows(work,bytearray(b'NAAAACGT'),[('h0',2,'A',['A','AA'])],excluded)
print(json.dumps({'rows':result,'excluded':dict(excluded)}))
"""
    result=subprocess.run(['docker','run','--rm','--network','none','--read-only','--tmpfs','/work:rw,size=32m','-i','lifetech/genome-compare:20261031','python3','-c',worker+'\n'+fixture],capture_output=True,timeout=180)
    assert result.returncode==0,result.stderr.decode()
    value=json.loads(result.stdout)
    assert value['rows']=={}
    assert value['excluded']=={'excluded_ambiguous_normalized_sequence':1}


def test_compatibility_requires_signed_run_and_both_exact_worker_versions(tmp_path):
    import base64,hashlib
    from types import SimpleNamespace
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
    from scripts.venter_peer_qc import verified
    from scripts.huref_projected_swarm import ProjectedComparison
    key=Ed25519PrivateKey.generate();old=b'previous algorithm';new='fixed algorithm'
    c=ProjectedComparison.__new__(ProjectedComparison);c.folder=tmp_path;c.worker_source=new;c.state={'id':'run'}
    c.config={'controller_public_key':key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex()}
    c.w=SimpleNamespace(load=lambda p:json.loads(p.read_text()),verified=verified)
    (tmp_path/'previous-worker.py').write_bytes(old)
    value={'schema':'huref-worker-compatibility.v1','run':'run','from_worker_sha256':hashlib.sha256(old).hexdigest(),'to_worker_sha256':hashlib.sha256(new.encode()).hexdigest()}
    def write(v):
        raw=json.dumps(v,sort_keys=True,separators=(',',':')).encode()
        (tmp_path/'worker-compatibility.json').write_text(json.dumps({'value':v,'signature':base64.b64encode(key.sign(raw)).decode()}))
    write(value);assert c.compatible_worker()==value['from_worker_sha256']
    c.state['id']='different-run';assert c.compatible_worker() is None
    c.state['id']='run';c.worker_source='unapproved-new-code';assert c.compatible_worker() is None
    c.worker_source=new;(tmp_path/'previous-worker.py').write_bytes(b'changed');assert c.compatible_worker() is None
    (tmp_path/'previous-worker.py').write_bytes(old);write(value)
    envelope=json.loads((tmp_path/'worker-compatibility.json').read_text());envelope['value']['run']='forged'
    (tmp_path/'worker-compatibility.json').write_text(json.dumps(envelope));assert c.compatible_worker() is None
