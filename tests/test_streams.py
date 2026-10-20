"""External stream projection: OHT sensor events and trimmed transfer events."""
from types import SimpleNamespace
from fab_sim.streams import StreamProjection, TRANSFER_INTERNAL


def projection():
    traffic=SimpleNamespace(
        model={'nodes':[{'id':'a','x_um':0,'y_um':0},{'id':'b','x_um':1000,'y_um':3000}]},
        rails={'r':{'from_node':'a','to_node':'b','length_um':2000}},
        vehicles={'v':{'rail_id':'r','offset_um':500,'carrier_slot_id':'slot:v'}},
        requests={'t1:drop':{'vehicle_id':'v'}},hops={})
    custody=SimpleNamespace(slots={'slot:v':{'carrier_id':'c1'}})
    transfers=SimpleNamespace(demands={'t1':{'origin_id':'p0','destination_id':'p1'}})
    return StreamProjection(traffic,transfers,custody)


def test_pose_interpolates_along_the_rail_chord():
    assert projection().pose('r',1000)=={'rail_id':'r','offset_um':1000,'x_um':500,'y_um':1500}


def test_hop_start_becomes_a_segment_and_hop_arrival_is_dropped():
    p=projection()
    hop={'vehicle_id':'v','request_id':'t1:drop','parts':[{'rail_id':'r','start_um':500,'end_um':1500}],
         'target_pose':{'rail_id':'r','offset_um':1500},'started_at_us':10,'duration_us':7,'distance_um':1000}
    [(stream,kind,body)]=p.project('traffic_hop_started',hop)
    assert (stream,kind)==('oht','oht_segment')
    assert body['task']=='drop' and body['transfer_id']=='t1' and body['carrier_id']=='c1'
    assert body['expected_arrival_us']==17 and body['to']['x_um']==750
    assert p.project('traffic_hop_arrived',hop)==[]


def test_blocked_loaded_and_idle_vehicles_share_one_event():
    p=projection();blockers=[{'kind':'rail','id':'r','owner':'w'}]
    [(_,kind,loaded)]=p.project('traffic_wait_changed',{'request_id':'t1:drop','blockers':blockers})
    [(_,idle_kind,idle)]=p.project('traffic_idle_wait_changed',{'vehicle_id':'v','blockers':blockers})
    assert kind==idle_kind=='oht_blocked'
    assert loaded['vehicle_id']==idle['vehicle_id']=='v' and loaded['blocked_by']==idle['blocked_by']==blockers
    assert loaded['task']=='drop' and idle['task']=='clear'


def test_transfer_events_are_trimmed_and_hoists_go_to_the_oht_stream():
    p=projection()
    record={'id':'t1','carrier_id':'c1','lot_id':'l1','vehicle_id':'v','origin_id':'p0','destination_id':'p1',
            'status':'delivered','loaded_path':{'sublinks':[]},'destinations':['p1'],'pickup_pose':{},
            'drop_pose':{},'dock_claims':[],'rack_owner':None}
    out=p.project('transfer_delivered',record)
    assert [(s,k) for s,k,_ in out]==[('events','transfer_delivered'),('oht','oht_unload_completed')]
    assert not set(out[0][2])&TRANSFER_INTERNAL and out[0][2]['carrier_id']=='c1'
    assert out[1][2]['port_id']=='p1'
    [(stream,kind,body)]=p.project('transfer_handling_started',{**record,'status':'pickup_handling'})
    assert (stream,kind,body['port_id'])==('oht','oht_load_started','p0')


def test_factory_events_pass_through_unchanged():
    payload={'lot_id':'l1'}
    assert projection().project('lot_admitted',payload)==[('events','lot_admitted',payload)]
