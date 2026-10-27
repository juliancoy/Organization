import pytest
from wgs_eta import estimate_eta


def points(*pairs):
    return [{'timestamp':t,'completed':c} for t,c in pairs]


def test_sustained_rate_and_complete():
    value=estimate_eta(points((0,0),(60,120),(120,240)),1000,now=120)
    assert value['status']=='estimating'
    assert value['rate_per_second']==2 and value['seconds_remaining']==380
    assert value['optimistic_seconds']==value['pessimistic_seconds']==380
    assert estimate_eta(points((120,1000)),1000,now=120)['status']=='complete'


def test_reset_and_generation_change_do_not_reuse_old_rate():
    assert estimate_eta(points((0,500),(60,800),(90,10)),1000,now=90)['status']=='insufficient_data'
    rows=[{'timestamp':0,'completed':0,'generation':'a'}, {'timestamp':60,'completed':800,'generation':'b'}]
    assert estimate_eta(rows,1000,now=60)['status']=='insufficient_data'


def test_blocked_stale_and_stalled_have_no_finite_eta():
    rows=points((0,0),(60,100),(240,100))
    for kwargs,status in [({'now':240,'blocked':True},'blocked'),({'now':400},'stale'),({'now':240},'stalled')]:
        value=estimate_eta(rows,1000,**kwargs)
        assert value['status']==status and value['seconds_remaining'] is None


def test_chunk_bursts_include_idle_time_and_variable_bounds():
    value=estimate_eta(points((0,0),(60,100),(120,100),(180,300)),1000,now=180)
    assert value['rate_per_second']==pytest.approx(300/180)
    assert value['optimistic_seconds']==210
    assert value['pessimistic_seconds'] is None  # observed idle window


def test_invalid_duplicates_and_out_of_order_samples():
    rows=points((0,0),(60,100),(30,50),(60,120),(90,float('nan')),(1000,999))
    value=estimate_eta(rows,1000,now=60)
    assert value['rate_per_second']==2
    with pytest.raises(ValueError):estimate_eta([],float('nan'),now=0)
    with pytest.raises(ValueError):estimate_eta([],100,now=0,min_span=0)
