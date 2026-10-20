# Layout inputs

**English** | [한국어](README.ko.md)

Prepared inputs for SMT2020 Dataset 1 on the SMAT2022 LVHM_E layout. They are derived from the
source workbooks; see [DATA_NOTICE.md](../DATA_NOTICE.md) for sources, citations and terms.

| File | Contents |
| --- | --- |
| `manufacturing.json` | SMT2020 Dataset 1 (HVLM): products, routes, tool groups, processing, setups, batching, maintenance, releases. Prepared with `fab_sim.cli prepare-manufacturing` |
| `reference.json` | SMAT2022 LVHM_E physical reference: rails, bays, stockers, ports, vehicle type. Prepared with `fab_sim.cli prepare-topology` and `layout/physical-profile.json` |
| `topologies/<sha256>.json` | The production topology: 1,050 tools placed by `smat_area_flow_v1`, 200 vehicles. Generated from the two files above and the `generation` settings of `baseline/config.json` |
| `topologies/requests/<key>.json` | Registration that lets a run with those settings find the topology instead of generating it |
| `topology.svg` | Floor plan of the production topology (`tools/render_topology.py`) |

Do not edit these files: their hashes are part of the baseline checkpoint's identity, and an edited
file is rejected rather than silently used. To use other inputs, prepare new files (see
[docs/running.md](../docs/running.md)) and point a configuration at them.
