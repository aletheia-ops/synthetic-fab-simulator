from copy import deepcopy
import json
from pathlib import Path
import pytest
from test_generation import inputs


def test_matching_settings_reuse_saved_topology_without_generation(tmp_path,monkeypatch):
    from fab_sim import topology_store as store
    m,p=inputs()
    a,first=store.generated(m,p,{},tmp_path)
    before=Path(first['path']).stat().st_mtime_ns
    def forbidden(*args):raise AssertionError('cache hit regenerated topology')
    monkeypatch.setattr(store,'generate_topology',forbidden)
    b,second=store.generated(m,p,{'counts':{'A':1,'B':1},'seed':0,'entry_capacity':8,
        'exit_capacity':8,'improvement_limit':10,'pickup_seconds':'10.0'},tmp_path)
    assert a==b and first['generation_key']==second['generation_key']
    assert second['cache_hit'] and Path(second['path']).stat().st_mtime_ns==before


@pytest.mark.parametrize('settings',[{'counts':{'A':2}},{'rack_capacities':{'A':8}},
    {'fleet_count':2},{'seed':2},{'improvement_limit':1},{'drop_seconds':12}])
def test_changed_generation_inputs_select_different_artifact(tmp_path,settings):
    from fab_sim import topology_store as store
    m,p=inputs();_,old=store.generated(m,p,{},tmp_path)
    _,new=store.generated(m,p,settings,tmp_path)
    assert old['generation_key']!=new['generation_key'] and old['path']!=new['path']
    assert Path(old['path']).is_file() and Path(new['path']).is_file()


def test_corrupt_registered_artifact_is_rejected_not_replaced(tmp_path):
    from fab_sim import topology_store as store
    m,p=inputs();_,record=store.generated(m,p,{},tmp_path)
    path=Path(record['path']);path.write_text('{}')
    with pytest.raises(ValueError):store.generated(m,p,{},tmp_path)
    assert path.read_text()=='{}'


def test_registry_does_not_accept_redirected_generation_key(tmp_path):
    from fab_sim import topology_store as store
    m,p=inputs();_,one=store.generated(m,p,{},tmp_path)
    _,two=store.generated(m,p,{'counts':{'A':2}},tmp_path)
    entry=tmp_path/'requests'/(one['generation_key']+'.json')
    data=json.loads(entry.read_text());data['content_sha256']=two['content_sha256']
    entry.write_text(json.dumps(data))
    with pytest.raises(ValueError):store.generated(m,p,{},tmp_path)


def test_preflight_and_run_share_saved_topology_and_manifest_reference(tmp_path):
    from test_runner import fixture
    from fab_sim.runner import load_config,run
    from fab_sim.preflight import prepare
    path=fixture(tmp_path,inputs={'manufacturing_path':'manufacturing.json','physical_reference_path':'reference.json'},
        generation={'seed':1,'improvement_limit':1,'entry_capacity':2,'exit_capacity':1})
    config=load_config(path);_,topology=prepare(config)
    saved=Path(config['output']['topology_directory'])/(topology['validation']['content_sha256']+'.json')
    assert saved.is_file()
    result=run(path)
    manifest=json.loads((tmp_path/'result/manifest.json').read_text())
    record=manifest['topology_artifact']
    assert Path(record['path'])==saved and record['cache_hit']
    assert record['content_sha256']==topology['validation']['content_sha256']
    assert result['exited_count']==2 and result['outcome']=='Completed'


def test_cli_always_registers_topology_and_keeps_explicit_export(tmp_path,monkeypatch,capsys):
    from fab_sim.cli import main
    from fab_sim.inputs import canonical_text
    m,p=inputs();monkeypatch.chdir(tmp_path)
    (tmp_path/'pyproject.toml').write_text('[project]\nname="test-fab"\n')
    for name,value in [('m',m),('p',p)]:
        (tmp_path/(name+'.json')).write_text(canonical_text(value))
    assert main(['generate-topology','--manufacturing','m.json','--reference','p.json','--output','export.json'])==0
    record=json.loads(capsys.readouterr().out)
    assert Path(record['topology_artifact']['path']).parent==tmp_path/'topologies'
    assert json.loads(Path(record['topology_artifact']['path']).read_text())==json.loads((tmp_path/'export.json').read_text())
    assert main(['generate-topology','--manufacturing','m.json','--reference','p.json'])==0
    assert json.loads(capsys.readouterr().out)['topology_artifact']['cache_hit']


def test_equivalent_geometry_numbers_and_stream_order_reuse(tmp_path):
    from fab_sim import topology_store as store
    from fab_sim.generation import DEFAULT_GEOMETRY
    from fab_sim.inputs import bind_validation
    from fab_sim.sources.smt import VALIDATOR
    m,p=inputs();m['model']['releases'][0]['id']='regular'
    m=bind_validation(m,{'tool_groups':['A','B']},VALIDATOR)
    from fab_sim.sources.smat import VALIDATOR as PV,traffic_requirements
    p=bind_validation(p,traffic_requirements(m),PV)
    geometry=deepcopy(DEFAULT_GEOMETRY);geometry['clearance_mm']=500.1
    _,one=store.generated(m,p,{'geometry':geometry},tmp_path)
    _,two=store.generated(m,p,{'geometry':json.loads(json.dumps(geometry)),'selected_streams':['regular']},tmp_path)
    assert one['generation_key']==two['generation_key'] and two['cache_hit']


def test_source_and_generator_changes_invalidate_lookup(tmp_path,monkeypatch):
    from fab_sim import topology_store as store
    from fab_sim.inputs import bind_validation
    from fab_sim.sources.smat import VALIDATOR,traffic_requirements
    m,p=inputs();_,one=store.generated(m,p,{},tmp_path)
    changed=deepcopy(p);changed['provenance']['source_sha256']='second-source'
    changed=bind_validation(changed,traffic_requirements(m),VALIDATOR)
    _,two=store.generated(m,changed,{},tmp_path)
    assert one['generation_key']!=two['generation_key']
    monkeypatch.setattr(store,'ADAPTER',store.ADAPTER+'-next')
    import fab_sim.generation as generation
    monkeypatch.setattr(generation,'ADAPTER',store.ADAPTER)
    _,three=store.generated(m,p,{},tmp_path)
    assert one['generation_key']!=three['generation_key']


def test_simultaneous_identical_generation_preserves_complete_registration(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from fab_sim import topology_store as store
    m,p=inputs()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results=list(executor.map(lambda _:store.generated(m,p,{},tmp_path),range(2)))
    assert results[0][0]==results[1][0]
    assert len(list(tmp_path.glob('*.json')))==1
    assert store.generated(m,p,{},tmp_path)[1]['cache_hit']
    assert not list(tmp_path.rglob('.pending-*'))


def test_invalid_explicit_footprint_cannot_bypass_validation_through_cache(tmp_path):
    from fab_sim import topology_store as store
    m,p=inputs();store.generated(m,p,{},tmp_path)
    with pytest.raises(ValueError):store.generated(m,p,{'footprints':{'A':None}},tmp_path)


def test_none_geometry_and_numeric_strings_reuse_defaults(tmp_path):
    from fab_sim import topology_store as store
    from fab_sim.generation import DEFAULT_GEOMETRY
    m,p=inputs();_,one=store.generated(m,p,{},tmp_path)
    _,two=store.generated(m,p,{'geometry':None},tmp_path)
    geometry=deepcopy(DEFAULT_GEOMETRY);geometry['length_mm']='60000.0'
    _,three=store.generated(m,p,{'geometry':geometry},tmp_path)
    assert one['generation_key']==two['generation_key']==three['generation_key']


def test_external_config_and_cwd_use_checkout_store(tmp_path,monkeypatch):
    from test_runner import fixture
    from fab_sim.runner import load_config
    from fab_sim import topology_store as store
    path=fixture(tmp_path);monkeypatch.chdir(tmp_path)
    expected=Path(store.__file__).resolve().parents[2]/'topologies'
    assert Path(load_config(path)['output']['topology_directory'])==expected
    assert store.default_directory()==expected


def test_simultaneous_equivalent_settings_return_validated_winner(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from fab_sim import topology_store as store
    m,p=inputs();barrier=Barrier(2);generate=store.generate_topology
    def simultaneous(*args):
        barrier.wait(timeout=10)
        return generate(*args)
    monkeypatch.setattr(store,'generate_topology',simultaneous)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results=list(executor.map(lambda s:store.generated(m,p,s,tmp_path),[{}, {'seed':0}]))
    assert results[0][0]==results[1][0] and results[0][1]['path']==results[1][1]['path']
    registration=json.loads(next((tmp_path/'requests').glob('*.json')).read_text())
    assert registration['content_sha256']==results[0][1]['content_sha256']
