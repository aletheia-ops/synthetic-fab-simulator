# Capacity of SMT2020 Dataset 1

**English** | [한국어](capacity.ko.md)

Dataset 1 (HVLM: Products 3 and 4, 105 tool groups, 1,043 tools) releases about 57 LOTs per day at the
source rate. Whether a configuration can carry that load decides whether WIP levels off or keeps
growing, so check capacity before reading anything else into a run.

## Static load

With the SMT2020 workbook downloaded to `data/` (see [DATA_NOTICE.md](../DATA_NOTICE.md)):

```bash
.venv/bin/python tools/static_capacity.py data/SMT_2020_Model_Data_-_HVLM.xlsx [--worst-setups]
```

compares demanded tool time per day with available tool time per group: sampling and rework visits;
lot, wafer and batch processing (a cascading tool counts each unit for max(interval, processing −
interval), as two slots in series); one minute each of loading and unloading; downtime from
breakdowns (MTTR / (MTTF + MTTR)) and calendar or counter PMs. Setups are not counted unless
`--worst-setups` charges every visit its setup (an upper bound). It is an estimate, not a queueing
model.

At the source rate no Dataset 1 group exceeds 100% statically, but 46 exceed 85%: wet etch and litho
sit at 92–97%, and LithoTrack at 97–98% with worst-case setups. Such a fab has very little slack.

## What runs show

Measured with `tools/run_report.py` on full-scale runs:

- **Setups are the hidden load.** Implant gas changes take 60–80 minutes with a minimum run of 7 LOTs.
  On Implant_128, which Product_3 visits 12 times per LOT (Product_4: 4), setups took 15–22% of tool
  time. At the source release rate, processing plus setups plus downtime exceeded the group's
  capacity, and Product_3's WIP grew without bound. The SMT2020 authors' own simulation also shows
  Product_3 cycle time still rising after a year at 100% starts (72.6 days at 6 months, 98.3 at 1
  year; arXiv 2302.07162).
- **Small groups suffer most from downtime.** With 2–4 tools, one long PM or breakdown removes a
  quarter to half of the group; queues build there even when the average load is moderate.
- **Transport is not the limit once the AMHS settings are on.** With the production AMHS settings,
  vehicles spend about 20 s blocked per delivery.

## The production configuration

`baseline/config.json` makes the source workload carry with small, declared changes:

- release at 93% of the source rate (`release_time_scale` 1.075);
- one more tool at Implant_128, Implant_91, Implant_132, Litho_FE_98 and Litho_FE_92, two more at
  Litho_FE_111 (1,050 tools);
- `setup_policy: group_least_setup` (the SMT2020 setup ranking) and `down_tool_avoid_s: 7200`.

On this configuration WIP stays at about 1,300–1,500 LOTs from day 38 to day 105, creeping by about
one LOT per day after day 64, with Product_3 cycle time about 33 days and Product_4 about 19 days, and
no traffic breakdown. The most loaded groups (Implant_128 with 15% setups, several
small dielectric and thin-film groups, Litho_BE_99 with 3 tools) run at 91–94% of tool time including
downtime and setups.
To reduce the remaining drift, add a tool to those groups or lower the release rate further.
