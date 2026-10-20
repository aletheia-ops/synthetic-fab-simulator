# Simulation model

What the simulator models and how: the fab it builds from the SMT2020 manufacturing model and the
SMAT2022 physical layout, how LOTs are released, processed and transported, how vehicles move, and
how time, randomness and outcomes work. Settings that switch behaviours are listed in
[settings.md](settings.md), output records in [outputs.md](outputs.md), the layout and tool placement
in [layout.md](layout.md).

The simulator is a single-process discrete-event simulation with one scheduler. It is a physically
consistent model of a fab that emits the events a real fab's systems would record. It does not
reproduce the exact performance of the source fabs or of the commercial engines used to publish them.
Where the sources leave a choice open, the choice made here is called a *project choice*.

## 1. Sources and what the simulator adds

| Source | Used for |
| --- | --- |
| SMT2020 Dataset 1 (HVLM) | Products and routes, release streams, processing times and units, tool eligibility, setups, batching, cascading, sampling, rework, dedication, CQT, breakdowns and PMs |
| SMAT2022 (LVHM_E layout) | Rail network, bays, junction control zones, stockers, ports and vehicle type |
| This project | Placement of the Dataset 1 tools in the layout, ports, racks and buffers, the vehicle fleet, and the policies described below |

Adaptations of the source data:

- **Transport replaces abstract delays.** SMT2020 models transport as a fixed delay step
  (`Delay_32`, plus fab-to-fab transport). Here those steps are replaced by real transport on the
  AMHS, with finite storage and waiting; they never run as timers on top of it. The original step IDs
  are kept, so dedication and CQT references still resolve.
- **Complete routes.** Routes keep all 583 (Product_3) and 343 (Product_4) source steps and their
  IDs. Sampling skips and rework follow the source rules; they are not a shortened route.
- **Data fixes, recorded.** The route workbook repeats the header `OFFSET` (processing and setup
  offsets); both columns are read by position. The layout's `DefMEt_FE_118` is read as
  `DefMet_FE_118`. Anything ambiguous or unsupported is rejected during preparation, not guessed.

## 2. Preparation and validation

A run reads prepared JSON inputs (`layout/manufacturing.json`, `layout/reference.json`) produced once
from the workbooks by `fab_sim.cli prepare-*`. Each prepared file carries its source hashes and a
validation record. The topology (tool positions, ports, rails, fleet) is generated from those inputs
and the run's `generation` settings, validated once and cached (see [layout.md](layout.md)); later
runs and resumes load it after checking its identity.

Validation covers coverage of every required tool group, unique and resolved references, tools and
ports fitting their bays, finite storage, directed access for every required transfer, traffic
resources, entry and exit connectivity, and the initial vehicle positions. An edited input fails its
identity check and must be prepared again. Validation proves structure, not that a given load will
flow without congestion.

While the simulation runs, cheap invariants are checked continuously: one owner per carrier,
reservations and occupancy consistent, no two vehicles in the same space.

## 3. The physical fab

- **Tools** stand in bays along one-way intrabay rail loops, sized by the largest source footprint of
  their group. Each tool has load ports on its rail and an internal **rack** of carrier places
  (`generation.rack_capacities`). A carrier stays in its rack slot while its wafers are processed.
- **Track buffers** are one-carrier storage places beside the rails; the layout's **stockers** hold
  eight carriers each. New LOTs are created at the **entry** stocker and finished LOTs leave through
  the **exit** stocker (capacities `entry_capacity`, `exit_capacity`).
- **Vehicles** (OHTs) carry one carrier each. The baseline fleet is 200 vehicles.

The full-scale fab is the SMAT2022 rail network with the 1,050 tools of the baseline placed by
process area and flow. Tests and the smoke run use a small synthetic single-spine fab. Both are
described in [layout.md](layout.md).

## 4. Release and admission

**Release streams.** Dataset 1 has five periodic streams: regular and hot LOTs of Product_3 and
Product_4, and super-hot LOTs of Product_3. Each keeps its source priority (regular 10, hot 20,
super-hot 30), 25 wafers per LOT, interval and due-date allowance. The first source start date is
simulation time zero. Engineering and variable-date releases are not modelled.

**Rate and mix.** `release_time_scale` multiplies every interval (1.075 releases at 93% of the
source rate). `product_weights` changes the product mix while keeping the total rate. Release times
are computed from the start time plus occurrence × interval, so rounding never accumulates.

**Cohort or ongoing.** With `workload.count`, a fixed cohort of that many LOTs is released (split
over products by largest remainder) and the run can complete. Without it, release goes on until the
horizon; this is the mode of the full-scale fab.

**Admission.** A release becomes a LOT only when the entry stocker has room: the LOT and its carrier
are created at admission, and its wait is recorded. The due date is release time plus the source
allowance; a late admission does not move it. Missing a due date has no effect on processing.

## 5. Manufacturing

### Visits and processing

A LOT works through its route as a sequence of **visits**. Rework and repeated steps get new visit
IDs, so the history is append-only.

- **Sampling** is decided once when a step becomes the next visit, before transport. A skipped step
  has no processing, dedication or rework.
- **Rework** is drawn once at successful processing completion; the whole LOT returns to the named
  source step. There is no retry limit; the horizon bounds a run.
- **Processing time** is sampled once per LOT, per batch or per wafer, according to the step's unit.
  Uniform source times are U(mean − offset, mean + offset); constant times use no random draw.
- **Cascading tools** have two internal slots in series. A unit (LOT or wafer) takes min(P, C) in the
  first slot and the rest of its processing time P in the second; a new unit can start after the
  cascade interval C, and only when the first slot is free. Setups and downtime pause the interval
  timer.

### Choosing the next LOT

Among the LOTs that can start on a tool now, the highest priority goes first, then the least expected
setup time, then the LOT that has waited longest at the tool, then the visit ID. Only LOTs that are
physically at the tool, eligible, compatible with dedication and setup rules count; a blocked
candidate does not stop others. Priority never interrupts work already started or committed, and
does not reserve tools ahead of time for LOTs still in transit (super-hot reservation can be turned
on, see [settings.md](settings.md)).

### Setups, batches, dedication

- **Setups** follow the source rules (setup needed or always), with exact transitions before
  wildcards. The setup time is sampled when the setup starts. Implant gas setups require seven
  completed LOTs before the next change. When a tool may change its setup is the `setup_policy`
  setting.
- **Batching** groups whole LOTs of the same product, step and setup. A batch tool starts once it has
  at least the batch minimum and takes the largest ready set that fits the maximum. Members are
  chosen and their rack places reserved before they are delivered, so incompatible LOTs cannot fill
  a batch tool. Membership is fixed once chosen.
- **Dedication** binds a LOT to the tool where it first processes a dedicated step; later steps of the
  same dedication chain must use that tool, for the LOT's whole life.
- **CQT** (queue-time limits) start at the declaring step's processing completion and close when the
  target step starts processing. A late target produces one `cqt_violation` event per limit; work
  continues (no scrap or priority change).

### Inside a tool

A delivered carrier is moved from the load port into its reserved rack slot by the tool's **carrier
exchange** (1 s per move). The tool then performs the setup if needed, loads the wafers, processes
them and unloads them back into the carrier, using the source load and unload times once per LOT or
batch. Finished carriers are moved back to a port for pickup. Port moves, load/unload, processing and
vehicle pickup/drop are separate activities with their own events. A tool whose rack is full of
finished work it cannot hand off takes no new work.

### Maintenance and downtime

- **PMs** (calendar or wafer-count) are spread across the tools of a group as in the source. When a
  PM is due, the tool finishes the work it has committed and then starts the PM; the next calendar
  PM is counted from the actual start.
- **Breakdowns** occur per tool and cause, at source time-to-failure intervals in calendar time. A
  breakdown pauses work on the tool, and the work resumes where it stopped once every cause has
  cleared.
- **Injected faults** (`injected_faults`) are scheduled outages of a tool, vehicle, rail or port, used
  to test what-if situations.

The fab starts empty: no WIP, each tool in the first eligible setup of its group, no PM or breakdown
in progress.

## 6. Transport

Every move of a carrier is a **transfer** with its own ID, from an origin (entry, port, buffer) to a
destination. A transfer is assigned only when the destination port and rack place, and a vehicle,
can all be claimed at once; until pickup it can still be cancelled. After pickup the carrier belongs
to the vehicle and the destination space stays reserved until delivery.

- **Destination.** Among eligible tools with free space, the one with the lowest loaded travel time
  (optional policies weigh load and congestion).
- **Vehicle.** The free vehicle with the lowest empty travel time to the pickup.
- **Order.** The oldest waiting transfer first; a blocked transfer does not hold up others.
- **Overflow.** If no eligible tool has space, the carrier may go to one buffer and wait there for a
  tool; it never moves from buffer to buffer. If a tool has space but no vehicle is free, the
  carrier waits at its origin.
- **Handling.** Pickup and drop take 10 s each; the vehicle stays in place and keeps its track space
  meanwhile.
- **Same tool.** If a LOT's next step runs on the tool it is already in, it stays in its rack slot
  without transport.

## 7. Vehicle motion and traffic

**Movement.** Vehicles move at constant speed along directed rails: the vehicle's straight-rail
speed, capped at 1,000 mm/s on curves. Acceleration is not modelled. Distances come from the rail
geometry (straight chords between nodes), times are computed exactly and rounded once per move.

**Space.** Each vehicle claims an interval of rail covering its body plus the minimum gap. A vehicle
moves in **hops**, from one holding point (a safe stopping position every 2 m outside junctions and
ports) to the next, or to its destination port. Before a hop it reserves all the track it will sweep
and every junction or control zone (ZCU) on the way, all at once; if anything is taken it waits where
it is. Vehicles cannot overlap or pass each other on a rail. With `hop_lookahead_mm`, a vehicle on
clear track reserves several hops in one go, with the same arrival times and far fewer events.

**Routing.** Vehicles follow the fastest free-flow route. By default congestion does not change a
route; the optional congestion and divert policies in [settings.md](settings.md) re-route vehicles
around queues.

**Idle vehicles.** An idle vehicle waits at a holding point. If it is in the way of another
vehicle, it moves forward to the nearest free holding point; after a drop it leaves the port.
Vehicles never reverse along a rail or jump.

**Blocked traffic** stays visible in the OHT events (`oht_blocked`, with the blocking owner). The
simulator does not force movement or detect deadlocks by proof; a stuck fab keeps waiting until it
moves again or the horizon is reached.

## 8. Time, ordering and randomness

**Time** is integer microseconds from the start. Source values are parsed exactly and rounded once,
to the nearest microsecond. Positions are integer micrometres.

**Order.** Events at the same time are settled in a fixed order: completions first, then the ends of
downtime, then the starts of downtime, then releases. After that, admission, manufacturing, transfer
assignment and traffic run in turn until nothing changes at that time. Activities occupy
[start, end), so a completion at t happens before a breakdown at t.

**Randomness.** Every random decision draws from its own stream: releases per stream, processing,
sampling and rework per LOT, maintenance per tool and cause, injected faults per rule. Each stream's
seed is a SHA-256 of the master seed, the channel and the scope ID, so adding a LOT or a fault does
not shift the draws of others. Candidate ranking never draws; only actual decisions do.
`replication_salt` gives independent replications from the same checkpoint.

**Reproducibility.** The same inputs, settings, seed and CPython minor version reproduce the same
run exactly. A resumed checkpoint continues exactly as the uninterrupted run would.

## 9. Output

Each committed state change is written immediately as records in `events.jsonl` (factory) and
`oht.jsonl` (vehicles), with one shared event sequence; see [outputs.md](outputs.md). Records carry
the LOT, visit, execution, carrier, transfer and equipment IDs they concern. Times that are not known
yet are `null`; nothing is invented.

`replay.rate` paces the output in wall-clock time (simulated seconds per wall second) without changing
the simulation; by default the simulator runs as fast as it can.

## 10. Outcomes and stopping

| Outcome | Meaning |
| --- | --- |
| Completed | A cohort run in which every LOT was admitted, finished its route and left the fab, with all output written |
| Incomplete | The run stopped at the horizon, a settlement limit, its exit targets or an operator stop. Ongoing release always ends this way |
| Failed | An error or broken invariant prevents correct continuation; the cause is written to `diagnostics.jsonl` |
| Rejected | Invalid or unsupported input, found before the simulation starts |

The horizon is inclusive: events at the horizon are settled, nothing after it runs. An operator stop
takes effect between events. Breakdowns, PMs and congestion are normal fab behaviour, not failures.
CQT compliance (`constraint_status`) is reported separately from the outcome. The simulator does not
restart itself; continuing from a checkpoint is an explicit action (or `ops/fabctl` after a crash,
see [ops/README.md](../ops/README.md)).

## 11. Simplifications

- No acceleration or braking; constant speed with a curve cap.
- No robot or wafer-level mechanics inside tools; racks and internal processing are aggregate.
- No wafer loss, scrap or LOT splitting.
- No look-ahead dispatch that idles a tool for a LOT still in transit (unless super-hot reservation is
  on).
- Curved rails are measured as straight chords.
- Pickup, drop and carrier-exchange times are project choices (10 s, 10 s, 1 s), not measurements.
