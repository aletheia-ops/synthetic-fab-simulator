"""Static lookup is copied once and cannot acquire physical resources."""
import pytest


def records():
    slots={'rack':{'id':'rack','kind':'rack','equipment_id':'t'},
           'store':{'id':'store','kind':'entry','equipment_id':'entry','dock_id':'entry-dock'}}
    tools={'t':{'id':'t','tool_group_id':'A','rack_slots':['rack']},
           'u':{'id':'u','tool_group_id':'B','rack_slots':[]}}
    model={'docks':[{'id':'in','equipment_id':'t','role':'IN','rail_id':'r'},
                    {'id':'out','equipment_id':'t','role':'OUT','rail_id':'r'},
                    {'id':'entry-dock','equipment_id':'entry','role':'OUT','rail_id':'s'}],
           'vehicles':[{'id':'v','type_id':'type'},{'id':'other','type_id':'other-type'}]}
    return slots,tools,model


def test_index_copies_sorted_relationships_and_internal_stocker_rails():
    from fab_sim.resource_index import ResourceIndex
    slots,tools,model=records();index=ResourceIndex(slots,tools,model)
    assert index.tool_by_slot=={'rack':'t'}
    assert index.tools_by_group=={'A':('t',),'B':('u',)}
    assert index.group_by_tool=={'t':'A','u':'B'}
    assert index.input_docks_by_group['A']==('in',) and index.output_docks_by_group['A']==('out',)
    assert index.rail_by_dock['store']=='s'
    assert index.vehicles_by_type=={'type':('v',),'other-type':('other',)}
    with pytest.raises(TypeError):index.tools_by_group['A']=('stolen',)
    tools['t']['tool_group_id']='changed';model['docks'][0]['rail_id']='changed'
    assert index.group_by_tool['t']=='A' and index.rail_by_dock['in']=='r'


def test_index_rejects_contradictory_rack_ownership():
    from fab_sim.resource_index import ResourceIndex
    slots,tools,model=records();slots['rack']['equipment_id']='u'
    with pytest.raises(ValueError,match='rack'):ResourceIndex(slots,tools,model)
