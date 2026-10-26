import gzip
from scripts.genome_reference_compute import compute, finalize, subtract


def batch(rows):
    return compute(gzip.compress(('\n'.join(rows) + '\n').encode()))


def record(pos, ref='A', alt='.', info='.', fields='GT:DP:GQ', values='0/0:30:30', filt='PASS'):
    return '\t'.join(['1', str(pos), '.', ref, alt, '.', filt, info, fields, values])


def test_reference_quality_and_block_minimum():
    part = batch([
        record(1, info='END=10;MinDP=25'),
        record(11, info='END=20', values='0/0:40:30'),
        record(21, info='END=30;MinDP=25', fields='GT:DP', values='0/0:30'),
        record(31, values='0/0:30:19'),
        record(32, values='0:30:20'),
        record(33, values='./.:30:30'),
        record(34, filt='LowGQ'),
    ])
    result = finalize([part])
    assert result['counts']['confirmed_reference'] == {'1': 11}
    assert result['counts']['reference_quality_unknown'] == {'1': 20}


def test_cross_chunk_variant_deletion_and_conflict_exclusion():
    reference = batch([record(1, info='END=100;MinDP=30')])
    variants = batch([record(20, ref='AAAA', alt='A', values='0/1:30:30'),
                      record(40, values='0/0:30:.'),
                      record(50, values='0/0:10:30')])
    result = finalize([reference, variants])
    assert result['counts']['confirmed_reference'] == {'1': 94}
    assert result['counts']['reference_quality_unknown'] == {'1': 1}


def test_union_subtraction_handles_overlap_and_adjacency():
    assert subtract([[0, 10], [8, 20], [20, 30]], [[5, 12], [11, 15], [25, 50]]) == [[0, 5], [15, 25]]


def test_minimum_depth_cannot_fall_back_to_block_average():
    result = finalize([batch([record(1, info='END=10;MinDP=.', values='0/0:100:30')])])
    assert 'confirmed_reference' not in result['counts']
    assert result['counts']['reference_quality_unknown'] == {'1': 10}


def test_real_alt_excluded_even_with_reference_genotype():
    result = finalize([batch([record(1, info='END=10;MinDP=30'),
                              record(3, alt='T', values='0/0:30:30')])])
    assert result['counts']['confirmed_reference'] == {'1': 9}
