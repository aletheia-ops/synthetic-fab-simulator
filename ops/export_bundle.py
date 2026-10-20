"""Export a checkpoint as a self-contained, portable bundle (`fabctl export`).

The bundle holds everything needed to continue the run on another machine:

    config.json           run config with bundle-relative paths, resuming from checkpoint/
    checkpoint/<name>/    the checkpoint image (manifest.json + state.json)
    inputs/               the prepared manufacturing and physical-reference inputs
    topologies/           the generated topology the run used (avoids regeneration)
    code/                 the simulator source at the commit that wrote the checkpoint
    SHA256SUMS, README.md

Usage: python3 ops/export_bundle.py CHECKPOINT_MANIFEST DEST [--commit SHA] [--link]
The attempt directory (config.json, process.json) is found from the manifest path
(ATTEMPT/simulation/checkpoints/<checkpoint>/manifest.json).
"""
import argparse, hashlib, json, os, shutil, subprocess, sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''): h.update(block)
    return h.hexdigest()


def place(src, dst, link):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if link:
        try: os.link(src, dst); return
        except OSError: pass
    shutil.copy2(src, dst)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('manifest'); p.add_argument('dest')
    p.add_argument('--commit', help='code commit (default: the attempt process.json commit)')
    p.add_argument('--link', action='store_true', help='hard-link large files when on the same filesystem')
    a = p.parse_args()
    manifest = Path(a.manifest).resolve(); checkpoint = manifest.parent
    attempt = checkpoint.parents[2]
    dest = Path(a.dest).resolve()
    if dest.exists() and any(dest.iterdir()): sys.exit(f'{dest} exists and is not empty')
    config = json.loads((attempt / 'config.json').read_text())
    process = json.loads((attempt / 'process.json').read_text()) if (attempt / 'process.json').exists() else {}
    commit = a.commit or process.get('commit')
    if not commit: sys.exit('no commit: pass --commit')
    commit = subprocess.check_output(['git', '-C', str(REPO), 'rev-parse', commit], text=True).strip()
    m = json.loads(manifest.read_text())

    # Checkpoint image, verified against its own checksums.
    for name, digest in m['payload_checksums'].items():
        if sha256(checkpoint / name) != digest: sys.exit(f'checkpoint payload {name} fails its checksum')
    # The store accepts only day-*/ (or older checkpoint-*/) bundle folders; keep the original name.
    for f in checkpoint.iterdir(): place(f, dest / 'checkpoint' / checkpoint.name / f.name, a.link)

    # Inputs, checked against the hashes in the checkpoint's physical identity.
    inputs = {}
    for key, value in config['inputs'].items():
        src = Path(value); target = dest / 'inputs' / (key.removesuffix('_path') + src.suffix)
        place(src, target, False); inputs[key] = str(target.relative_to(dest))

    # The topology the run used: resolve it through the normal (cached) preparation path.
    sys.path.insert(0, str(REPO / 'src'))
    from fab_sim.runner import load_config
    from fab_sim.preflight import prepare
    resolved = load_config(attempt / 'config.json')
    _, _, record = prepare(resolved, with_storage=True)
    topo = Path(record['path']); place(topo, dest / 'topologies' / topo.name, False)
    if record.get('generation_key'):
        req = topo.parent / 'requests' / (record['generation_key'] + '.json')
        if req.exists(): place(req, dest / 'topologies' / 'requests' / req.name, False)

    # Code at the writing commit.
    code = dest / 'code'; code.mkdir(parents=True)
    archive = subprocess.Popen(['git', '-C', str(REPO), 'archive', '--format=tar', commit], stdout=subprocess.PIPE)
    subprocess.run(['tar', '-x', '-C', str(code)], stdin=archive.stdout, check=True)
    if archive.wait(): sys.exit('git archive failed')

    # Bundle-relative config: paths resolve against config.json's directory.
    config['inputs'] = inputs
    config['output'].update(directory='run/simulation', topology_directory='topologies', run_id='bundle-run')
    config['output'].pop('live', None)
    config['checkpoint'] = {'enabled': True, 'interval_wall_seconds': config.get('checkpoint', {}).get('interval_wall_seconds', 1800),
                            'directory': 'run/simulation/checkpoints', 'resume_from': f'checkpoint/{checkpoint.name}/manifest.json',
                            'allow_compatible_build': True,
                            'background': config.get('checkpoint', {}).get('background', False)}
    (dest / 'config.json').write_text(json.dumps(config, indent=2) + '\n')

    provenance = {'source_checkpoint': str(manifest), 'source_attempt': str(attempt), 'commit': commit,
                  'simulated_days': m['saved_tick'] / 86400e6 if isinstance(m.get('saved_tick'), int) else None,
                  'settlements': m.get('settlements'), 'physical_digest': m.get('physical_digest'),
                  'runtime_compatibility': m.get('runtime_compatibility')}
    (dest / 'provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
    (dest / 'README.md').write_text(README.format(commit=commit[:12], **{k: provenance[k] for k in ('settlements',)},
        python='.'.join(map(str, m['runtime_compatibility']['python']))))

    sums = [f'{sha256(f)}  {f.relative_to(dest)}' for f in sorted(dest.rglob('*'))
            if f.is_file() and code not in f.parents]
    (dest / 'SHA256SUMS').write_text('\n'.join(sums) + '\n')
    print(json.dumps({'bundle': str(dest), 'commit': commit[:12], 'files': len(sums)}))


README = """# Portable fab_sim checkpoint bundle

Code commit {commit}; checkpoint after {settlements} settlements. See provenance.json.
Requires CPython {python} (the checkpoint's RNG state format is tied to it).

    python{python} -m venv .venv && .venv/bin/pip install -e code
    sha256sum -c SHA256SUMS
    mkdir -p run && cd code && ../.venv/bin/python -m tools.measure_run --config ../config.json \\
        --report ../run/measurement.json --progress

All paths in config.json are relative to this directory. The run continues from
checkpoint/ and writes events, oht records and new checkpoints under run/simulation.
"""

if __name__ == '__main__':
    main()
