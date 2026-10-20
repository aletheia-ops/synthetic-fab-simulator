# Outputs

The simulator is an upstream event generator: it stands in for the sensors and
controllers of a physical fab. Each simulation output directory (`output.directory`;
for runs started with `fabctl launch`, `outputs/runs/NAME/attempts/<attempt>/simulation/`) contains:

| File | Contents |
| --- | --- |
| `events.jsonl` | Factory events: releases, admissions, visits, processing, cascade units, batches, maintenance, equipment exchanges and carrier custody (`transfer_*`) |
| `oht.jsonl` | Per-vehicle OHT controller events (below) |
| `diagnostics.jsonl` | Records kept out of the observational streams: runtime failures, `processing_perturbed` (ground truth of `extra_processing`), `transport_abstraction_replaced` (a dataset transport placeholder step replaced by real AMHS moves) and `dispatch_cap_changed` (the `busy_vehicle_cap` guard engaging or releasing) |
| `manifest.json` | Run identity, configuration, record counts and final summary (`event_streams: fab-events-v3`) |
| `checkpoints/` | Checkpoints, one folder per save named by simulated day and a short id (`day-061.25-1a2b3c4d/`: `manifest.json` + `state.json`), and `latest.json` pointing at the newest. Any folder starting with `day-` is read, so a copied checkpoint may drop the id (the baseline's is `day-060.40/`) |
| `history/` | Retired history of finished LOTs and closed records: compressed segments in shard folders (`<id[:2]>/<id>.jsonz`), chained by hash; only needed for audits (`checkpoint.verify_history`) |

With `--progress`, `fab_sim.cli run` and `tools/measure_run.py` write one progress record per
interval to stderr (simulated days, settlements, admitted/exited per product, pending transfers);
redirect it to a file such as `progress.jsonl` (`fabctl` does). `measure_run` also writes the final
summary to its `--report` file (`measurement.json` under `fabctl`).

Volume at full production load (about 1,400 LOTs in process): roughly 0.8 GB of `events.jsonl`,
1.4 GB of `oht.jsonl` and 0.3 GB of `history/` per simulated day, plus about 0.65 GB per retained
checkpoint (with `retain_daily`, one per day for two weeks, then one per week).

`events.jsonl` and `oht.jsonl` share one record envelope (`schema_version`
`fab-events-v3`,
`run_id`, `event_sequence`, `event_id`, `simulation_time_us`,
`simulation_time_s`, `event_type`, `entity_refs`, `payload`) and **one event
sequence**: merging both files by `event_sequence` gives the exact emission
order, contiguous from the run's base sequence. Records are written
synchronously as each transition commits.

## Factory event types (`events.jsonl`)

| Group | Event types |
| --- | --- |
| Release | `release_intent_due`, `lot_admitted`, `lot_exited`, `route_completed` |
| Visit | `visit_entered`, `visit_rack_ready`, `visit_skipped`, `cqt_violation` |
| Processing | `execution_committed`, `processing_started`, `processing_completed`, `unload_completed` |
| Cascade | `cascade_unit_started`, `cascade_slot2_started`, `cascade_unit_completed` |
| Batch | `batch_member_arrived`, `batch_committed`, `batch_processing_started`, `batch_processing_completed`, `batch_unload_completed`, `batch_cleared` |
| Exchange | `carrier_exchange_started`, `carrier_exchange_completed`, `carrier_exchange_cancelled` |
| Clearance | `clearance_lease_committed`, `clearance_lease_cancelled`, `clearance_member_buffered`, `clearance_member_departed`, `clearance_unused_members_released` |
| Transfer | `transfer_assigned`, `transfer_picked_up`, `transfer_delivered`, `transfer_rack_delivered`, `transfer_rack_reused`, `transfer_cancelled` |
| Handoff | `handoff_pickup_ready`, `handoff_committed`, `handoff_completed`, `handoff_cancelled`, `handoff_cancel_deferred` |
| Setup campaign | `setup_campaign_committed`, `setup_campaign_serving`, `setup_campaign_ready`, `setup_campaign_completed` (minimum-run setup staging under `setup_policy: staged_campaign`) |
| Service | `service_started`, `service_completed` (`kind`: `setup`, load/unload and other tool services) |
| Maintenance | `maintenance_pending`, `maintenance_started`, `maintenance_recovered` (`kind`: `pm`, `breakdown`, `injected`) |
| Traffic | `traffic_route_amended` (re-routing decisions such as queue divert) |

Useful timestamps for analysis: `visit_entered.entered_at_us` (a LOT is released to its next step
and starts waiting, including transport), `processing_started.processing_started_at_us`,
`unload_completed.unloaded_at_us`, `lot_admitted.admitted_at_us` and `lot_exited.exited_at_us`
(cycle time). `tools/run_report.py` is a worked example of reading these streams.

## OHT events

Poses carry `rail_id`, `offset_um` (vehicle centre along the directed rail) and
`x_um`/`y_um`, interpolated along the straight chord between the rail's nodes.
`task` is `pickup`, `drop`, `reposition` (idle repositioning) or `clear`
(an idle vehicle that cannot find space); `transfer_id` links transport tasks
to `transfer_*` events.

| Event | When | Payload |
| --- | --- | --- |
| `oht_dispatched` | A transport task is given to a vehicle | `vehicle_id`, `task`, `transfer_id`, `request_id`, `port_id`, `queued_at_us` |
| `oht_segment` | The vehicle starts moving to its next stop | `vehicle_id`, `task`, `transfer_id`, `request_id`, `from`, `to`, `started_at_us`, `expected_arrival_us`, `distance_um`, `carrier_id` |
| `oht_arrived` | A movement reaches its destination | `vehicle_id`, `task`, `transfer_id`, `request_id`, `pose`, `carrier_id` |
| `oht_movement_cancelled` | A cancelled movement stops at its safe endpoint | same as `oht_arrived` |
| `oht_blocked` | The vehicle cannot proceed (or the blockers change) | `vehicle_id`, `task`, `transfer_id`, `request_id`, `pose`, `blocked_by` (`kind` rail/passage/fault, `id`, `owner`) |
| `oht_load_started` / `oht_load_completed` | Hoist pickup at a port | `vehicle_id`, `transfer_id`, `carrier_id`, `lot_id`, `port_id` |
| `oht_unload_started` / `oht_unload_completed` | Hoist drop at a port | same |
| `amhs_fault_started` / `amhs_fault_recovered` | A vehicle or rail fault | `target_type`, `target_id`, `cause_id`, `segments` (affected moving vehicles and their current `expected_arrival_us`, `null` while paused) |

A vehicle's segments chain: each `oht_segment.from` equals the previous
segment's `to`, and a segment ends at `expected_arrival_us` unless a fault
pauses it (then the recovery record gives the new expected arrival). There is no
separate per-stop arrival record; a stopped vehicle is at its last segment's
`to` until its next `oht_segment`, `oht_arrived` or `oht_blocked`. A vehicle
that leaves the blocked state appears in its next `oht_segment`.

## Transfer events

`transfer_*` payloads in `events.jsonl` describe the carrier: `id`,
`carrier_id`, `lot_id`, `visit_id`, `vehicle_id`, `origin_id`,
`destination_id`, `rack_slot_id`, `status`, times and exchange links. Planned
routes, candidate destination lists and internal claim bookkeeping are not
exported.

## Entity references (v3)

`entity_refs` lists every entity a record concerns, as sorted id lists:
`lot_ids`, `carrier_ids`, `visit_ids`, `step_ids` (route step, `source_step_id`),
`execution_ids`, `batch_ids`, `equipment_ids`, `transfer_ids`, `exchange_ids`,
`oht_ids`, `movement_ids`. Rules beyond the payload's own id fields:

- a carrier belongs to one LOT for its whole life (`{lot_id}:carrier`), so every
  carrier reference also references its LOT;
- rack, port and service-owner ids name their tool (`TOOL:tool:NNN:…`), which is
  referenced in `equipment_ids`;
- `handoff_*` payloads carry the transfer's `carrier_id` and `lot_id`.

LOT ids (`{stream_id}:{occurrence_index}`) are unique within a simulation run and
continue across checkpoints. Wafers have no identity of their own.

Payloads are record snapshots: fields that are not yet known are `null`
(for example `processing_completed_at_us` on `processing_started`).
