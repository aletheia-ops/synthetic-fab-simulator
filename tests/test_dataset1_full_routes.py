"""Full SMT2020 Dataset 1 routes on the default synthetic layout (needs the source workbooks).

Every source route operation is kept; runs are bounded by simulated time.
"""
import hashlib
import json
import os
from pathlib import Path
import pytest
from fab_sim.inputs import canonical_text,read_json
from fab_sim.sources.smt import prepare_manufacturing,validate_manufacturing
from fab_sim.sources.smat import prepare_topology
from fab_sim.generation import generate_topology,load_prepared
from fab_sim.runner import run


@pytest.fixture(scope='module')
def prepared(tmp_path_factory):
    hv=os.environ.get('FAB_HVLM_WORKBOOK');sm=os.environ.get('FAB_SMAT_WORKBOOK')
    if not hv or not sm:pytest.skip('supplied Dataset1/SMAT workbooks are external')
    assert hashlib.sha256(Path(hv).read_bytes()).hexdigest()=='11c5cc9f477eafcbb1d37abf2a6802eade5964a95fa1b1fd3351ddce841c1a18'
    assert hashlib.sha256(Path(sm).read_bytes()).hexdigest()=='8e74e6db2272ad7dbeeb26de2adb1cd69e27a7848fe7baa0524803fd317156c0'
    root=tmp_path_factory.mktemp('dataset1-full-source')
    cache=os.environ.get('FAB_PREPARED_DIR')
    if cache:
        m=validate_manufacturing(read_json(Path(cache)/'manufacturing.json'))
        reference=load_prepared(Path(cache)/'reference.json',m)
        topology=load_prepared(Path(cache)/'topology.json',m)
    else:
        m=prepare_manufacturing(Path(hv));reference=prepare_topology(Path(sm),m,{'stocker_capacity':8})
        topology=generate_topology(m,reference,{})
    assert m['provenance']['source_sha256']==hashlib.sha256(Path(hv).read_bytes()).hexdigest()
    assert reference['provenance']['source_sha256']==hashlib.sha256(Path(sm).read_bytes()).hexdigest()
    for name,value in [('manufacturing',m),('reference',reference),('topology',topology)]:
        (root/(name+'.json')).write_text(canonical_text(value))
    return root,m


def config(root,destination,*,horizon,generation=None,probes=True):
    inputs={'manufacturing_path':str(root/'manufacturing.json')}
    if generation is None:inputs['topology_path']=str(root/'topology.json')
    else:inputs['physical_reference_path']=str(root/'reference.json')
    c={'inputs':inputs,'workload':{'count':24,'product_weights':{'Product_3':'0.5','Product_4':'0.5'},
          'release_time_scale':'1','dual_route_validation':True},
       'execution':{'master_seed':7,'horizon_us':horizon},
       'probes':{'enabled':probes,'sample_every':100},
       'output':{'directory':str(destination),'run_id':destination.name,'live':False}}
    if generation is not None:c['generation']=generation
    path=destination.parent/(destination.name+'-config.json');path.write_text(canonical_text(c));return path


def test_complete_source_routes_and_native_maintenance_are_retained(prepared):
    _,m=prepared
    assert {r['product_id']:len(r['steps']) for r in m['model']['routes']}=={'Product_3':583,'Product_4':343}
    assert m['model']['maintenance'] and m['model']['breakdowns']
    assert all(r['steps'][0]['batch_min']==125 for r in m['model']['routes'])


def test_entry_capacity_8_admits_both_products_and_waits_reproducibly(prepared,tmp_path):
    root,_=prepared;digests=[]
    for enabled in (True,False):
        destination=tmp_path/('probed' if enabled else 'unprobed')
        result=run(config(root,destination,horizon=12000000000,probes=enabled))
        assert (result['outcome'],result['reason'],result['exited_count'])==('Incomplete','horizon',0)
        assert result['wip_count']==8 and result['pending_admission_count']>0
        rows=[json.loads(line) for line in (destination/'events.jsonl').read_text().splitlines()]
        admitted=[r['payload'] for r in rows if r['event_type']=='lot_admitted']
        assert {p:sum(l['product_id']==p for l in admitted) for p in ('Product_3','Product_4')}=={'Product_3':4,'Product_4':4}
        assert not any(r['event_type'] in ('batch_committed','processing_started') for r in rows)
        assert (destination/'diagnostics.jsonl').stat().st_size>0
        digests.append(result['domain_digest'])
    assert digests[0]==digests[1]


@pytest.mark.parametrize('fleet,horizon',[(1,14000000000),(None,14000000000)])
def test_entry_capacity_12_reaches_batch_processing_after_six_arrivals(prepared,tmp_path,fleet,horizon):
    root,m=prepared;before=m['validation']['content_sha256'];destination=tmp_path/'entry12-fleet1'
    generation={'entry_capacity':12,'exit_capacity':8}
    if fleet is not None:generation['fleet_count']=fleet
    result=run(config(root,destination,horizon=horizon,generation=generation))
    assert result['outcome']=='Incomplete' and result['reason']=='horizon'
    assert result['capacity']['entry']==12 and result['capacity']['fleet']==(1 if fleet==1 else 7)
    assert result['release_policy']['quotas']=={'Product_3':12,'Product_4':12}
    started=None;arrived=set()
    with (destination/'events.jsonl').open() as stream:
        for line in stream:
            event=json.loads(line);payload=event['payload']
            if event['event_type']=='batch_member_arrived':arrived.add(payload['visit_id'])
            if event['event_type']=='batch_processing_started':
                # Each product has twelve LOTs: five would leave an impossible
                # seven-LOT remainder, so both root batches must contain six.
                assert len(payload['members'])==6
                assert all(member['visit_id'] in arrived for member in payload['members'])
                started=payload
    assert started is not None
    assert validate_manufacturing(read_json(root/'manufacturing.json'))['validation']['content_sha256']==before
