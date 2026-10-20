from collections import Counter
from copy import deepcopy
import pytest
from test_generation import generate


def stocker_model():
    model=deepcopy(generate({'improvement_limit':1})['model'])
    model['equipment'].append({'id':'stocker','kind':'stocker','rack_capacity':8})
    prototype=next(d for d in model['docks'] if d['kind']=='track_buffer')
    for name in ('in','out'):
        model['docks'].append(prototype|{'id':'stocker:'+name,'kind':'stocker','equipment_id':'stocker','role':'BOTH'})
    return model


def test_multiport_stocker_keeps_capacity_and_allocates_each_slot_to_one_service_port():
    from fab_sim.runner import allocate
    _,custody,_,_,_=allocate(stocker_model())
    slots=[s for s in custody.slots.values() if s['kind']=='storage']
    assert len(slots)==8
    assert Counter(s['dock_id'] for s in slots)=={'stocker:in':4,'stocker:out':4}
    assert all(s['equipment_id']=='stocker' for s in slots)


def test_multiport_stocker_rejects_unsupported_directional_slot_mapping():
    from fab_sim.runner import allocate
    model=stocker_model();model['docks'][-1]['role']='IN'
    with pytest.raises(ValueError,match='bidirectional'):
        allocate(model)
