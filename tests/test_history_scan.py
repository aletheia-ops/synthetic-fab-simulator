import zlib
"""HistoryArchive.scan: same chain checks as records(), without decoding records."""
from fractions import Fraction
import pytest
from fab_sim.checkpoint_format import CheckpointError, decode_tagged
from fab_sim.history_archive import HistoryArchive


def chain(tmp_path):
    a = HistoryArchive(tmp_path / 'h', max_records=3)
    head = None
    for n in range(4):
        records = [{'kind': 'k', 'id': f'{n}:{i}', 'record': {'x': Fraction(n, i + 1), 'tags': {'a', 'b'}, 'n': None}}
                   for i in range(3)]
        head = a.append(head, records)
    return a, head


def test_scan_matches_decoded_records(tmp_path):
    a, head = chain(tmp_path)
    decoded = list(a.records(head))
    scanned = list(a.scan(head))
    assert [(r['kind'], r['id']) for r in decoded] == [(k, i) for k, i, _ in scanned]
    assert [r['record'] for r in decoded] == [decode_tagged(t) for _, _, t in scanned]
    assert len(scanned) == 12


@pytest.mark.parametrize('damage', ['digest', 'records', 'payload'])
def test_scan_rejects_tampered_chains(tmp_path, damage):
    a, head = chain(tmp_path)
    if damage == 'digest':head = {**head, 'logical_digest': '0' * 64}
    elif damage == 'records':head = {**head, 'records': head['records'] + 1}
    else:
        path = a._segment_path(head['head'])
        path.write_bytes(zlib.compress(a._read(path).replace(b'"3:0"', b'"3:9"')))
    with pytest.raises(CheckpointError):
        list(a.scan(head))


def test_identities_match_scan_and_fall_back_on_lookalike_records(tmp_path):
    a = HistoryArchive(tmp_path / 'h', max_records=3)
    head = a.append(None, [{'kind': 'k', 'id': 'plain', 'record': {'v': 1}}])
    # A record whose content contains the record-start pattern forces the full parse.
    head = a.append(head, [{'kind': 'k', 'id': 'nested', 'record': {'inner': {'id': 'x', 'kind': 'y', 'record': {}}}},
                           {'kind': 'k', 'id': 'quote"d', 'record': {}}])
    assert list(a.identities(head)) == [(k, i) for k, i, _ in a.scan(head)]
    assert [i for _, i in a.identities(head)] == ['nested', 'quote"d', 'plain']


def test_identities_reject_corruption_and_missing_segments(tmp_path):
    a, head = chain(tmp_path)
    path = a._segment_path(head['head'])
    original = path.read_bytes()
    path.write_bytes(zlib.compress(a._read(path).replace(b'"3:0"', b'"3:9"')))
    with pytest.raises(CheckpointError, match='checksum'):list(a.identities(head))
    path.write_bytes(original)
    with pytest.raises(CheckpointError, match='incomplete|inventory'):list(a.identities({**head, 'records': head['records'] + 1}))
    path.unlink()
    with pytest.raises(CheckpointError, match='unavailable'):list(a.identities(head))
