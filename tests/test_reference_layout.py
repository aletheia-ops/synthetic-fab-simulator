from copy import deepcopy
import pytest


def site(name, width=2_000_000, height=2_000_000, x=0, group='A'):
    return {'id':name, 'source_equipment_id':name, 'source_group_id':group,
            'bay_id':'Bay', 'x_um':x, 'y_um':0, 'width_um':width, 'height_um':height,
            'orientation':'source_xy', 'source_docks':[{'id':name+':dock','role':'BOTH'}]}


def instance(name, width=2_000_000, height=2_000_000, group='A'):
    return {'id':name,'tool_group_id':group,'width_um':width,'height_um':height}


def test_assignment_rehomes_flexible_instance():
    from fab_sim.reference_layout import assign_sites
    sites=[site('a-large',width=4_000_000),site('z-small',x=10_000_000)]
    machines=[instance('a-flexible'),instance('z-restricted',width=4_000_000)]
    assert assign_sites(machines,sites,500_000)=={'a-flexible':'z-small','z-restricted':'a-large'}
    assert assign_sites(machines[::-1],sites[::-1],500_000)=={'a-flexible':'z-small','z-restricted':'a-large'}


def test_assignment_preserves_counts_and_stable_ties():
    from fab_sim.reference_layout import assign_sites
    sites=[site('s1',group='B'),site('s2',x=10_000_000),site('s3',x=20_000_000)]
    machines=[instance('a'),instance('b',group='B')]
    original=deepcopy((machines,sites))
    result=assign_sites(machines,sites,500_000)
    assert result=={'a':'s2','b':'s1'}
    assert (machines,sites)==original


def test_incompatible_footprint_reports_group_and_sites():
    from fab_sim.reference_layout import assign_sites
    with pytest.raises(ValueError,match='A.*oversized.*small'):
        assign_sites([instance('oversized',width=3_000_000)],[site('small')],500_000)


def test_duplicate_site_or_missing_service_role_rejects():
    from fab_sim.reference_layout import assign_sites,extract_sites
    with pytest.raises(ValueError,match='duplicate'):
        assign_sites([instance('a')],[site('s'),site('s')],500_000)
    model={'equipment':[{'id':'s','kind':'ordinary','tool_group_id':'A','bay_id':'Bay',
                        'x_um':0,'y_um':0,'width_um':2_000_000,'height_um':2_000_000}],
           'docks':[{'id':'d','equipment_id':'s','role':'IN','rail_id':'rail','offset_um':1_000_000}],
           'rails':[{'id':'rail','length_um':10_000_000}]}
    with pytest.raises(ValueError,match='s.*service'):
        extract_sites(model,500_000)


def test_extract_sites_preserves_original_pose_and_ports():
    from fab_sim.reference_layout import extract_sites
    model={'equipment':[{'id':'s','kind':'ordinary','tool_group_id':'A','bay_id':'Bay',
                        'x_um':100,'y_um':200,'width_um':2_000_000,'height_um':3_000_000}],
           'docks':[{'id':'d','equipment_id':'s','role':'BOTH','rail_id':'rail','offset_um':1_000_000}],
           'rails':[{'id':'rail','length_um':10_000_000}]}
    original=deepcopy(model)
    result=extract_sites(model,500_000)
    assert len(result)==1
    assert (result[0]['x_um'],result[0]['y_um'],result[0]['orientation'])==(100,200,'source_xy')
    assert result[0]['source_docks']==model['docks']
    assert model==original


def test_clearance_violation_names_conflicting_objects():
    from fab_sim.reference_layout import validate_placement
    sites=[site('s1'),site('s2',x=2_499_999)]
    with pytest.raises(ValueError,match='clearance.*s1.*s2'):
        validate_placement({'equipment':[]},[instance('a'),instance('b')],sites,{'a':'s1','b':'s2'},500_000)
    sites[1]['x_um']=2_500_000
    validate_placement({'equipment':[]},[instance('a'),instance('b')],sites,{'a':'s1','b':'s2'},500_000)


def test_placement_rejects_preserved_infrastructure_collision():
    from fab_sim.reference_layout import validate_placement
    stocker={'id':'stocker','kind':'stocker','x_um':1_000_000,'y_um':0,
             'width_um':2_000_000,'height_um':2_000_000}
    with pytest.raises(ValueError,match='clearance.*s1.*stocker'):
        validate_placement({'equipment':[stocker]},[instance('a')],[site('s1')],{'a':'s1'},500_000)


def test_placement_rejects_duplicate_or_missing_machine_assignment():
    from fab_sim.reference_layout import validate_placement
    machines=[instance('a'),instance('b')]
    with pytest.raises(ValueError,match='assignment'):
        validate_placement({'equipment':[]},machines,[site('s')],{'a':'s','b':'s'},500_000)
    with pytest.raises(ValueError,match='assignment'):
        validate_placement({'equipment':[]},machines,[site('s')],{'a':'s'},500_000)


@pytest.mark.parametrize('height,other_height',[(3_000_000,3_000_000),(3_000_001,3_000_000),(3_000_000,3_000_001)])
def test_derived_positions_resolve_overlap_without_moving_ports(height,other_height):
    from fab_sim.reference_layout import derive_sites, validate_placement
    model={'nodes':[{'id':'n0','x_um':0,'y_um':0},{'id':'n1','x_um':0,'y_um':15_000_000}],
           'rails':[{'id':'rail','from_node':'n0','to_node':'n1','bay_id':'Bay','length_um':15_000_000}],
           'equipment':[
               {'id':s,'kind':'ordinary','tool_group_id':'A','bay_id':'Bay','x_um':2_000_000,
                'y_um':y,'width_um':2_000_000,'height_um':3_000_000}
               for s,y in [('s1',5_000_000),('s2',7_000_000)]],
           'docks':[{'id':s+':dock','equipment_id':s,'role':'BOTH','rail_id':'rail','offset_um':y}
                    for s,y in [('s1',5_000_000),('s2',7_000_000)]]}
    original=deepcopy(model)
    machines=[instance('a',height=height),instance('b',height=other_height)]
    sites,assignment=derive_sites(model,machines,500_000)
    validate_placement(model,machines,sites,assignment,500_000)
    assert model==original
    assert {d['id']:d['offset_um'] for s in sites for d in s['source_docks']}=={'s1:dock':5_000_000,'s2:dock':7_000_000}
    assert 2*abs(sites[0]['y_um']-sites[1]['y_um'])>=height+other_height+1_000_000


def test_derived_positions_report_capacity_failure_without_stretching():
    from fab_sim.reference_layout import derive_sites
    model={'nodes':[{'id':'n0','x_um':0,'y_um':0},{'id':'n1','x_um':0,'y_um':2_000_000}],
           'rails':[{'id':'rail','from_node':'n0','to_node':'n1','bay_id':'Bay','length_um':2_000_000}],
           'equipment':[{'id':'s','kind':'ordinary','tool_group_id':'A','bay_id':'Bay','x_um':0,
                         'y_um':1_000_000,'width_um':2_000_000,'height_um':2_000_000}],
           'docks':[{'id':'d','equipment_id':'s','role':'BOTH','rail_id':'rail','offset_um':1_000_000}]}
    with pytest.raises(ValueError,match='placement search.*oversized'):
        derive_sites(model,[instance('oversized',height=3_000_000)],500_000)


def test_ports_relocate_beside_body_on_existing_rail():
    from fab_sim.reference_layout import relocate_ports
    model={'nodes':[{'id':'n0','x_um':0,'y_um':0},{'id':'n1','x_um':0,'y_um':20_000_000}],
           'rails':[{'id':'r','from_node':'n0','to_node':'n1','bay_id':'Bay','length_um':20_000_000}],
           'traffic':{'common_half_um':500_000,'passages':[]}}
    s=site('s',x=2_000_000);s['y_um']=10_000_000
    s['source_docks']=[{'id':'d','role':'BOTH','rail_id':'r','offset_um':1_000_000}]
    ports,mapping=relocate_ports(model,[s],{'a':'s'})
    assert ports[0]['equipment_id']=='a'
    assert ports[0]['rail_id']=='r' and ports[0]['offset_um']==10_000_000
    assert mapping[0]['source_offset_um']==1_000_000
    assert mapping[0]['offset_um']==10_000_000


def test_derived_positions_avoid_unserviceable_junction_gap():
    from fab_sim.reference_layout import derive_sites,relocate_ports
    model={'nodes':[{'id':str(k),'x_um':0,'y_um':y} for k,y in enumerate([0,9_000_000,11_200_000,20_000_000])],
           'rails':[{'id':str(k),'from_node':str(k),'to_node':str(k+1),'bay_id':'Bay',
                     'length_um':length} for k,length in enumerate([9_000_000,2_200_000,8_800_000])],
           'traffic':{'common_half_um':500_000,'passages':[{'rail_ids':['1']}]},
           'equipment':[{'id':'s','kind':'ordinary','tool_group_id':'A','bay_id':'Bay','x_um':2_000_000,
                         'y_um':10_000_000,'width_um':2_000_000,'height_um':2_000_000}],
           'docks':[{'id':'d','equipment_id':'s','role':'BOTH','rail_id':'0','offset_um':8_000_000}]}
    sites,assignment=derive_sites(model,[instance('a')],500_000)
    ports,_=relocate_ports(model,sites,assignment)
    assert ports and sites[0]['y_um']!=10_000_000
