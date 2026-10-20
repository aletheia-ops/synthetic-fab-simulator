# 출력

[English](outputs.md) | **한국어**

시뮬레이터는 상위(upstream) 이벤트 생성기입니다. 실제 팹의 센서와 제어 시스템을 대신합니다. 각 시뮬레이션
출력 디렉터리(`output.directory`; `fabctl launch`로 시작한 실행은
`outputs/runs/NAME/attempts/<attempt>/simulation/`)에는 다음이 들어 있습니다:

| 파일 | 내용 |
| --- | --- |
| `events.jsonl` | 공장 이벤트: 투입, 입고, 방문(visit), 공정, 캐스케이드 단위, 배치, 유지보수, 장비 캐리어 교환, 캐리어 보관 이전(`transfer_*`) |
| `oht.jsonl` | 차량별 OHT 제어 이벤트(아래) |
| `diagnostics.jsonl` | 관측용 스트림에서 제외되는 기록: 런타임 실패, `processing_perturbed`(`extra_processing`의 실제 값), `transport_abstraction_replaced`(데이터셋의 반송 대체 단계가 실제 AMHS 이동으로 바뀜), `dispatch_cap_changed`(`busy_vehicle_cap` 장치의 작동 또는 해제) |
| `manifest.json` | 실행 식별 정보, 설정, 레코드 수, 최종 요약(`event_streams: fab-events-v3`) |
| `checkpoints/` | 체크포인트. 저장마다 시뮬레이션일과 짧은 ID로 이름 붙은 폴더 하나(`day-061.25-1a2b3c4d/`: `manifest.json` + `state.json`)와 최신 체크포인트를 가리키는 `latest.json`. `day-`로 시작하는 폴더는 모두 읽으므로 복사한 체크포인트는 ID를 생략할 수 있음(베이스라인은 `day-060.40/`) |
| `history/` | 완료된 LOT과 종료된 기록의 은퇴 이력: 샤드 폴더 안의 압축 세그먼트(`<id[:2]>/<id>.jsonz`), 해시로 연결됨; 감사(`checkpoint.verify_history`)에만 필요 |

`--progress`를 주면 `fab_sim.cli run`과 `tools/measure_run.py`는 간격마다 하나의 진행 기록(시뮬레이션일,
settlement 수, 제품별 입고/출고, 대기 중인 반송)을 stderr에 씁니다. 이를 `progress.jsonl` 같은 파일로
리다이렉트하세요(`fabctl`은 자동으로 그렇게 합니다). `measure_run`은 최종 요약도 `--report` 파일에
씁니다(`fabctl`에서는 `measurement.json`).

풀 프로덕션 부하(공정 중 LOT 약 1,400개)에서의 양: 시뮬레이션 하루마다 `events.jsonl` 약 0.8 GB,
`oht.jsonl` 약 1.4 GB, `history/` 약 0.3 GB, 그리고 보관 체크포인트 하나당 약 0.65 GB(`retain_daily`면
2주 동안은 하루에 하나, 그 이후는 주에 하나).

`events.jsonl`과 `oht.jsonl`은 하나의 레코드 봉투(envelope)를 공유합니다(`schema_version`
`fab-events-v3`, `run_id`, `event_sequence`, `event_id`, `simulation_time_us`, `simulation_time_s`,
`event_type`, `entity_refs`, `payload`). 또한 **하나의 이벤트 순번**을 공유합니다. 두 파일을
`event_sequence`로 병합하면 정확한 발생 순서가 되며, 실행의 시작 순번부터 빈틈없이 이어집니다. 레코드는
각 전이가 확정되는 즉시 동기적으로 기록됩니다.

## 공장 이벤트 유형(`events.jsonl`)

| 그룹 | 이벤트 유형 |
| --- | --- |
| 투입 | `release_intent_due`, `lot_admitted`, `lot_exited`, `route_completed` |
| 방문 | `visit_entered`, `visit_rack_ready`, `visit_skipped`, `cqt_violation` |
| 공정 | `execution_committed`, `processing_started`, `processing_completed`, `unload_completed` |
| 캐스케이드 | `cascade_unit_started`, `cascade_slot2_started`, `cascade_unit_completed` |
| 배치 | `batch_member_arrived`, `batch_committed`, `batch_processing_started`, `batch_processing_completed`, `batch_unload_completed`, `batch_cleared` |
| 캐리어 교환 | `carrier_exchange_started`, `carrier_exchange_completed`, `carrier_exchange_cancelled` |
| 클리어런스 | `clearance_lease_committed`, `clearance_lease_cancelled`, `clearance_member_buffered`, `clearance_member_departed`, `clearance_unused_members_released` |
| 반송 | `transfer_assigned`, `transfer_picked_up`, `transfer_delivered`, `transfer_rack_delivered`, `transfer_rack_reused`, `transfer_cancelled` |
| 핸드오프 | `handoff_pickup_ready`, `handoff_committed`, `handoff_completed`, `handoff_cancelled`, `handoff_cancel_deferred` |
| 셋업 캠페인 | `setup_campaign_committed`, `setup_campaign_serving`, `setup_campaign_ready`, `setup_campaign_completed`(`setup_policy: staged_campaign`에서 최소 연속 처리 셋업을 위한 대기 구성) |
| 서비스 | `service_started`, `service_completed`(`kind`: `setup`, 로드/언로드 및 기타 장비 서비스) |
| 유지보수 | `maintenance_pending`, `maintenance_started`, `maintenance_recovered`(`kind`: `pm`, `breakdown`, `injected`) |
| 교통 | `traffic_route_amended`(queue divert 같은 경로 재설정 결정) |

분석에 유용한 타임스탬프: `visit_entered.entered_at_us`(LOT이 다음 단계로 넘어가 반송을 포함한 대기를
시작), `processing_started.processing_started_at_us`, `unload_completed.unloaded_at_us`,
`lot_admitted.admitted_at_us`와 `lot_exited.exited_at_us`(사이클 타임). `tools/run_report.py`가 이
스트림을 읽는 실제 예시입니다.

## OHT 이벤트

자세(pose)에는 `rail_id`, `offset_um`(방향이 있는 레일을 따른 차량 중심 위치), 레일 노드 사이의 직선
현(chord)을 따라 보간한 `x_um`/`y_um`이 들어 있습니다. `task`는 `pickup`, `drop`, `reposition`(유휴
차량 재배치) 또는 `clear`(자리를 찾지 못한 유휴 차량)이며, `transfer_id`는 반송 작업을 `transfer_*`
이벤트와 연결합니다.

| 이벤트 | 시점 | 페이로드 |
| --- | --- | --- |
| `oht_dispatched` | 반송 작업이 차량에 배정됨 | `vehicle_id`, `task`, `transfer_id`, `request_id`, `port_id`, `queued_at_us` |
| `oht_segment` | 차량이 다음 정지 지점으로 출발 | `vehicle_id`, `task`, `transfer_id`, `request_id`, `from`, `to`, `started_at_us`, `expected_arrival_us`, `distance_um`, `carrier_id` |
| `oht_arrived` | 이동이 목적지에 도달 | `vehicle_id`, `task`, `transfer_id`, `request_id`, `pose`, `carrier_id` |
| `oht_movement_cancelled` | 취소된 이동이 안전한 끝점에서 멈춤 | `oht_arrived`와 같음 |
| `oht_blocked` | 차량이 진행할 수 없음(또는 막는 대상이 바뀜) | `vehicle_id`, `task`, `transfer_id`, `request_id`, `pose`, `blocked_by`(`kind` rail/passage/fault, `id`, `owner`) |
| `oht_load_started` / `oht_load_completed` | 포트에서 호이스트 픽업 | `vehicle_id`, `transfer_id`, `carrier_id`, `lot_id`, `port_id` |
| `oht_unload_started` / `oht_unload_completed` | 포트에서 호이스트 내려놓기 | 같음 |
| `amhs_fault_started` / `amhs_fault_recovered` | 차량 또는 레일 고장 | `target_type`, `target_id`, `cause_id`, `segments`(영향받는 이동 중 차량과 현재 `expected_arrival_us`, 멈춰 있는 동안은 `null`) |

차량의 세그먼트는 사슬처럼 이어집니다. 각 `oht_segment.from`은 이전 세그먼트의 `to`와 같고, 고장으로
멈추지 않는 한 세그먼트는 `expected_arrival_us`에 끝납니다(멈춘 경우 복구 기록이 새 예상 도착 시각을
알려 줍니다). 정지 지점마다의 별도 도착 기록은 없습니다. 멈춘 차량은 다음 `oht_segment`, `oht_arrived`
또는 `oht_blocked`가 나올 때까지 마지막 세그먼트의 `to`에 있습니다. 막힘 상태에서 벗어난 차량은 다음
`oht_segment`에 나타납니다.

## 반송 이벤트

`events.jsonl`의 `transfer_*` 페이로드는 캐리어를 설명합니다: `id`, `carrier_id`, `lot_id`, `visit_id`,
`vehicle_id`, `origin_id`, `destination_id`, `rack_slot_id`, `status`, 시각, 교환 연결 정보. 계획된 경로,
후보 목적지 목록, 내부 점유 기록은 내보내지 않습니다.

## 엔티티 참조(v3)

`entity_refs`는 레코드와 관련된 모든 엔티티를 정렬된 ID 목록으로 나열합니다: `lot_ids`, `carrier_ids`,
`visit_ids`, `step_ids`(공정 단계, `source_step_id`), `execution_ids`, `batch_ids`, `equipment_ids`,
`transfer_ids`, `exchange_ids`, `oht_ids`, `movement_ids`. 페이로드 자체의 ID 필드 외의 규칙:

- 캐리어는 평생 하나의 LOT에 속하므로(`{lot_id}:carrier`), 모든 캐리어 참조는 그 LOT도 참조합니다;
- 랙, 포트, 서비스 소유자 ID는 장비 이름을 포함하며(`TOOL:tool:NNN:…`), 그 장비는 `equipment_ids`에서
  참조됩니다;
- `handoff_*` 페이로드에는 반송의 `carrier_id`와 `lot_id`가 들어 있습니다.

LOT ID(`{stream_id}:{occurrence_index}`)는 한 시뮬레이션 실행 안에서 고유하며 체크포인트를 넘어 이어집니다.
웨이퍼는 고유 ID가 없습니다.

페이로드는 레코드의 스냅숏입니다. 아직 알 수 없는 필드는 `null`입니다(예: `processing_started`의
`processing_completed_at_us`).
