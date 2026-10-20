# Baseline fab state

A ready-to-continue state of the full-scale fab at **simulated day 60.40** (75,142,454 settlements),
and the configuration that continues it. Start here to get factory data without waiting for an empty
fab to fill up.

| | |
| --- | --- |
| Configuration | `config.json`: SMT2020 Dataset 1 at 93% of the source release rate, 1,050 tools (seven more than the source at the implant and litho bottlenecks), 200 OHTs at 5 m/s, and the transport and dispatch policies listed in [docs/settings.md](../docs/settings.md) (see [docs/capacity.md](../docs/capacity.md) for why) |
| State | About 1,400 LOTs in process; Product_3 and Product_4 exit at about their release rates |
| Checkpoint | `checkpoint/day-060.40/`: `manifest.json` and `state.json.xz` (33 MB; 625 MB unpacked) |
| Physical digest | `391f860473035d6244f86b4a1ee71cf472d80bd63ebdb3f3f0ac6ae4f4301c22` |
| Requires | CPython 3.12 (a checkpoint stores Python random-generator state); the inputs and topology in `../layout/` |

The fab is in stable operation at this point: no traffic breakdown (vehicles are blocked about 20 s
per delivery) and WIP bounded at about 1,300–1,420 LOTs. Continued from here, WIP stays between about
1,400 and 1,500 LOTs through day 105, creeping by about one LOT per day. Use
`tools/run_report.py --steady-state` on your continuation to judge steady state for your purpose.

## Continue it

As a supervised long run (Linux or WSL2 with systemd; see [ops/README.md](../ops/README.md)):

```bash
ops/fabctl install
ops/fabctl launch baseline/config.json --name baseline     # unpacks the state if needed
```

or directly in the terminal:

```bash
.venv/bin/python tools/unpack_baseline.py      # state.json.xz -> state.json, checksum verified
mkdir -p outputs/baseline
.venv/bin/python -m fab_sim.cli run --config baseline/config.json --progress 2> outputs/baseline/progress.jsonl
```

Restoring the state takes about 2 minutes on an otherwise idle machine (longer when the CPU is
busy) and 4.5 GB of memory at its peak; the run then uses about
3.2–3.6 GB. Checkpoints are saved every 30 minutes by a forked background writer (the run pauses
5–10 s), which needs about as much memory again for about 40 s (12 GB of RAM or more). When not enough
memory is free, a save runs in the foreground instead (pauses about a minute, needs about 1 GB
extra); see [docs/running.md](../docs/running.md#checkpoint-memory). The continued run's `manifest.json` records
where it came from under `continuation` (`cross_build` names the build that wrote the checkpoint when
yours differs) and `topology_artifact.cache_hit: true`.

The state refers to the history of LOTs that had already left the fab (an archive under
`/runs/production-v3/`); that archive is not included. A run does not read it; only the optional
`checkpoint.verify_history` audit does.
