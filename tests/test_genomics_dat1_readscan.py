from scripts.genomics_dat1_readscan_worker import extract_candidate
import pytest


def test_recruitment_preserves_record_and_excludes_quality_line_hits():
    sequence=b'ACGT'*25;quality=b'I'*100
    record=b'@SRR7097858.42 1/1\n'+sequence+b'\n+\n'+quality+b'\n'
    result=extract_candidate(record,record.index(sequence)+40,'SRR7097858')
    assert result==(b'@SRR7097858.42',record)
    assert extract_candidate(record,record.index(quality)+40,'SRR7097858') is None
    assert extract_candidate(record,record.index(sequence)+40,'SRR000000') is None
    assert extract_candidate(record[:-20],record.index(sequence)+40,'SRR7097858') is None
    with pytest.raises(ValueError):extract_candidate(record.replace(b'\n+\n',b'\nX\n'),record.index(sequence)+40,'SRR7097858')
