from itertools import product
import pytest


@pytest.mark.parametrize('closed',[False,True])
def test_directed_reachability_matches_route_solver_including_backward_offsets(closed):
    from fab_sim.routing import DirectedReachability,free_flow_path
    nodes=[{'id':str(i)} for i in range(3)]
    rails=[{'id':str(i),'from_node':str(i),'to_node':str((i+1)%3),'length_um':1000,
            'curve':False,'speed_multiplier':'1'} for i in range(3 if closed else 2)]
    model={'nodes':nodes,'rails':rails,'vehicle_types':[{'id':'v','speed_um_s':'1000'}],'policy':{}}
    check=DirectedReachability(model)
    poses=[{'rail_id':r['id'],'offset_um':offset} for r in rails for offset in (0,400,1000)]
    for a,b in product(poses,repeat=2):
        try:free_flow_path(model,a,b,'v');expected=True
        except ValueError:expected=False
        assert check.reachable(a,b)==expected
    with pytest.raises(ValueError,match='offset'):
        check.reachable(poses[0],{'rail_id':'0','offset_um':1001})
