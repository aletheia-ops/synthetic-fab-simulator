# Layout and topology

The physical fab is the SMAT2022 LVHM_E layout: a 300 × 153 m rail network with 40 one-way intrabay
loops, three interbay areas, 2,858 rail nodes, 17,760 one-carrier track buffers and 40 stockers with
eight spaces each. Rail coordinates, directions, lengths, curves, speed caps and zone control units
(ZCUs) are copied exactly from the source. What the project derives is where the SMT2020 Dataset 1
tools sit, their ports, and the vehicle fleet. `layout/topology.svg` shows the production topology;
`tools/render_topology.py` draws any topology.

## Layout policies (`generation.layout_policy`)

| Policy | Tool placement |
| --- | --- |
| `single_spine_slots_v1` (default) | A synthetic test fab, not the SMAT layout: tool slots along one rail spine, `counts` default to one tool per group. Used by `examples/smoke-run.json` and the test fixtures |
| `smat_reference_slots_v1` | Each Dataset 1 tool takes a source equipment site in the SMAT layout (which was designed for Dataset 4), with bodies packed into the source bays and transport-improving swaps between compatible sites |
| `smat_area_flow_v1` | Tools are assigned to bay columns for Dataset 1's own counts and flows. The score puts process-area cohesion first (as the SMAT paper's layout does: most bays hold one area), then flow-weighted loaded travel plus `balance_weight` × bay-load imbalance. A parallel move/swap search runs to a local optimum. `fit_tolerance` (default 0.02) applies to body width |

Further `generation` settings:

| Key | Meaning |
| --- | --- |
| `counts` | Tools per tool group (default: the source counts, 1,043 tools in 105 groups; one per group for `single_spine_slots_v1`) |
| `rack_capacities` | Carrier places at each tool group's ports |
| `fleet_count` | Number of OHT vehicles (required for the reference policies) |
| `vehicle_speed_mm_s` | Straight-rail speed; default 1,500 mm/s (workbook OHTV0). The baseline uses 5,000 mm/s, the SMAT2022 paper's value; the curve cap stays 1,000 mm/s. Acceleration is not modelled |
| `clearance_mm` | Clearance around machine bodies (default 500, a project choice) |
| `section_window_mm`, `section_max_swaps` | After area placement, exchange whole bays to flatten the expected handling load per window of track (production: 50 m windows, 40 swaps) |
| `entry_capacity`, `exit_capacity` | Carrier capacity of the fab's entry and exit stockers |
| `improvement_limit`, `balance_weight`, `fit_tolerance` | Search limits and weights of the placement |

Machine bodies use the maximum source width and height of their group; no body is shrunk to fit.
Ports sit on straight bay rails within the body's span, outside ZCU passages. Each stocker's eight
spaces map round-robin to its two service ports. Pickup and drop take 10 s each. Preflight checks
directed connectivity and capacities; it does not prove that traffic will flow at a given load.

## The topology cache (`output.topology_directory`)

Generating a full-scale topology takes minutes, so it is cached. A topology is saved as
`<content_sha256>.json` (complete model, validation binding, provenance) and registered under
`requests/<key>.json`, where the key covers the input hashes, the effective generation settings and
a fingerprint of the generator's source files. A run with the same inputs and settings reuses the
cached topology; changed settings or generator code generate a new one. A corrupt or mismatched
registration is an error, never a silent regeneration. Each run manifest records the topology used
(`topology_artifact`: path, content hash, key, cache hit).

`layout/topologies/` is the cache for the production configuration, so the baseline resumes without
generating anything. `tests/test_baseline_identity.py` fails if a change to the generator
code would make the shipped topology unreachable.

A config may instead give `inputs.topology_path` (a prepared topology file) and no `generation`
settings.
