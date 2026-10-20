# Running the simulator

Commands run from the repository root. Requirements: Linux, WSL2 or macOS (native Windows is not
supported), CPython 3.12. A full-scale run uses about 3.5 GB of memory while running (4.5 GB at the peak of a restore) and
needs room for its checkpoint saves on top (see [Checkpoint memory](#checkpoint-memory)): 12 GB of
RAM or more for background saves; with less (8 GB at minimum), saves fall back to the foreground
automatically. It writes several GB of disk per
simulated day (see [outputs.md](outputs.md)).

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

## 1. Resume the baseline (no workbooks needed)

`baseline/` holds the full-scale fab at simulated day 60.4, and `layout/` holds the inputs and
topology it was built on. The run continues to the 180-day horizon with a checkpoint every 30 minutes.

For a long run use `fabctl` (section 5): `ops/fabctl launch baseline/config.json --name baseline`.
It handles stopping at a checkpoint, resuming and crash recovery for you.

To run it directly in the terminal instead:

```bash
.venv/bin/python tools/unpack_baseline.py
mkdir -p outputs/baseline
.venv/bin/python -m fab_sim.cli run --config baseline/config.json --progress 2> outputs/baseline/progress.jsonl
```

This writes to `outputs/baseline/simulation/`; follow it with `tail -f outputs/baseline/progress.jsonl`.
Ctrl-C stops it (work since the last checkpoint is lost). Continue later from the newest checkpoint
with `--resume-from outputs/baseline/simulation/checkpoints/<checkpoint>/manifest.json` and a new
`output.directory` (an output directory is never reused). See [baseline/README.md](../baseline/README.md) for what the state contains.

## 2. A short smoke run

`examples/smoke-run.json` is a reduced synthetic fab (`single_spine_slots_v1`: one tool per group on a
single rail spine, 7 vehicles), not the SMAT layout. It releases a 120-LOT cohort and stops at
5,000 settlements (about half a simulated day, a few seconds of wall time) with outcome `Incomplete`.

```bash
.venv/bin/python -m fab_sim.cli preflight --config examples/smoke-run.json
.venv/bin/python -m tools.measure_run --config examples/smoke-run.json \
  --report outputs/smoke-measurement.json --progress
```

Output directories and report files are never overwritten: to run it again, delete
`outputs/smoke-run/` and `outputs/smoke-measurement.json` first (or change `output.directory` and
`--report`).

Preflight is read-only: `ready` or `warnings` exit 0, `rejected` exits 2 with the reasons (missing
docks, batch quota shortages, unreachable deliveries and the like). A ready preflight does not prove
the run will flow.

`measure_run` writes the result JSON to stdout, progress records to stderr and the measurement report
to `--report`. `result.outcome` is `Completed` (finite cohort fully exited), `Incomplete` (horizon,
settlement limit or operator stop; normal for ongoing release), `Failed` or `Rejected`.

`baseline/config.json` is the full-scale production configuration (see [settings.md](settings.md)).
To try a variant, copy a config, change settings and `output.directory`, and run it.

## 3. Prepare inputs from the source workbooks

`layout/manufacturing.json` and `layout/reference.json` are already prepared. To prepare them yourself
(for example from a different SMT2020 dataset), download the workbooks (see
[DATA_NOTICE.md](../DATA_NOTICE.md)) into `data/` and run:

```bash
mkdir -p prepared
.venv/bin/python -m fab_sim.cli prepare-manufacturing --source data/SMT_2020_Model_Data_-_HVLM.xlsx --output prepared/manufacturing.json
.venv/bin/python -m fab_sim.cli prepare-topology --source data/SMAT_2022_Model_Data_-_LVHM_E.xlsx \
  --manufacturing prepared/manufacturing.json --profile layout/physical-profile.json --output prepared/reference.json
```

Preparation never overwrites an existing file. Point a config's `inputs` at the new files. The
topology is generated from the config's `generation` settings at the first run and cached under
`output.topology_directory` (default: `topologies/` at the repository root; keyed by inputs, settings
and generator code), so later runs and resumes reuse it. Tests that need the workbooks read `FAB_HVLM_WORKBOOK` and `FAB_SMAT_WORKBOOK` and skip
otherwise; `FAB_PREPARED_DIR` (a folder with prepared `manufacturing.json`, `reference.json` and
`topology.json`) lets the full-route tests skip preparation.

## 4. Checkpoints

With `checkpoint.enabled`, a run writes a checkpoint every `interval_wall_seconds` (at full scale the run pauses
about a minute per foreground save, 5–10 s per background save). Resume with `--resume-from MANIFEST` or
`checkpoint.resume_from`. A checkpoint resumes only with the same inputs, generation, workload, seed
and policy settings, and the same CPython minor version; the options for crossing builds or changing
a policy setting are in [settings.md](settings.md#checkpoints). A resumed run continues exactly as the
uninterrupted run would (same build, same settings).

### Checkpoint memory

Measured on the baseline (about 1,400 LOTs in process; the run itself uses 3.2 GB at day 60 and
3.6 GB at day 105, growing with WIP):

| `checkpoint.background` | Run pauses per save | Extra memory during a save |
| --- | --- | --- |
| `true` (baseline default) | 5–10 s | A forked writer that grows to about 1.15× the run's own memory (3.8 GB at day 60) for about 40 s, plus one more CPU core |
| `false` | about 1 minute | About 0.5–1 GB |

The forked writer starts sharing the run's memory, but Python's reference counting copies almost
every page it reads, so a background save peaks at about 2.2× the run's memory (about 8.5 GB of used
system memory for the baseline, measured).

With `background: true` the run checks before every save whether a writer fits: available memory
(`MemAvailable`, capped by any cgroup limit such as systemd `MemoryMax` or a container limit) must be
at least 1.3× the run's resident memory plus 0.5 GB. If it is not, that save runs in the foreground
and its entry in `checkpoint_metrics` carries `background_fallback` with the numbers; the next save
checks again. Where memory cannot be read (no `/proc`, e.g. macOS) the writer is forked. To always
save in the foreground, set `"background": false` (or `fabctl ... --checkpoint-background off`).

## 5. Long runs with fabctl (systemd)

`ops/fabctl` runs one long simulation as a systemd user service with checkpoints, crash recovery and
a watchdog, and lets you stop, resume, rewind or move it to new code:

```bash
ops/fabctl install
ops/fabctl launch baseline/config.json --name my-run
ops/fabctl status
```

See [ops/README.md](../ops/README.md) for runs and attempts, all commands and options, and what
happens after a crash.

## 6. Watching and analysing a run

| Tool | Use |
| --- | --- |
| `tools/static_capacity.py WORKBOOK.xlsx` | Before a run: static load per tool group from the SMT2020 workbook (download it first, see [DATA_NOTICE.md](../DATA_NOTICE.md)) |
| `tools/traffic_watch.py SOURCE TARGET_DAY [LIMIT_S] [--follow]` | During a run: OHT blocked seconds per delivery per 30 simulated minutes, with `BREAKDOWN`/`RECOVERED` lines. SOURCE is a run folder, an attempt or `outputs/live-ops/current_live.json` |
| `tools/run_report.py RUN_DIR` | Any time: throughput, cycle time, trends, workload of every tool group, where the WIP is, long downtimes; `--steady-state` adds a steady-state assessment, `--csv DIR` exports tables |
| `tools/render_topology.py TOPOLOGY.json -o plan.svg` | Floor plan of a topology |

`run_report` keeps a small state file (`RUN_DIR/report/state.json`), so repeated calls read only new
events: well under a second per call on a live run, about 30 MB of memory. The first call reads the
whole event log (a few minutes for a 70-day run). `traffic_watch` costs well under 1% of one core.
Both drop consumed file ranges from the page cache. Run them with `nice` next to a live simulation.

## CPU placement

`tools/measure_run.py --cpu-affinity` can pin a run to chosen CPUs (`performance` or explicit IDs;
`performance` is not available on WSL); the default `none` leaves placement to the OS.
