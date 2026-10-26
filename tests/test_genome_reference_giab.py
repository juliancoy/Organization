import gzip
import pytest
from scripts.genome_reference_giab import compute,from_lines


def row(pos,ref,alt,gt='0/1',info='.'):
    return f'V\tchr1\t{pos}\t.\t{ref}\t{alt}\t.\tPASS\t{info}\tGT\t{gt}\n'


def test_reference_calls_exclude_full_overlapping_variant_spans():
    lines=['B\tchr1\t0\t20\n','B\t1\t10\t30\n','B\tX\t0\t100\n',
           row(2,'A','G'),row(5,'A','AT'),row(8,'AAAA','A'),
           row(10,'AA','CC'),row(20,'A','G','./.'),row(25,'A','<DEL>',info='END=27')]
    result=compute(gzip.compress(''.join(lines).encode()))
    assert result['benchmark_positions']==30
    assert result['excluded_variant_span_positions']==10
    assert result['same']==20
    assert result['variant_records']==6
    assert set(result['chromosomes'])=={'1'}


def test_no_reference_claim_outside_bed_or_for_missing_call():
    result=from_lines(['B\t1\t10\t20\n',row(10,'AAA','A','./.'),row(100,'A','G')])
    assert result['benchmark_positions']==10
    assert result['same']==8


def test_boundaries_empty_variant_and_reference_record():
    result=from_lines(['B\t1\t0\t10\n',row(1,'A','.','0/0'),row(10,'A','G'),row(11,'A','G')])
    assert result['same']==9


def test_invalid_bed_fails_closed():
    with pytest.raises(ValueError):from_lines(['B\t1\t9\t2\n'])


def test_prepared_shifted_anchor_also_excluded_without_double_counting_records():
    official=row(10,'AAA','A')
    shifted=row(5,'AAA','A').replace('V\t','P\t',1)
    duplicate=row(10,'AAA','A').replace('V\t','P\t',1)
    result=from_lines(['B\t1\t0\t20\n',official,shifted,duplicate])
    assert result['records']==1
    assert result['variant_records']==1
    assert result['prepared_records']==2
    assert result['excluded_variant_span_positions']==6
    assert result['same']==14
