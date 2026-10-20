"""The shipped baseline config resumes the shipped checkpoint on this build.

Checks identity and the topology cache without loading the (large) checkpoint state:
the config's physical identity equals the manifest's, and the topology request key
computed by the current generator code is registered under layout/topologies, so a
resume finds the shipped topology instead of regenerating it.
"""
import json
from pathlib import Path

from fab_sim.checkpoint import STATE_CONTRACT, physical_identity
from fab_sim.checkpoint_format import decode_value, encode_value
from fab_sim.inputs import canonical_digest, read_json
from fab_sim.runner import load_config
from fab_sim.topology_store import _request

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'baseline/config.json'


def manifest_path(config):
    return (CONFIG.parent / json.loads(CONFIG.read_text())['checkpoint']['resume_from']).resolve()


def test_baseline_config_matches_checkpoint_identity():
    config = load_config(CONFIG)
    manifest = json.loads(manifest_path(config).read_text())
    assert manifest['state_contract'] == STATE_CONTRACT
    saved = decode_value(json.dumps(manifest['physical_identity']).encode())
    inputs = {k: Path(v) for k, v in config['inputs'].items() if v is not None}
    assert encode_value(saved) == encode_value(physical_identity(config, inputs))
    assert manifest['runtime_compatibility']['python'] == [3, 12]


def test_baseline_topology_is_registered_for_this_generator():
    config = load_config(CONFIG)
    manufacturing = read_json(Path(config['inputs']['manufacturing_path']))
    reference = read_json(Path(config['inputs']['physical_reference_path']))
    key = canonical_digest(_request(manufacturing, reference, config['generation']))
    entry = Path(config['output']['topology_directory']) / 'requests' / (key + '.json')
    assert entry.exists(), 'generator code changed: the shipped topology no longer matches (regenerate layout/topologies)'
    assert (entry.parent.parent / (json.loads(entry.read_text())['content_sha256'] + '.json')).exists()
