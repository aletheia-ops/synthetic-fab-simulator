import json
import pytest
from fab_sim.inputs import read_json,canonical_digest,validate_keys,bind_validation,verify_binding

@pytest.mark.parametrize('text',['{"a":1,"a":2}','{"a":NaN}','{"a":Infinity}','[]'])
def test_invalid_json_rejects(tmp_path,text):
    p=tmp_path/'bad.json';p.write_text(text)
    with pytest.raises(ValueError):read_json(p)

def test_canonical_identity_and_binding(tmp_path):
    a={'schema_version':1,'model':{'id':'한글'},'provenance':{'source':'test'}}
    b=bind_validation(a,{'groups':['T1']},'test-v1')
    assert verify_binding(b,{'groups':['T1']},'test-v1')
    assert canonical_digest({'a':1,'b':2})==canonical_digest({'b':2,'a':1})
    b['model']['id']='edited'
    assert not verify_binding(b,{'groups':['T1']},'test-v1')

def test_unknown_keys_reject_and_extensions_are_explicit():
    with pytest.raises(ValueError,match='unknown'):validate_keys({'typo':1},{'id'},'config')
    validate_keys({'id':'a','extensions':{'custom':1}},{'id'},'config')


@pytest.mark.parametrize('number',[1e-6,1e20,-0.0,1.5e-12])
def test_bound_numeric_settings_survive_exact_json_reload(tmp_path,number):
    a=bind_validation({'schema_version':1,'model':{'setting':number},'provenance':{}},{},'test-v1')
    path=tmp_path/'prepared.json'; path.write_text(json.dumps(a),encoding='utf-8')
    assert verify_binding(read_json(path),{},'test-v1')


def test_decimal_canonical_identity_never_rounds_to_context_precision():
    from decimal import Decimal
    assert canonical_digest({'n':Decimal('123456789012345678901234567890.1')}) != canonical_digest({'n':Decimal('123456789012345678901234567890.2')})
