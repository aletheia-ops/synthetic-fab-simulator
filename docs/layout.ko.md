# 레이아웃과 토폴로지

[English](layout.md) | **한국어**

물리 팹은 SMAT2022 LVHM_E 레이아웃입니다. 300 × 153 m 레일 네트워크로, 단방향 인트라베이 루프 40개,
인터베이 영역 3개, 레일 노드 2,858개, 캐리어 1개용 트랙 버퍼 17,760개, 칸이 8개씩인 스토커 40개로
이루어집니다. 레일 좌표, 방향, 길이, 곡선, 속도 제한, 구역 제어 장치(ZCU)는 원본 그대로 복사합니다. 이
프로젝트가 만들어 내는 것은 SMT2020 Dataset 1 장비의 위치, 그 포트, 그리고 차량 편대입니다.
`layout/topology.svg`는 프로덕션 토폴로지를 보여 주며, `tools/render_topology.py`로 어떤 토폴로지든 그릴
수 있습니다.

## 레이아웃 정책(`generation.layout_policy`)

| 정책 | 장비 배치 |
| --- | --- |
| `single_spine_slots_v1`(기본) | SMAT 레이아웃이 아닌 가상 테스트 팹: 레일 스파인 하나를 따라 장비 슬롯 배치, `counts`의 기본은 그룹당 장비 1대. `examples/smoke-run.json`과 테스트 픽스처가 사용 |
| `smat_reference_slots_v1` | 각 Dataset 1 장비가 SMAT 레이아웃(Dataset 4용으로 설계됨)의 원본 장비 자리를 차지하며, 장비 본체를 원본 베이에 채우고 호환되는 자리끼리 반송을 개선하는 교환을 수행 |
| `smat_area_flow_v1` | Dataset 1 자체의 장비 수와 흐름에 맞춰 장비를 베이 열에 배정. 점수는 공정 영역 응집을 최우선으로 하고(SMAT 논문의 레이아웃처럼 대부분의 베이가 한 영역을 담음), 그다음 흐름 가중 적재 주행 거리 + `balance_weight` × 베이 부하 불균형. 병렬 이동/교환 탐색으로 국소 최적까지 실행. `fit_tolerance`(기본 0.02)는 본체 폭에 적용 |

그 밖의 `generation` 설정:

| 키 | 의미 |
| --- | --- |
| `counts` | 장비 그룹별 장비 수(기본: 원본 수, 105개 그룹에 1,043대; `single_spine_slots_v1`에서는 그룹당 1대) |
| `rack_capacities` | 각 장비 그룹 포트의 캐리어 자리 수 |
| `fleet_count` | OHT 차량 수(reference 정책들에서 필수) |
| `vehicle_speed_mm_s` | 직선 레일 속도; 기본 1,500 mm/s(워크북 OHTV0). 베이스라인은 SMAT2022 논문 값인 5,000 mm/s를 사용하며 곡선 제한은 1,000 mm/s 그대로. 가속은 모델링하지 않음 |
| `clearance_mm` | 장비 본체 주변 여유 공간(기본 500, 프로젝트 선택) |
| `section_window_mm`, `section_max_swaps` | 영역 배치 후, 선로 구간(window)마다 예상 처리 부하를 고르게 하도록 베이 전체를 교환(프로덕션: 50 m 구간, 40회 교환) |
| `entry_capacity`, `exit_capacity` | 팹 입구·출구 스토커의 캐리어 용량 |
| `improvement_limit`, `balance_weight`, `fit_tolerance` | 배치 탐색의 한도와 가중치 |

장비 본체는 그룹의 원본 최대 폭과 높이를 사용하며, 맞추기 위해 줄이지 않습니다. 포트는 본체 범위 안의
직선 베이 레일 위, ZCU 통로 밖에 놓입니다. 각 스토커의 8칸은 두 서비스 포트에 번갈아 대응합니다. 픽업과
내려놓기는 각각 10초 걸립니다. Preflight는 방향성 연결과 용량을 검사하지만, 주어진 부하에서 교통이
원활하다는 것을 증명하지는 않습니다.

## 토폴로지 캐시(`output.topology_directory`)

풀스케일 토폴로지 생성에는 몇 분이 걸리므로 캐시합니다. 토폴로지는 `<content_sha256>.json`(전체 모델,
검증 연결 정보, 출처)으로 저장되고 `requests/<key>.json`으로 등록됩니다. 키는 입력 해시, 실제 적용된
generation 설정, 생성기 소스 파일의 지문을 포함합니다. 입력과 설정이 같은 실행은 캐시된 토폴로지를
재사용하고, 설정이나 생성기 코드가 바뀌면 새로 생성합니다. 손상되었거나 맞지 않는 등록은 조용히 다시
생성하지 않고 오류로 처리합니다. 각 실행 manifest는 사용한 토폴로지를 기록합니다(`topology_artifact`:
경로, 내용 해시, 키, 캐시 적중 여부).

`layout/topologies/`는 프로덕션 설정용 캐시이므로 베이스라인은 아무것도 생성하지 않고 재개됩니다. 생성기
코드 변경으로 동봉된 토폴로지에 도달할 수 없게 되면 `tests/test_baseline_identity.py`가 실패합니다.

설정에 `generation` 없이 `inputs.topology_path`(준비된 토폴로지 파일)를 줄 수도 있습니다.
