import gzip
from scripts.genome_composition import composition


def test_reference_denominator_and_disjoint_starts(tmp_path):
    source=tmp_path/'variants.vcf.gz'
    rows=['##contig=<ID=1,length=100>', '#CHROM\tPOS\tID\tREF\tALT',
          '1\t1\t.\tA\tG', '1\t2\t.\tA\tAT', '1\t3\t.\tAT\tA',
          '1\t4\t.\tA\tG,AT', '1\t1\t.\tA\tAT']
    with gzip.open(source,'wt') as stream:
        stream.write('\n'.join(rows)+'\n')
    result=composition(source)
    assert result['same'] is None
    assert result['reference_positions']==100
    assert result['counts']=={'other':2,'insertions':1,'deletions':1,'unknown':96}
    assert sum(result['counts'].values())==100
