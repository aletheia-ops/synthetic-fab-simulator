import json
import pytest
from test_runner import fixture
from fab_sim.runner import load_config
from fab_sim.checkpoint import physical_identity


def test_budget_replacement_eviction_and_oversize():
    from fab_sim.derived_cache import DerivedCache
    c=DerivedCache(2048)
    assert c.put('row','key',{'a':12})
    size=c.stats()['retained_bytes']
    assert c.put('row','key',{'a':12}) and c.stats()['retained_bytes']==size
    for n in range(10000):
        c.put('row',str(n),{'b':n})
        assert c.stats()['retained_bytes']<=2048
    assert c.stats()['evictions']>0 and c.stats()['entries']<10
    assert not c.put('row','huge','x'*10000)
    assert c.get('row','huge') is None
    c.clear();assert c.stats()['entries']==0 and c.stats()['retained_bytes']==0


@pytest.mark.parametrize('value',[True,0,-1,1.5,'512'])
def test_invalid_memory_budget(value):
    from fab_sim.derived_cache import DerivedCache
    with pytest.raises(ValueError):DerivedCache(value)


def test_budget_is_performance_only(tmp_path):
    path=fixture(tmp_path);c=load_config(path)
    assert c['execution']['derived_cache_budget_mib']==512
    before=physical_identity(c,{})
    c['execution']['derived_cache_budget_mib']=1
    assert physical_identity(c,{})==before
    c['execution']['master_seed']+=1
    assert physical_identity(c,{})!=before
    raw=json.loads(path.read_text())
    for value in (True,0,-1,1.5,'512'):
        raw['execution']['derived_cache_budget_mib']=value;path.write_text(json.dumps(raw))
        with pytest.raises(ValueError):load_config(path)
