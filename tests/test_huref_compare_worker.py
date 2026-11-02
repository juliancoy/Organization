import gzip
import hashlib
import io
import json
from pathlib import Path
import random
import subprocess
import tarfile
import pytest
from scripts.huref_compare_worker import genotype,map_snps,receive


def row(gt='0/1',extra='DP:GQ',quality='30:40'):
    return ['1','5','.','A','C,<NON_REF>','.','PASS','.','GT:'+extra,gt+':'+quality]


def test_quality_missing_nan_and_reference_symbols_not_inferred():
    assert genotype(row(),True)==['A','C']
    for gt in ['0/.','./.','0/2','0']:
        assert genotype(row(gt),True) is None
    assert genotype(row(quality='30:nan'),True) is None
    assert genotype(row(quality='.:40'),True) is None
    assert genotype(row(quality='10:40'),True) is None
    block=row(gt='0/0');block[7]='END=100';assert genotype(block,True) is None
    block[8]='GT:MIN_DP:GQ';assert genotype(block,True)==['A','A']


def test_mapping_rejects_duplicate_and_handles_reverse_and_gap():
    query=b'ACGTACGTAA';ref=query
    paf='q\t10\t0\t10\t+\tr\t10\t0\t10\t10\t10\t60\ttp:A:P\tcg:Z:10='
    mapped,_=map_snps([paf],{2:[],8:[]},query,ref);assert mapped=={2:(2,1),8:(8,1)}
    assert map_snps([paf,paf],{2:[]},query,ref)[0]=={}
    reverse=paf.replace('\t+\t','\t-\t')
    assert map_snps([reverse],{2:[]},query,ref)[0]=={2:(7,-1)}
    with pytest.raises(ValueError):map_snps([paf.replace('10=','9=')],{2:[]},query,ref)


def test_input_hash_required(tmp_path):
    with pytest.raises(ValueError):receive(io.BytesIO(b'wrong'),tmp_path/'in',5,'0'*64)


@pytest.mark.parametrize('query_start',[0,1000])
def test_real_minimap_container_fixture(query_start):
    random.seed(7);sequence=''.join(random.choices('ACGT',k=50000));position=5001;ref=sequence[position-1];alt=next(a for a in 'ACGT' if a!=ref)
    line=f'1\t{position}\t.\t{ref}\t{alt}\t.\tPASS\t.\tGT:DP:GQ\t0/1:30:40\n'
    variants=gzip.compress(line.encode());private=gzip.compress(line.encode());bundle=io.BytesIO()
    with tarfile.open(fileobj=bundle,mode='w:gz') as tar:
        for name,data in [('reference.fa',('>1\n'+sequence+'\n').encode()),('query.fa',('>query\n'+sequence+'\n').encode()),('huref.vcf.gz',variants)]:
            info=tarfile.TarInfo(name);info.size=len(data);tar.addfile(info,io.BytesIO(data))
    public=bundle.getvalue();header={'public_bytes':len(public),'public_sha256':hashlib.sha256(public).hexdigest(),'private_bytes':len(private),
        'private_sha256':hashlib.sha256(private).hexdigest(),'chromosome':'1','cpus':2,'query_start':query_start,'query_end':40000 if query_start else len(sequence)}
    command=['docker','run','--rm','--network','none','--read-only','--memory-reservation','512m','--cpus','2','--ulimit','memlock=-1:-1','--tmpfs','/work:rw,nosuid,nodev,size=128m','-i','lifetech/genome-compare:20261031','python3','-c',Path('scripts/huref_compare_worker.py').read_text()]
    result=subprocess.run(command,input=(json.dumps(header)+'\n').encode()+public+private,capture_output=True,timeout=180)
    assert result.returncode==0,result.stderr.decode()
    counts=json.loads(result.stdout);assert counts['genotype_agree']==1 and counts['mapped_snp_candidates']==1


def test_tile_offsets_and_bounded_query_files(tmp_path):
    from scripts.huref_compare_worker import tile_query
    source=tmp_path/'query.fa';dest=tmp_path/'tiles.fa';source.write_bytes(b'>original\nACGTAC\nGTAA\n')
    tile_query(source,dest,4)
    assert dest.read_bytes()==b'>tile_0\nACGT\n>tile_4\nACGT\n>tile_8\nAA\n'
    paf='tile_10\t10\t0\t10\t+\tr\t20\t0\t10\t10\t10\t60\ttp:A:P\tcg:Z:10='
    assert map_snps([paf],{12:[]},b'A'*20,b'A'*20)[0]=={12:(2,1)}
    assert map_snps([paf.replace('\t+\t','\t-\t')],{12:[]},b'A'*20,b'A'*20)[0]=={12:(7,-1)}
