# Run settings

A run is configured by one JSON file (see `examples/` and `baseline/config.json`). Unknown keys are
rejected, so a misspelt setting fails instead of being ignored. Relative paths resolve against the
config file's directory.

Everything about the simulated fab is in this file. A few run-time options can also be given on the
command line, and then win over the file:

| Where | Options |
| --- | --- |
| `fab_sim.cli run`, `tools/measure_run.py` | `--resume-from MANIFEST` (= `checkpoint.resume_from`), `--checkpoint-interval-wall-seconds S` (enables checkpoints at that interval), `--progress`, `--progress-interval S`; `measure_run` also `--report`, `--cpu-affinity` |
| `ops/fabctl launch` / `start` | Writes each attempt's own copy of the configuration (`attempts/<attempt>/config.json`) with its output paths, the checkpoint options (`--checkpoint-interval`, `--checkpoint-background`, `--checkpoint-retain-daily`, `--checkpoint-retain-all`) and policy changes from `--set KEY=JSON`; `launch` drops `execution.max_settlements`. Options carry over to later attempts |

The resolved configuration of every run, with all defaults filled in, is recorded in its
`manifest.json` (`resolved_config`).

| Group | Contents |
| --- | --- |
| `inputs` | `manufacturing_path`, `physical_reference_path` (prepared JSON, see [layout.md](layout.md)); optional `topology_path` for a supplied topology instead of generation |
| `generation` | How the topology is built: equipment `counts`, `rack_capacities`, `fleet_count`, layout policy and its parameters (see [layout.md](layout.md)) |
| `workload` | `count` (a fixed cohort; omit for ongoing release), `selected_streams`, `product_weights`, `release_time_scale` |
| `execution` | `master_seed`, `horizon_us`, stopping limits and the policy settings below |
| `replay` | `rate`: `null` runs as fast as possible; a number paces output in simulated seconds per wall second |
| `output` | `directory`, `topology_directory` (topology cache; default `topologies/` at the repository root), `run_id`, record/chunk limits. `live` and `publication` configure live publication through a publisher adapter; they are available only when the simulator is called from Python (`fab_sim.runner.run(..., publication_adapter=...)`), not from the command line |
| `probes` | Timing probes: `enabled`, `sample_every` (time every Nth handler), `recent_limit` (recent traces kept for a failure report) |
| `checkpoint` | Periodic checkpoints and resume (below) |
| `injected_faults` | Optional scheduled unavailability of a tool, vehicle, rail or port |

## Workload

| Key | Meaning |
| --- | --- |
| `count` | A fixed cohort: release this many LOTs, split over products by `product_weights` (source mix by default), then stop releasing. Omit it for ongoing release: the release streams run until the horizon (the full-scale fab) |
| `product_weights` | Product mix of released LOTs, e.g. `{"Product_3": "0.5", "Product_4": "0.5"}` (default: the source mix) |
| `selected_streams` | SMT2020 release streams: `Regular_Lot_3`, `Regular_Lot_4`, `HotLot_3`, `HotLot_4`, `SuperHotLot` |
| `release_time_scale` | Multiplies release intervals (decimal string). `"1"` is the source rate; `"1.075"` releases at 93% of it |
| `dual_route_validation` | `true` rejects a cohort that would not release LOTs of both products (a guard for short test runs) |

## Execution: stopping and reproducibility

| Key | Default | Meaning |
| --- | --- | --- |
| `master_seed` | 7 | Seed of every random stream (streams are independent per scope) |
| `horizon_us` | required | Simulated time limit in microseconds (180 days = `15552000000000`) |
| `max_settlements` | none | Stop after this many scheduler settlements (for bounded tests) |
| `exit_targets` | none | Stop when these many LOTs per product have exited, e.g. `{"Product_3": 3}` |
| `replication_salt` | none | Positive integer mixed into the seed of random streams created after a start or resume; gives independent replications from one checkpoint |
| `transition_budget`, `derived_cache_budget_mib` | 100000, 512 | Internal limits; leave as is |

## Execution: policy settings

These are the project's own choices where the sources leave room. All are off unless set, and all
are recorded in the checkpoint's physical identity: resuming a checkpoint with a different value
needs `checkpoint.allow_policy_change: true` (`fabctl start --set KEY=JSON`) and is recorded in the
run manifest. The production column is the value in `baseline/config.json`.

| Key | Production | What it does |
| --- | --- | --- |
| `setup_policy` | `group_least_setup` | When a tool with a minimum-run setup (implant gases) may change recipe. `staged_campaign` (default): only after a full minimum run of LOTs is staged at the tool. `switch_on_empty`: as soon as no work for the installed recipe is bound to the tool. `group_least_setup`: only when no LOT of at least the asking LOT's priority waits for the installed recipe anywhere in the group, and destinations are ranked by least setup first (SMT2020 AutoSched ranking: priority, least setup, FIFO; minimum run 7) |
| `setup_minimum_runs` | — | Override minimum runs per setup name |
| `hop_lookahead_mm` | 30000 | A vehicle may reserve through holding points along clear track up to this distance (no extra junctions), instead of one 2 m holding pitch at a time. Same arrival times on clear track, far fewer events |
| `bay_entry_divert` | true | A vehicle stuck at a bay entrance behind blocked vehicles is re-routed around through the interbay and approaches again (at most 4 times per movement) |
| `queue_divert` | true | A vehicle waiting ≥ 60 s behind a stuck queue takes a free alternative branch (detour ≤ 900 s, ≤ 8 amendments per movement) |
| `congestion_routing_s` | 20 | Plan transfer legs on free-flow time plus this many seconds per vehicle waiting on each rail entered |
| `congestion_destination_s` | 60 | Among equivalent destinations (equally loaded tools, overflow buffers), add this many seconds per vehicle waiting in the destination's zone |
| `dispatch_metering` | 6 | Do not dispatch a non-super-hot transfer into a zone where this many vehicles already wait; the LOT waits at its origin instead of on the shared interbay |
| `busy_vehicle_cap` | 120 | While this many vehicles carry transfers, further non-super-hot dispatches wait (a guard against congestion feedback) |
| `buffer_selection` | `next_group_bays` | Overflow storage choice: `nearest_origin` (default) or buffers in the bays of the next step's tool group first, then neighbouring bays |
| `down_tool_output` | true | A finished LOT on a down tool's port can be picked up (load ports work while the tool is down) |
| `down_tool_avoid_s` | 7200 | Do not deliver to a tool in a downtime of at least this many seconds; the LOT goes to a peer or waits in a buffer |
| `super_hot_reservation` | true | A tool holds its next start while a super-hot LOT is travelling to it (SMT2020 `SuperHotLotFIRST_and_Reservation`) |
| `super_hot_expedite` | true | Super-hot LOTs may start a batch below its minimum, and vehicles are assigned in LOT priority order |
| `extra_processing` | off | Mock-inefficiency hook: adds extra processing time to chosen steps with a probability (ground truth in `diagnostics.jsonl` only). Rules: route (optionally area, tool group, source step), probability, relative or absolute extra time |

## Checkpoints

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | false | Write checkpoints |
| `interval_wall_seconds` | 300 | Wall-clock seconds between checkpoints |
| `directory` | `<output>/checkpoints` | Where checkpoints go |
| `retain_completed` | 3 | Newest checkpoints kept |
| `retain_daily` | false | Also keep the first checkpoint of every simulated day for the last 14 simulated days, and of every simulated week before that |
| `resume_from` | none | A checkpoint `manifest.json` to continue from |
| `allow_compatible_build` | false | Accept a checkpoint written by another build with the same state contract and Python/RNG runtime (recorded as `cross_build`) |
| `allow_policy_change` | false | Accept a checkpoint whose identity differs only in the policy settings above (recorded as `policy_changes`) |
| `background` | false (baseline: true) | Write checkpoints from a forked process: the run pauses 5–10 s instead of about a minute, but the writer needs about 1.15× the run's memory while it writes. Before each save the run checks free memory (including container and systemd limits) and saves in the foreground instead when less than 1.3× its own memory + 0.5 GB is available; such saves are marked `background_fallback` in the checkpoint metrics (see [running.md](running.md#checkpoint-memory)) |
| `verify_history` | false | On resume, also verify the retired-history archive chain (needs the `history/` directories of the run the checkpoint came from) |

A checkpoint resumes only with the same inputs, generation, workload, seed and policy settings (except
as allowed above), the same state contract and the same CPython minor version. It does not need the
source workbooks or any history archive.
