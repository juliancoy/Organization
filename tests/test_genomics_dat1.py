import gzip,json
from scripts.genomics_dat1 import controls,package,LOCUS
from scripts.genomics_dat1_worker import audit


def test_missing_and_short_read_evidence_never_means_absent():
    for _,sam,expected in controls():
        result=audit({'locus':LOCUS,'sam':sam})
        assert result['direct_spanning_templates']==expected
        assert result['absence_supported'] is False
        assert result['genotype']=='not_assessed'
    result=audit({'locus':LOCUS,'sam':''})
    assert result['records']==0 and result['repeat']['mean_depth']==0
    assert result['absence_supported'] is False


def test_private_payload_is_bounded_hashed_and_counts_records():
    batch=package(controls()[1][1]);value=json.loads(gzip.decompress(batch['payload']))
    assert batch['records']==1 and value['locus']['assembly']=='GRCh38'
    assert len(batch['sha256'])==64


def test_state_updates_are_atomic_private_and_reusable(tmp_path):
    from scripts.genomics_dat1 import save_state
    save_state(tmp_path,{'status':'preparing'})
    save_state(tmp_path,{'status':'running'})
    assert json.loads((tmp_path/'state.json').read_text())=={'status':'running'}
    assert not list(tmp_path.glob('*.tmp'))
    assert (tmp_path/'state.json').stat().st_mode & 0o077 == 0
