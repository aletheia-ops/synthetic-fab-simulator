from copy import deepcopy
import pytest
from test_runner import fixture
from fab_sim.runner import build_runtime,load_config
from fab_sim.runtime_state import capture_runtime,restore_runtime
from fab_sim.checkpoint_format import CheckpointError
from fab_sim.progress import Progress


def test_progress_must_match_retained_lot_ledger():
    lots={'l':{'product_id':'p','admitted_at_us':0,'exited_at_us':10}}
    observer=Progress(lots,{},['p']);state=observer.export_state()
    state['counts']['p']['exited']=0
    with pytest.raises(CheckpointError):observer.restore_state(state)


def test_unknown_unreachable_native_object_is_rejected(tmp_path):
    config=load_config(fixture(tmp_path));owner=build_runtime(config,lambda *args:None,initialize=True)
    owner.scheduler.settle(0);state=capture_runtime(owner)
    state['nodes'].append(['UnsupportedOwner',{}])
    clone=build_runtime(config,lambda *args:None,initialize=False)
    with pytest.raises(CheckpointError):restore_runtime(clone,state,clone.actions)
