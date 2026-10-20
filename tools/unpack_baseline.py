"""Unpack the shipped baseline checkpoint (state.json.xz -> state.json) and verify it.

The checkpoint store reads a plain state.json; this streams the decompression, checks the
SHA-256 recorded in the checkpoint's manifest.json and only then moves the file into place.

Usage: python tools/unpack_baseline.py [CHECKPOINT_DIR]
       (default: the single checkpoint directory under baseline/checkpoint/)
"""
import hashlib, json, lzma, os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    if len(sys.argv) > 1:
        directory = Path(sys.argv[1])
    else:
        found = sorted(d.parent for d in (ROOT / 'baseline/checkpoint').glob('*/manifest.json'))
        if len(found) != 1:
            sys.exit(f'expected one checkpoint directory under baseline/checkpoint, found {len(found)}')
        directory = found[0]
    expected = json.loads((directory / 'manifest.json').read_text())['payload_checksums']['state.json']
    target, packed = directory / 'state.json', directory / 'state.json.xz'
    if target.exists():
        h = hashlib.sha256()
        with open(target, 'rb') as f:
            for block in iter(lambda: f.read(1 << 20), b''):
                h.update(block)
        if h.hexdigest() == expected:
            print(f'{target} is already unpacked and verified')
            return
        sys.exit(f'{target} exists but does not match the manifest; remove it and run again')
    tmp, h, size = directory / 'state.json.tmp', hashlib.sha256(), 0
    try:
        with lzma.open(packed, 'rb') as src, open(tmp, 'wb') as dst:
            for block in iter(lambda: src.read(1 << 20), b''):
                h.update(block); dst.write(block); size += len(block)
        if h.hexdigest() != expected:
            print(f'checksum mismatch: got {h.hexdigest()}, manifest says {expected}', file=sys.stderr)
            sys.exit(2)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    print(f'unpacked {target} ({size / 1e6:.0f} MB), checksum verified')


if __name__ == '__main__':
    main()
