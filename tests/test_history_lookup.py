from fab_sim.history_archive import HistoryArchive
from fab_sim.history_lookup import HistoryLookup

def record(i):return {'kind':'service:lane','id':str(i),'record':{'id':str(i),'status':'complete'}}

def test_disk_membership_is_exact_at_checkpoint_branch_head(tmp_path):
    archive=HistoryArchive(tmp_path);head=archive.append(None,[record(1),record(2)])
    lookup=HistoryLookup();assert lookup.contains(head,'service:lane','1')
    assert not lookup.contains(head,'service:lane','3')
    later=archive.append(head,[record(3)])
    assert lookup.contains(later,'service:lane','3')
    assert not lookup.contains(head,'service:lane','3')
    branch=archive.append(head,[record(4)])
    assert lookup.contains(branch,'service:lane','4')
    assert not lookup.contains(branch,'service:lane','3')
    assert lookup.contains(branch,'service:lane','2')
    lookup.close()

def test_incremental_lookup_avoids_history_scan_after_append(tmp_path,monkeypatch):
    archive=HistoryArchive(tmp_path);head=archive.append(None,[record(1)])
    lookup=HistoryLookup();assert lookup.contains(head,'service:lane','1')
    later=archive.append(head,[record(2)])
    lookup.advance(head,later,'service:lane',[record(2)])
    monkeypatch.setattr(HistoryArchive,'records',lambda *a: (_ for _ in ()).throw(AssertionError('reindexed historical chain')))
    assert lookup.contains(later,'service:lane','1') and lookup.contains(later,'service:lane','2')
    assert not lookup.contains(later,'service:lane','new')
    lookup.close()

def test_lookup_retains_only_current_namespace_watermark(tmp_path):
    archive=HistoryArchive(tmp_path);head=None;lookup=HistoryLookup()
    for batch in range(100):
        records=[record(batch*50+i) for i in range(50)]
        old=head;head=archive.append(head,records)
        lookup.advance(old,head,'service:lane',records)
        assert lookup.contains(head,'service:lane',str(batch*50))
        assert len(lookup._heads)==1
    assert lookup.stats()['namespaces']==1
    assert lookup.stats()['page_cache_kib']==2048
    assert lookup.stats()['disk_bytes']>0
    lookup.close()

def test_missing_archive_rebuild_rejects_before_membership(tmp_path):
    import pytest
    from fab_sim.checkpoint_format import CheckpointError
    archive=HistoryArchive(tmp_path);head=archive.append(None,[record(1)])
    archive._segment_path(head['head']).unlink()
    lookup=HistoryLookup()
    with pytest.raises(CheckpointError):lookup.contains(head,'service:lane','1')
    assert not lookup._heads
    lookup.close()


def test_lookup_replaces_prior_lineage_watermark_for_same_owner(tmp_path):
    lookup=HistoryLookup()
    for i in range(20):
        archive=HistoryArchive(tmp_path/str(i));head=archive.append(None,[record(i)])
        assert lookup.contains(head,'service:lane',str(i))
        assert len(lookup._heads)==1
    lookup.close()
