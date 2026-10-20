# 실행 설정

[English](settings.md) | **한국어**

실행은 하나의 JSON 파일로 설정합니다(`examples/`와 `baseline/config.json` 참고). 알 수 없는 키는 거부되므로
철자가 틀린 설정은 무시되지 않고 오류가 납니다. 상대 경로는 설정 파일이 있는 디렉터리를 기준으로
해석됩니다.

시뮬레이션되는 팹에 관한 모든 것은 이 파일에 있습니다. 몇몇 실행 옵션은 명령행에서도 줄 수 있으며, 그
경우 파일보다 우선합니다:

| 위치 | 옵션 |
| --- | --- |
| `fab_sim.cli run`, `tools/measure_run.py` | `--resume-from MANIFEST`(= `checkpoint.resume_from`), `--checkpoint-interval-wall-seconds S`(그 간격으로 체크포인트 활성화), `--progress`, `--progress-interval S`; `measure_run`은 추가로 `--report`, `--cpu-affinity` |
| `ops/fabctl launch` / `start` | 시도마다 설정 사본(`attempts/<attempt>/config.json`)을 쓰며, 여기에 출력 경로, 체크포인트 옵션(`--checkpoint-interval`, `--checkpoint-background`, `--checkpoint-retain-daily`, `--checkpoint-retain-all`), `--set KEY=JSON`의 정책 변경을 반영합니다. `launch`는 `execution.max_settlements`를 제거합니다. 옵션은 이후 시도에 이어집니다 |

모든 실행의 최종 설정(기본값이 모두 채워진 상태)은 그 실행의 `manifest.json`(`resolved_config`)에
기록됩니다.

| 그룹 | 내용 |
| --- | --- |
| `inputs` | `manufacturing_path`, `physical_reference_path`(준비된 JSON, [layout.ko.md](layout.ko.md) 참고); 생성 대신 주어진 토폴로지를 쓰려면 선택적으로 `topology_path` |
| `generation` | 토폴로지 생성 방식: 장비 `counts`, `rack_capacities`, `fleet_count`, 레이아웃 정책과 그 파라미터([layout.ko.md](layout.ko.md) 참고) |
| `workload` | `count`(고정 코호트; 계속 투입하려면 생략), `selected_streams`, `product_weights`, `release_time_scale` |
| `execution` | `master_seed`, `horizon_us`, 종료 한도와 아래 정책 설정 |
| `replay` | `rate`: `null`이면 최대한 빠르게 실행; 숫자면 실제 1초당 시뮬레이션 초 단위로 출력 속도를 맞춤 |
| `output` | `directory`, `topology_directory`(토폴로지 캐시; 기본은 저장소 루트의 `topologies/`), `run_id`, 레코드/청크 한도. `live`와 `publication`은 퍼블리셔 어댑터를 통한 실시간 발행을 설정하며, Python에서 시뮬레이터를 호출할 때(`fab_sim.runner.run(..., publication_adapter=...)`)만 쓸 수 있고 명령행에서는 쓸 수 없습니다 |
| `probes` | 타이밍 프로브: `enabled`, `sample_every`(N번째 핸들러마다 측정), `recent_limit`(실패 보고용으로 보관하는 최근 트레이스 수) |
| `checkpoint` | 주기적 체크포인트와 재개(아래) |
| `injected_faults` | 장비, 차량, 레일, 포트에 대한 선택적 예정 비가용 구간 |

## 워크로드

| 키 | 의미 |
| --- | --- |
| `count` | 고정 코호트: 이만큼의 LOT을 `product_weights`(기본은 원본 비율)에 따라 제품별로 나누어 투입한 뒤 투입을 멈춤. 생략하면 계속 투입: 투입 스트림이 종료 시점까지 계속됨(풀스케일 팹) |
| `product_weights` | 투입 LOT의 제품 비율, 예: `{"Product_3": "0.5", "Product_4": "0.5"}`(기본: 원본 비율) |
| `selected_streams` | SMT2020 투입 스트림: `Regular_Lot_3`, `Regular_Lot_4`, `HotLot_3`, `HotLot_4`, `SuperHotLot` |
| `release_time_scale` | 투입 간격에 곱하는 값(십진 문자열). `"1"`은 원본 투입률, `"1.075"`는 원본의 93%로 투입 |
| `dual_route_validation` | `true`면 두 제품의 LOT을 모두 투입하지 않는 코호트를 거부(짧은 테스트 실행용 안전장치) |

## 실행: 종료와 재현성

| 키 | 기본값 | 의미 |
| --- | --- | --- |
| `master_seed` | 7 | 모든 난수 스트림의 시드(스트림은 범위별로 독립) |
| `horizon_us` | 필수 | 마이크로초 단위의 시뮬레이션 시간 한도(180일 = `15552000000000`) |
| `max_settlements` | 없음 | 이 횟수만큼 스케줄러 settlement 후 정지(한정된 테스트용) |
| `exit_targets` | 없음 | 제품별로 이만큼의 LOT이 출고되면 정지, 예: `{"Product_3": 3}` |
| `replication_salt` | 없음 | 시작 또는 재개 이후 생성되는 난수 스트림의 시드에 섞는 양의 정수; 하나의 체크포인트에서 독립적인 반복 실행을 만듦 |
| `transition_budget`, `derived_cache_budget_mib` | 100000, 512 | 내부 한도; 그대로 두세요 |

## 실행: 정책 설정

원본 데이터가 정하지 않은 부분에 대한 이 프로젝트의 선택입니다. 설정하지 않으면 모두 꺼져 있으며, 모두
체크포인트의 물리 식별 정보에 기록됩니다. 다른 값으로 체크포인트를 재개하려면
`checkpoint.allow_policy_change: true`(`fabctl start --set KEY=JSON`)가 필요하고, 이는 실행 manifest에
기록됩니다. 프로덕션 열은 `baseline/config.json`의 값입니다.

| 키 | 프로덕션 | 하는 일 |
| --- | --- | --- |
| `setup_policy` | `group_least_setup` | 최소 연속 처리(minimum run) 셋업이 있는 장비(임플란트 가스)가 레시피를 바꿀 수 있는 시점. `staged_campaign`(기본): 최소 연속 처리 분량의 LOT이 장비에 모두 대기한 뒤에만. `switch_on_empty`: 설치된 레시피의 작업이 장비에 묶여 있지 않으면 즉시. `group_least_setup`: 요청한 LOT 이상의 우선순위를 가진 LOT이 그룹 어디에서도 설치된 레시피를 기다리지 않을 때만, 그리고 목적지를 셋업이 가장 적은 순으로 정렬(SMT2020 AutoSched 순위: 우선순위, 최소 셋업, FIFO; 최소 연속 처리 7) |
| `setup_minimum_runs` | — | 셋업 이름별 최소 연속 처리 횟수 재정의 |
| `hop_lookahead_mm` | 30000 | 차량이 2 m 홀딩 간격마다 하나씩이 아니라, 비어 있는 선로를 따라 이 거리까지(추가 분기 없이) 여러 홀딩 지점을 한 번에 예약할 수 있음. 비어 있는 선로에서 도착 시각은 같고 이벤트는 훨씬 적음 |
| `bay_entry_divert` | true | 막힌 차량들 뒤에서 베이 입구에 갇힌 차량을 인터베이로 돌려 다시 진입시킴(이동당 최대 4회) |
| `queue_divert` | true | 막힌 대기열 뒤에서 60초 이상 기다린 차량이 비어 있는 다른 분기로 우회(우회 900초 이하, 이동당 최대 8회 수정) |
| `congestion_routing_s` | 20 | 반송 구간 경로를 자유 주행 시간 + 진입하는 레일마다 대기 차량 1대당 이 초만큼 더해 계획 |
| `congestion_destination_s` | 60 | 동등한 목적지(부하가 같은 장비, 오버플로 버퍼) 중에서 목적지 구역의 대기 차량 1대당 이 초만큼 더함 |
| `dispatch_metering` | 6 | 이미 이만큼의 차량이 대기 중인 구역으로는 슈퍼핫이 아닌 반송을 배차하지 않음; LOT은 공용 인터베이 대신 출발지에서 대기 |
| `busy_vehicle_cap` | 120 | 반송 중인 차량이 이만큼이면 슈퍼핫이 아닌 추가 배차를 대기시킴(혼잡 악순환 방지 장치) |
| `buffer_selection` | `next_group_bays` | 오버플로 보관 위치 선택: `nearest_origin`(기본) 또는 다음 단계 장비 그룹의 베이에 있는 버퍼를 먼저, 그다음 인접 베이 |
| `down_tool_output` | true | 다운된 장비의 포트에 있는 완료 LOT을 픽업할 수 있음(장비가 다운되어도 로드 포트는 동작) |
| `down_tool_avoid_s` | 7200 | 이 초 이상의 다운타임 중인 장비로는 배송하지 않음; LOT은 같은 그룹의 다른 장비로 가거나 버퍼에서 대기 |
| `super_hot_reservation` | true | 슈퍼핫 LOT이 이동해 오는 동안 장비가 다음 시작을 보류(SMT2020 `SuperHotLotFIRST_and_Reservation`) |
| `super_hot_expedite` | true | 슈퍼핫 LOT은 최소 배치 크기보다 작은 배치를 시작할 수 있고, 차량은 LOT 우선순위 순으로 배정 |
| `extra_processing` | 꺼짐 | 모의 비효율 훅: 선택한 단계에 확률적으로 추가 공정 시간을 더함(실제 값은 `diagnostics.jsonl`에만 기록). 규칙: 경로(선택적으로 영역, 장비 그룹, 원본 단계), 확률, 상대 또는 절대 추가 시간 |

## 체크포인트

| 키 | 기본값 | 의미 |
| --- | --- | --- |
| `enabled` | false | 체크포인트 쓰기 |
| `interval_wall_seconds` | 300 | 체크포인트 사이의 실제 시간(초) |
| `directory` | `<output>/checkpoints` | 체크포인트 위치 |
| `retain_completed` | 3 | 보관할 최신 체크포인트 수 |
| `retain_daily` | false | 추가로 최근 14 시뮬레이션일은 하루마다, 그 이전은 주마다 첫 체크포인트를 보관 |
| `resume_from` | 없음 | 이어 갈 체크포인트의 `manifest.json` |
| `allow_compatible_build` | false | 상태 계약(state contract)과 Python/RNG 런타임이 같은 다른 빌드가 쓴 체크포인트를 허용(`cross_build`로 기록) |
| `allow_policy_change` | false | 식별 정보가 위 정책 설정에서만 다른 체크포인트를 허용(`policy_changes`로 기록) |
| `background` | false(베이스라인: true) | fork된 프로세스에서 체크포인트 저장: 실행은 약 40초 대신 4–9초만 멈추지만, writer는 저장하는 동안 메모리가 필요(베이스라인에서 약 0.8 GB). 저장 전마다 여유 메모리(컨테이너와 systemd 제한 포함)를 이전 writer의 사용량과 비교해, 부족하면 대신 포그라운드로 저장; 그런 저장은 체크포인트 지표에 `background_fallback`으로 표시([running.ko.md](running.ko.md#체크포인트-메모리) 참고) |
| `verify_history` | false | 재개 시 은퇴한 이력 아카이브 체인도 검증(체크포인트가 나온 실행의 `history/` 디렉터리가 필요) |

체크포인트는 입력, generation, 워크로드, 시드, 정책 설정이 같고(위에서 허용한 경우 제외), 상태 계약과
CPython 마이너 버전이 같을 때만 재개됩니다. 원본 워크북이나 이력 아카이브는 필요하지 않습니다.
