# Data notice

**English** | [한국어](DATA_NOTICE.ko.md)

The simulator code in this repository is MIT licensed (see `LICENSE`). The files listed
below are **not** covered by that license. They are derived from two public research
testbeds and are redistributed here only so that the simulator can be run and its results
reproduced without first preparing the source workbooks. All rights in the underlying data
remain with the original authors. This project grants no license to them.

## Derived files

| File | Derived from | What this project changed |
| --- | --- | --- |
| `layout/manufacturing.json` | SMT2020 Dataset 1 (HVLM): products, routes, tool groups, processing and setup times, downtime and PM data | Converted from the workbook into a validated JSON model; content unchanged |
| `layout/reference.json` | SMAT2022 LVHM_E: rail network, bays, stockers, ports, vehicles | Converted into a validated JSON physical reference; service clearances applied |
| `layout/topologies/*.json` | SMAT2022 geometry + SMT2020 tool groups | Generated tool placement for Dataset 1, adjusted equipment counts and rack capacities, fleet of 200 vehicles at 5 m/s |
| `layout/topology.svg` | The topology above | A rendered plan of that topology |
| `baseline/checkpoint/**` | All of the above | A simulated factory state (day 60.4 of a run) that embeds the routes, tool groups and topology |

## Sources and citation

If you use these files or results produced with them, please cite the original work:

- **SMT2020:** D. Kopp, M. Hassoun, A. Kalir and L. Mönch, "SMT2020—A Semiconductor
  Manufacturing Testbed," *IEEE Transactions on Semiconductor Manufacturing*, 33(4), 2020.
  DOI: [10.1109/TSM.2020.3001933](https://doi.org/10.1109/TSM.2020.3001933).
  Data: <https://p2schedgen.fernuni-hagen.de/index.php/downloads/simulation>
- **SMAT2022:** K. Lee, S. Song, D. Chang and S. Park, "A New AMHS Testbed for
  Semiconductor Manufacturing," *Proceedings of the 2022 Winter Simulation Conference*.
  Paper: <https://informs-sim.org/wsc22papers/339.pdf>.
  Data: <https://github.com/kwoo-lee/SMAT2022>

BibTeX entries are in the README. The original workbooks are not redistributed; preparation
(`fab_sim.cli prepare-*`) records their hashes in the prepared files' provenance.

## Removal

If you are a rights holder and want any of these files removed, please open an issue in
this repository and they will be taken down.
