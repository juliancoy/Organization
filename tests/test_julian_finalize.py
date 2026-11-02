"""Biological and crash-recovery checks for bounded streaming finalization."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import pytest
from scripts.julian_finalize import plan,prepare_command,final_command,prepare_batch,SCHEMA

IMAGE='lifetech/genome-compare:20261031'


def test_streaming_space_plan_fits_the_measured_disk_and_bounds_all_ram():
    gb=10**9
    p=plan(32000,114*gb,150*10**6,215*gb)
    assert p['required_free_bytes']<215*gb
    assert p['ram_scratch_mb']+2*p['sort_memory_per_thread_mb']+3072<=p['memory_mb']
    assert p['memory_mb']<=32000-2048
    assert not p['swap_allowed']
    with pytest.raises(RuntimeError,match='output space'):plan(32000,114*gb,150*10**6,100*gb)
    with pytest.raises(RuntimeError,match='8 GiB'):plan(4000,gb,10,10*gb)


def test_final_pipeline_has_global_duplicate_marking_and_no_global_resort():
    cmd=final_command()
    assert 'samtools merge -u' in cmd and '| samtools markdup' in cmd
    assert 'samtools collate' not in cmd and 'samtools sort' not in cmd
    assert '/ram/markdup' in cmd


@pytest.mark.skipif(shutil.which('docker') is None,reason='Genome-tool integration requires host Docker; portable planning and recovery checks run without it')
def test_actual_tools_preserve_pairs_and_mark_duplicates_between_batches(tmp_path):
    bams=tmp_path/'bams';bams.mkdir();final=tmp_path/'final';final.mkdir();ref=tmp_path/'reference';ref.mkdir()
    (ref/'reference.fa').write_text('>chr1\n'+'ACGT'*250+'\n')
    # Two source batches have distinct read names/RGs but identical fragment
    # coordinates. Duplicate marking must happen AFTER their sorted merge.
    for n in range(2):
        header='@HD\tVN:1.6\tSO:coordinate\n@SQ\tSN:chr1\tLN:1000\n@RG\tID:batch'+str(n)+'\tSM:Julian\tLB:one-library\n'
        body=''
        for flag,pos,mate,tlen in [(99,101,201,150),(147,201,101,-150)]:
            body+='\t'.join(['pair'+str(n),str(flag),'chr1',str(pos),'60','50M','=',str(mate),str(tlen),'A'*50,'I'*50,'RG:Z:batch'+str(n)])+'\n'
        (bams/(f'{n:06d}.sam')).write_text(header+body)
    config={'sort_memory_per_thread_mb':64}
    commands=['samtools faidx /ref/reference.fa']
    for n in range(2):
        ident=f'{n:06d}'
        commands+=['samtools view -b -o /run/bams/'+ident+'.bam /run/bams/'+ident+'.sam',prepare_command(ident,config),'mv /work/'+ident+'.prepared.partial.bam /run/bams/'+ident+'.bam']
    (final/'bams.list').write_text('/run/bams/000000.bam\n/run/bams/000001.bam\n')
    commands += [final_command()]
    result=subprocess.run(['docker','run','--rm','--network','none','--read-only','--memory','768m','--memory-swap','768m','--tmpfs','/ram:rw,size=128m',
        '--mount',f'type=bind,src={tmp_path},dst=/run','--mount',f'type=bind,src={final},dst=/work','--mount',f'type=bind,src={ref},dst=/ref',IMAGE,'bash','-o','pipefail','-c',' && '.join(commands)],capture_output=True,timeout=45)
    assert result.returncode==0,result.stderr.decode()
    stats=json.loads((final/'flagstat.json').read_text())['QC-passed reads']
    assert stats['primary']==4
    assert stats['duplicates']==2
    assert (final/'Julian-HuRef2.partial.bam.bai').exists()
    assert list((tmp_path/'bams').glob('*.bam'))


def test_recover_after_replacement_preserves_provenance_and_cleans_only_old_alias(tmp_path):
    root=tmp_path;(root/'bams').mkdir();(root/'alignments/000000').mkdir(parents=True)
    source=root/'bams/000000.bam';source.write_bytes(b'original')
    alias=root/'alignments/000000/alignment.bam';os.link(source,alias)
    old={'path':str(source),'bytes':8,'sha256':hashlib.sha256(b'original').hexdigest(),'device':source.stat().st_dev,'inode':source.stat().st_ino}
    private_reads=root/'input.fq';private_reads.write_bytes(b'original reads retained')
    partial=root/'replacement';partial.write_bytes(b'prepared');partial.replace(source)
    folder=root/'final/mate-prepared';folder.mkdir(parents=True)
    new={'bytes':8,'sha256':hashlib.sha256(b'prepared').hexdigest()}
    (folder/'000000.json').write_text(json.dumps({'schema':SCHEMA,'job':'000000','pairs':1,'original':old,'prepared':new}))
    job={'id':'000000','pairs':1,'receipt':old};saved=[]
    def forbidden(*a,**kw):raise AssertionError('Recovered batch must not execute tools again')
    prepare_batch(root,job,{},IMAGE,{},lambda:saved.append(True),run=forbidden)
    assert job['receipt']['sha256']==new['sha256']
    assert job['mate_preparation']['original']['sha256']==old['sha256']
    assert not alias.exists() and private_reads.read_bytes()==b'original reads retained'
    prepare_batch(root,job,{},IMAGE,{},lambda:None,run=forbidden)
    assert source.read_bytes()==b'prepared'


def test_bad_checkpoint_cannot_retire_the_original_batch(tmp_path):
    (tmp_path/'bams').mkdir();source=tmp_path/'bams/000000.bam';source.write_bytes(b'original')
    job={'id':'000000','pairs':1,'receipt':{'bytes':8,'sha256':hashlib.sha256(b'original').hexdigest()}}
    def failed_stage(*a,**kw):raise RuntimeError('synthetic interrupted preparation')
    with pytest.raises(RuntimeError,match='interrupted'):
        prepare_batch(tmp_path,job,{},IMAGE,{'cpus':1,'memory_mb':8192,'sort_memory_per_thread_mb':512,'ram_scratch_mb':1024},lambda:None,run=failed_stage)
    assert source.read_bytes()==b'original'


def test_completed_bam_publication_resumes_without_remerging(tmp_path,monkeypatch):
    from scripts import julian_finalize as module
    final=tmp_path/'final';final.mkdir()
    data=b'verified-final-bam';partial=final/'Julian-HuRef2.partial.bam';partial.write_bytes(data)
    (final/'Julian-HuRef2.partial.bam.bai').write_bytes(b'verified-index')
    record={'bam':{'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()},'primary_pair_accounting_verified':True}
    (final/'completion-pending.json').write_text(json.dumps(record))
    monkeypatch.setattr(module,'leased_run',lambda *a,**kw:pytest.fail('Verified final output should not be merged again'))
    module.finalize(tmp_path,[],{}, {},tmp_path,lambda:None,10000)
    assert (final/'Julian-HuRef2.bam').read_bytes()==data
    assert (final/'Julian-HuRef2.bam.bai').read_bytes()==b'verified-index'
    assert json.loads((final/'complete.json').read_text())['bam']['path']==str(final/'Julian-HuRef2.bam')


def test_nonprimary_record_loss_is_rejected():
    from scripts.julian_finalize import check_counts
    original={'QC-passed reads':{'primary':2,'total':3,'secondary':1,'supplementary':0}}
    missing={'QC-passed reads':{'primary':2,'total':2,'secondary':0,'supplementary':0}}
    with pytest.raises(ValueError,match='record accounting'):check_counts(missing,1,original)


def test_resume_prefers_the_verified_pending_bam_over_a_stale_final(tmp_path):
    from scripts.julian_finalize import finalize
    final=tmp_path/'final';final.mkdir()
    (final/'Julian-HuRef2.bam').write_bytes(b'stale')
    (final/'Julian-HuRef2.partial.bam').write_bytes(b'new verified')
    (final/'Julian-HuRef2.partial.bam.bai').write_bytes(b'new index')
    (final/'completion-pending.json').write_text(json.dumps({'bam':{'bytes':12,'sha256':hashlib.sha256(b'new verified').hexdigest()}}))
    finalize(tmp_path,[],{}, {},tmp_path,lambda:None,10000)
    assert (final/'Julian-HuRef2.bam').read_bytes()==b'new verified'
