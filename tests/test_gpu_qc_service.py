import importlib.util,json,os
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('gpu_server',Path(__file__).parents[1]/'scripts/gpu-tools/qc_server.py');server=importlib.util.module_from_spec(spec);spec.loader.exec_module(server)


def test_private_batch_reader_rejects_escape_symlinks_modes_and_oversize(tmp_path,monkeypatch):
    root=tmp_path/'gpu-qc';batch=root/'batches';batch.mkdir(parents=True);data=batch/'batch.seq';data.write_bytes(b'ACGTN');data.chmod(0o600)
    monkeypatch.setattr(server,'ROOT',root);monkeypatch.setattr(server,'HOST_ROOT',Path('/worker/gpu-qc'));monkeypatch.setattr(server,'MAX_BATCH',5)
    assert server.read_batch('/worker/gpu-qc/batches/batch.seq')==b'ACGTN'
    with pytest.raises(ValueError,match='outside'):server.read_batch('/somewhere/private.seq')
    with pytest.raises(ValueError,match='traversal'):server.read_batch('/worker/gpu-qc/batches/../batch.seq')
    data.chmod(0o640)
    with pytest.raises(ValueError,match='owner-only'):server.read_batch('/worker/gpu-qc/batches/batch.seq')
    data.chmod(0o600);(batch/'link').symlink_to(data)
    with pytest.raises(ValueError,match='Symlink'):server.read_batch('/worker/gpu-qc/batches/link')
    data.write_bytes(b'ACGTNN')
    with pytest.raises(ValueError,match='bounds'):server.read_batch('/worker/gpu-qc/batches/batch.seq')
    os.mkfifo(batch/'fifo',0o600)
    with pytest.raises(ValueError,match='regular'):server.read_batch('/worker/gpu-qc/batches/fifo')


def test_quality_aggregates_reject_invalid_bins_and_inconsistent_totals():
    row={'device_type':1,'software_fallback':False,'bases':2,'counts':{'A':1,'C':0,'G':0,'T':0,'N':1,'other':0},'invalid_quality':0,'quality_histogram':[0]*94,'quality_sum':50,'q20':2,'q30':1};row['quality_histogram'][20]=row['quality_histogram'][30]=1
    assert server.validate_aggregates(row)['quality_sum']==50
    with pytest.raises(ValueError,match='Phred'):server.validate_aggregates({**row,'invalid_quality':1})
    with pytest.raises(RuntimeError,match='quality'):server.validate_aggregates({**row,'quality_sum':51})
    with pytest.raises(RuntimeError,match='Software'):server.validate_aggregates({**row,'device_type':4})


def test_mask_fixture_oracle_preserves_unknown_spans_and_pair_counts():
    # Every person: confident reference, ALT start, span-only unknown, global N.
    ref=sum(1<<(4*i) for i in range(6));variant=sum(10<<(4*i) for i in range(6));span=sum(2<<(4*i) for i in range(6))
    counts=server.mask_oracle([ref,variant,span,ref|0x80000000])
    assert counts[:6]==[1]*6 and counts[6:12]==[1]*6 and counts[12:18]==[2]*6
    assert counts[18:24]==[2]*6
    assert counts[24:]==[2,2,0]*15
    row={'positions':4,'mask_counts':counts,'software_fallback':False,'device_type':1}
    assert server.validate_mask(row)['positions']==4
    bad=counts.copy();bad[0]+=1
    with pytest.raises(RuntimeError,match='partition'):server.validate_mask({**row,'mask_counts':bad})
