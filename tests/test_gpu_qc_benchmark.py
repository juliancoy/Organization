import pytest
from scripts.gpu_qc_benchmark import validate_result


def result():
    return {'device_type':1,'software_fallback':False,'exact_match':True,'bases':6,'counts':dict(A=1,C=1,G=1,T=1,N=1,other=1)}


def test_software_vulkan_device_is_rejected_even_when_counts_match():
    software={**result(),'device_type':4}
    with pytest.raises(ValueError,match='physical'):validate_result(software)
    with pytest.raises(ValueError,match='physical'):validate_result({**result(),'software_fallback':True})


def test_gpu_counts_must_exactly_match_cpu_and_cover_every_base():
    assert validate_result(result())['bases']==6
    with pytest.raises(ValueError,match='validation'):validate_result({**result(),'exact_match':False})
    with pytest.raises(ValueError,match='validation'):validate_result({**result(),'bases':7})
    with pytest.raises(ValueError,match='aggregate'):validate_result({**result(),'counts':dict(A=-1,C=1,G=1,T=1,N=1,other=3)})
