import pytest
from fab_sim.history_archive import HistoryArchive
from fab_sim.checkpoint_format import CheckpointError

def test_immutable_archive_branch_stays_at_checkpoint_boundary(tmp_path):
    archive=HistoryArchive(tmp_path)
    first=archive.append(None,[{'kind':'clock','id':'a','record':{'id':'a','closed_at_us':1}}])
    second=archive.append(first,[{'kind':'clock','id':'b','record':{'id':'b','closed_at_us':2}}])
    branch=archive.append(first,[{'kind':'clock','id':'c','record':{'id':'c','closed_at_us':3}}])
    assert {r['id'] for r in archive.records(first)}=={'a'}
    assert {r['id'] for r in archive.records(second)}=={'a','b'}
    assert {r['id'] for r in archive.records(branch)}=={'a','c'}
    assert archive.append(first,[{'kind':'clock','id':'b','record':{'id':'b','closed_at_us':2}}])==second

@pytest.mark.parametrize('damage',['missing','corrupt','counter'])
def test_missing_or_corrupt_archive_rejected(tmp_path,damage):
    archive=HistoryArchive(tmp_path)
    head=archive.append(None,[{'kind':'clock','id':'a','record':{'id':'a'}}])
    path=archive._segment_path(head['head'])
    if damage=='missing':path.unlink()
    elif damage=='corrupt':path.write_bytes(b'bad')
    else:head['records']+=1
    with pytest.raises(CheckpointError):list(archive.records(head))

def test_archive_batch_is_bounded_and_no_seen_id_cache(tmp_path):
    archive=HistoryArchive(tmp_path,max_records=4,max_bytes=2048)
    head=None
    for i in range(100):
        head=archive.append(head,[{'kind':'clock','id':str(i),'record':{'id':str(i)}}])
    # Bounds, directory and the flush flag only; the deferred-sync list stays empty when
    # immediate durability is used.
    assert head['records']==100 and len(vars(archive))==6 and archive._unsynced==[]
    with pytest.raises(ValueError):archive.append(head,[{}]*5)
    with pytest.raises(ValueError):archive.append(head,[{'kind':'clock','id':'big','record':{'data':'x'*3000}}])


def test_segments_are_sharded_compressed_and_pre_sharding_segments_stay_readable(tmp_path):
    import hashlib,zlib
    from fab_sim.history_archive import _segment_payload
    archive=HistoryArchive(tmp_path/'new')
    head=archive.append(None,[{'kind':'clock','id':'a','record':{'id':'a'}}])
    path=archive._segment_path(head['head'])
    assert path==tmp_path/'new'/head['head'][:2]/(head['head']+'.jsonz')
    assert hashlib.sha256(zlib.decompress(path.read_bytes())).hexdigest()==head['head']
    # An archive written before sharding (flat, uncompressed) in a parent attempt's directory.
    old=tmp_path/'old';old.mkdir()
    data={'version':1,'previous_head':None,'previous_records':0,'previous_segments':0,
          'records':[{'kind':'clock','id':'x','record':{'id':'x'}}]}
    payload,_=_segment_payload(data);identity=hashlib.sha256(payload).hexdigest()
    (old/(identity+'.json')).write_bytes(payload)
    legacy={'schema':'fab-history-archive-v1','directory':str(old),'head':identity,'records':1,'segments':1,
            'logical_digest':format(int.from_bytes(hashlib.sha256(_segment_payload(data)[1][0]).digest(),'big'),'064x')}
    child=archive.append(legacy,[{'kind':'clock','id':'y','record':{'id':'y'}}])
    assert sorted(r['id'] for r in archive.records(child))==['x','y']


def test_retirement_waits_for_a_full_segment_unless_flushing(tmp_path):
    from fab_sim.history_archive import _retire_records
    class Owner:
        def __init__(self):self.records={str(i):{'done':True} for i in range(5)};self.history_archive=None
    archive=HistoryArchive(tmp_path,max_records=4)
    owner=Owner();archive.flush=False
    assert _retire_records(owner,archive,'k',lambda i,r:True)==4  # a full segment goes out
    assert _retire_records(owner,archive,'k',lambda i,r:True)==0 and len(owner.records)==1
    archive.flush=True
    assert _retire_records(owner,archive,'k',lambda i,r:True)==1 and not owner.records


def test_append_all_splits_a_batch_that_exceeds_the_byte_bound(tmp_path):
    archive=HistoryArchive(tmp_path,max_records=8,max_bytes=4096)
    records=[{'kind':'k','id':str(i),'record':{'data':'x'*900}} for i in range(8)]
    with pytest.raises(ValueError,match='byte bound'):archive.append(None,records)
    head=archive.append_all(None,records)
    assert head['records']==8 and head['segments']>1
    assert sorted(r['id'] for r in archive.records(head))==[str(i) for i in range(8)]


# Segments assembled from per-record encodings equal encode_value of the whole segment.
from fractions import Fraction


def _payload_record(i):
    return {'kind':'k'+str(i%3),'id':f'id:{i}','record':{'a':i,'b':[1,'x',None,True],
        'c':{'z':Fraction(1,3),'é':'u','k"q':2},'s':{3,1,2},'t':(1,2),'f':0.5}}


@pytest.mark.parametrize('count',[1,2,7,64])
@pytest.mark.parametrize('head',[None,{'head':'a'*64,'records':5,'segments':2}])
def test_segment_payload_matches_whole_segment_encoding(count,head):
    from fab_sim.checkpoint_format import encode_value
    from fab_sim.history_archive import _segment_payload
    data={'version':1,'previous_head':head['head'] if head else None,
        'previous_records':head['records'] if head else 0,
        'previous_segments':head['segments'] if head else 0,'records':[_payload_record(i) for i in range(count)]}
    payload,encoded=_segment_payload(data)
    assert payload==encode_value(data)
    assert encoded==[encode_value(record) for record in data['records']]
