import json
from pathlib import Path
import pytest
from test_generation import inputs, generate


def test_compatible_startup_does_not_repeat_geometry_validation(tmp_path,monkeypatch):
    from fab_sim.generation import load_prepared
    import fab_sim.topology as topology
    m,_=inputs(); artifact=generate(); path=tmp_path/'topology.json'
    path.write_text(json.dumps(artifact),encoding='utf-8')
    def reject(*args): raise AssertionError('expensive geometry validation repeated')
    monkeypatch.setattr(topology,'compile_traffic',reject)
    assert load_prepared(path,m)==artifact


def test_prepared_edit_and_missing_path_reject_without_generation(tmp_path):
    from fab_sim.generation import load_prepared
    m,_=inputs(); path=tmp_path/'missing.json'
    with pytest.raises(FileNotFoundError): load_prepared(path,m)
    a=generate(); a['model']['docks'][0]['offset_um']+=1
    path.write_text(json.dumps(a),encoding='utf-8')
    with pytest.raises(ValueError,match='identity'): load_prepared(path,m)
