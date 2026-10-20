"""Deferred archive durability: segments are synced before any durable reference."""
import os
from fab_sim.history_archive import HistoryArchive


def record(i):return {'kind':'k','id':f'r{i}','record':{'n':i}}


def counting(monkeypatch):
    calls=[]
    real=os.fsync
    monkeypatch.setattr(os,'fsync',lambda fd:(calls.append(fd),real(fd))[1])
    return calls


def test_immediate_archive_syncs_every_append(tmp_path,monkeypatch):
    calls=counting(monkeypatch);archive=HistoryArchive(tmp_path)
    archive.append(None,[record(1)])
    assert len(calls)==3  # segment, its shard directory and the new shard's entry


def test_deferred_archive_syncs_on_demand_with_same_content(tmp_path,monkeypatch):
    calls=counting(monkeypatch)
    deferred=HistoryArchive(tmp_path/'a',deferred_sync=True);immediate=HistoryArchive(tmp_path/'b')
    flushes=[];monkeypatch.setattr(os,'sync',lambda:flushes.append(1))
    head=deferred.append(None,[record(1)]);head=deferred.append(head,[record(2)])
    shards={p.parent for p in deferred._unsynced}
    assert calls==[]
    deferred.sync()
    # One filesystem flush, then the touched shard directories and the archive directory.
    assert flushes==[1] and len(calls)==len(shards)+1
    deferred.sync();assert flushes==[1] and len(calls)==len(shards)+1
    other=immediate.append(None,[record(1)]);other=immediate.append(other,[record(2)])
    assert {k:v for k,v in head.items() if k!='directory'}=={k:v for k,v in other.items() if k!='directory'}
    assert [r['id'] for r in deferred.records(head)]==[r['id'] for r in immediate.records(other)]


def test_runner_syncs_archives_before_each_checkpoint_and_the_final_manifest(tmp_path,monkeypatch):
    from test_runner import fixture
    from fab_sim.runner import run
    from fab_sim.checkpoint_store import CheckpointStore
    order=[]
    real_sync=HistoryArchive.sync;real_write=CheckpointStore.write
    monkeypatch.setattr(HistoryArchive,'sync',lambda self:(order.append('sync'),real_sync(self))[1])
    monkeypatch.setattr(CheckpointStore,'write',lambda self,bundle:(order.append('write'),real_write(self,bundle))[1])
    result=run(fixture(tmp_path,checkpoint={'enabled':True},execution={'horizon_us':1000000000,'max_settlements':10}))
    assert result['outcome']!='Failed',result.get('failure')
    assert 'write' in order
    assert all(order[i-1]=='sync' for i,item in enumerate(order) if item=='write')
