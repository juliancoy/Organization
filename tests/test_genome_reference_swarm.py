import gzip
import json
from pathlib import Path

import pytest
from scripts.genome_reference_giab import compute
from scripts.genome_reference_swarm import aggregate_giab, giab_batches, julian_source


def test_chromosome_partition_and_worker_identity(tmp_path):
    bed=tmp_path/'confidence.bed';bed.write_text('chr1\t0\t100\nchr2\t0\t100\nchrX\t0\t100\n')
    vcf=tmp_path/'benchmark.vcf.gz'
    with gzip.open(vcf,'wt') as stream:
        stream.write('# header\nchr1\t11\t.\tAT\tA\t.\tPASS\t.\nchr2\t21\t.\tA\tG\t.\tPASS\t.\nchrX\t1\t.\tA\tG\t.\tPASS\t.\n')
    batches=list(giab_batches('HG001',bed,vcf,tmp_path))
    assert len(batches)==2
    repeated=list(giab_batches('HG001',bed,vcf,tmp_path))
    assert [b['sha256'] for b in repeated]==[b['sha256'] for b in batches], 'Resume payloads must be deterministic'
    parts=[]
    for batch in batches:
        result=compute(batch['payload'],batch['kind'])
        for key in ('kind','records','sha256'):assert result[key]==batch[key]
        parts.append({**result,'host':'worker1','dataset':batch['dataset']})
    summary=aggregate_giab(parts)['HG001']
    assert summary['same']==197
    assert summary['excluded_variant_span_positions']==3
    assert summary['records']==2
    assert summary['workers']==['worker1']
    assert set(summary['chromosomes'])=={'1','2'}
    assert all((tmp_path/batch['id']).parent==tmp_path for batch in batches)


def test_repeated_chromosome_is_rejected():
    part={'dataset':'HG001','scope':'test','host':'worker','same':99,'benchmark_positions':100,'excluded_variant_span_positions':1,'records':1,'variant_records':1,'chromosomes':{'1':{'same':99}}}
    with pytest.raises(ValueError,match='Repeated chromosome'):
        aggregate_giab([part,part])


def test_prepared_normalization_span_is_excluded(tmp_path):
    bed=tmp_path/'confidence.bed';bed.write_text('1\t0\t100\n')
    official=tmp_path/'official.vcf.gz'
    prepared=tmp_path/'prepared.vcf.gz'
    with gzip.open(official,'wt') as stream:stream.write('1\t11\t.\tAT\tA\t.\tPASS\t.\n')
    with gzip.open(prepared,'wt') as stream:stream.write('1\t9\t.\tA\tAT\t.\tPASS\t.\n')
    batch=list(giab_batches('HG001',bed,official,tmp_path,prepared))[0]
    result=compute(batch['payload'],batch['kind'])
    assert result['records']==batch['records']==1
    assert result['prepared_records']==batch['prepared_records']==1
    assert result['same']==97
    summary=aggregate_giab([{**result,'host':'worker','dataset':'HG001'}])['HG001']
    assert summary['same']==97
    assert summary['prepared_records']==1


def test_julian_source_requires_explicit_or_private_resume(tmp_path):
    with pytest.raises(ValueError,match='Provide --julian'):
        julian_source(None,tmp_path)
    source=tmp_path/'private-source.g.vcf.gz';source.write_bytes(b'fixture')
    assert julian_source(source,tmp_path)==source.resolve()
    (tmp_path/'julian').mkdir()
    (tmp_path/'julian/state.json').write_text(json.dumps({'sources':{'Julian.qc':{'path':str(source)}}}))
    assert julian_source(None,tmp_path)==source.resolve()
