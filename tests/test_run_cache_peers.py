import io
from types import SimpleNamespace
import pytest
from scripts.run_cache_peers import PeerRanges

class Response(io.BytesIO):
    status=206
    def __init__(self,data,offset=0,total=4):
        super().__init__(data);self.headers={'Content-Length':str(len(data)),'Content-Range':f'bytes {offset}-{offset+len(data)-1}/{total}'}

def selector(outcomes):
    calls=[]
    def request(name,path,**kwargs):
        calls.append((name,path))
        result=outcomes[name]
        if isinstance(result,Exception):raise result
        return Response(result)
    peer=SimpleNamespace(name='local',manifest={'nodes':{'local':{},'a':{},'b':{},'origin':{}}},open_peer=request)
    manifest={'source':'origin','cache_peers':{'local':{},'a':{'port':1111},'b':{'port':2222}}}
    return PeerRanges(peer,manifest,'a'*32,4),calls,peer


def test_hit_avoids_origin_and_preserves_source_endpoint():
    ranges,calls,peer=selector({'a:cache':b'data'})
    assert ranges(0,4)==b'data' and ranges.hits==1
    assert calls==[('a:cache','/cache/'+'a'*32)]
    assert 'a:cache' not in peer.manifest['nodes']


def test_miss_and_disconnect_fall_back_with_cooldown():
    ranges,calls,_=selector({'a:cache':OSError('miss'),'b:cache':TimeoutError(),'origin':b'data'})
    assert ranges(0,4)==b'data'
    assert ranges(0,4)==b'data'
    assert [name for name,_ in calls]==['a:cache','b:cache','origin','origin']
    assert ranges.source_fetches==2


def test_invalid_peer_data_does_not_prevent_origin_fallback():
    ranges,calls,_=selector({'a:cache':b'bad','b:cache':OSError(),'origin':b'data'})
    assert ranges(0,4)==b'data'
    assert calls[-1][0]=='origin'


def test_unenrolled_peer_rejected():
    with pytest.raises(ValueError,match='Unenrolled'):
        PeerRanges(SimpleNamespace(manifest={'nodes':{}}),{'cache_peers':{'stranger':{}}},'a'*32,4)


def test_successful_peer_prioritized_next_time():
    outcomes={'a:cache':OSError(),'b:cache':b'data','origin':b'data'}
    ranges,calls,_=selector(outcomes)
    ranges(0,4);ranges.cooldown.clear();calls.clear()
    assert ranges(0,4)==b'data' and calls[0][0]=='b:cache'
