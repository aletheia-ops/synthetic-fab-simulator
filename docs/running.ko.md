# 시뮬레이터 실행

[English](running.md) | **한국어**

명령은 저장소 루트에서 실행합니다. 요구 사항: Linux, WSL2 또는 macOS(네이티브 Windows는 지원하지 않음),
CPython 3.12. 풀스케일 실행은 동작 중 약 3.5 GB의 메모리를 쓰고(복원 시 최대 4.5 GB), 그 위에
체크포인트 저장을 위한 여유가 필요합니다([체크포인트 메모리](#체크포인트-메모리) 참고). 백그라운드 저장에는
RAM 8 GB면 충분하며, 여유 메모리가 부족하면 저장이 자동으로 포그라운드로 전환됩니다. WSL2에서는 기본적으로
Linux VM이 Windows 메모리의 절반만 받습니다. RAM 8 GB PC에서는 `%UserProfile%\.wslconfig`의 `[wsl2]` 아래에
`memory=6GB`를 설정하고 `wsl --shutdown`을 실행하세요. 그렇지 않으면 풀스케일 베이스라인 복원이 들어가지 않습니다. 시뮬레이션
하루마다 수 GB의 디스크를 씁니다([outputs.ko.md](outputs.ko.md) 참고).

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

## 1. 베이스라인 재개(워크북 불필요)

`baseline/`에는 시뮬레이션 60.4일 시점의 풀스케일 팹이, `layout/`에는 그 팹을 만든 입력과 토폴로지가
있습니다. 실행은 180일 종료 시점(horizon)까지 이어지며 30분마다 체크포인트를 저장합니다.

장기 실행에는 `fabctl`(5절)을 사용하세요: `ops/fabctl launch baseline/config.json --name baseline`.
체크포인트에서의 정지, 재개, 크래시 복구를 대신 처리해 줍니다.

대신 터미널에서 직접 실행하려면:

```bash
.venv/bin/python tools/unpack_baseline.py
mkdir -p outputs/baseline
.venv/bin/python -m fab_sim.cli run --config baseline/config.json --progress 2> outputs/baseline/progress.jsonl
```

결과는 `outputs/baseline/simulation/`에 기록되며, `tail -f outputs/baseline/progress.jsonl`로 따라갈 수
있습니다. Ctrl-C로 멈춥니다(마지막 체크포인트 이후의 작업은 손실). 나중에 최신 체크포인트에서
`--resume-from outputs/baseline/simulation/checkpoints/<checkpoint>/manifest.json`과 새
`output.directory`로 이어 갑니다(출력 디렉터리는 다시 쓰지 않습니다). 상태에 무엇이 들어 있는지는
[baseline/README.ko.md](../baseline/README.ko.md)를 참고하세요.

## 2. 짧은 스모크 실행

`examples/smoke-run.json`은 SMAT 레이아웃이 아닌 축소된 가상 팹입니다(`single_spine_slots_v1`: 단일 레일
스파인에 그룹당 장비 1대, 차량 7대). 120 LOT 코호트를 투입하고 5,000 settlement(약 0.5 시뮬레이션일,
실제 시간 몇 초)에서 `Incomplete` 결과로 멈춥니다.

```bash
.venv/bin/python -m fab_sim.cli preflight --config examples/smoke-run.json
.venv/bin/python -m tools.measure_run --config examples/smoke-run.json \
  --report outputs/smoke-measurement.json --progress
```

출력 디렉터리와 리포트 파일은 절대 덮어쓰지 않습니다. 다시 실행하려면 먼저 `outputs/smoke-run/`과
`outputs/smoke-measurement.json`을 지우세요(또는 `output.directory`와 `--report`를 바꾸세요).

Preflight는 읽기 전용입니다. `ready` 또는 `warnings`는 종료 코드 0, `rejected`는 사유(도킹 위치 누락,
배치 할당 부족, 도달 불가능한 배송 등)와 함께 종료 코드 2를 냅니다. preflight가 ready라고 해서 실행이
원활히 흐른다는 보장은 없습니다.

`measure_run`은 결과 JSON을 stdout에, 진행 기록을 stderr에, 측정 리포트를 `--report`에 씁니다.
`result.outcome`은 `Completed`(코호트가 모두 출고), `Incomplete`(종료 시점, settlement 한도 또는 운영자
정지; 계속 투입 모드에서는 정상), `Failed` 또는 `Rejected`입니다.

`baseline/config.json`은 풀스케일 프로덕션 설정입니다([settings.ko.md](settings.ko.md) 참고). 변형을
시험하려면 설정을 복사해 값과 `output.directory`를 바꾼 뒤 실행하세요.

## 3. 원본 워크북에서 입력 준비

`layout/manufacturing.json`과 `layout/reference.json`은 이미 준비되어 있습니다. 직접 준비하려면(예:
다른 SMT2020 데이터셋에서) 워크북을 내려받아([DATA_NOTICE.ko.md](../DATA_NOTICE.ko.md) 참고) `data/`에
두고 다음을 실행합니다:

```bash
mkdir -p prepared
.venv/bin/python -m fab_sim.cli prepare-manufacturing --source data/SMT_2020_Model_Data_-_HVLM.xlsx --output prepared/manufacturing.json
.venv/bin/python -m fab_sim.cli prepare-topology --source data/SMAT_2022_Model_Data_-_LVHM_E.xlsx \
  --manufacturing prepared/manufacturing.json --profile layout/physical-profile.json --output prepared/reference.json
```

준비 과정은 기존 파일을 절대 덮어쓰지 않습니다. 설정의 `inputs`가 새 파일을 가리키게 하세요. 토폴로지는
첫 실행 때 설정의 `generation` 값으로 생성되어 `output.topology_directory`(기본: 저장소 루트의
`topologies/`; 입력, 설정, 생성기 코드로 키가 정해짐)에 캐시되므로, 이후 실행과 재개는 이를 재사용합니다.
워크북이 필요한 테스트는 `FAB_HVLM_WORKBOOK`과 `FAB_SMAT_WORKBOOK`을 읽고 없으면 건너뜁니다.
`FAB_PREPARED_DIR`(준비된 `manufacturing.json`, `reference.json`, `topology.json`이 있는 폴더)을
지정하면 전체 경로 테스트가 준비 단계를 건너뜁니다.

## 4. 체크포인트

`checkpoint.enabled`이면 실행은 `interval_wall_seconds`마다 체크포인트를 씁니다(풀스케일에서 포그라운드
저장은 약 40초, 백그라운드 저장은 4–9초 멈춤). `--resume-from MANIFEST` 또는 `checkpoint.resume_from`으로
재개합니다. 체크포인트는 입력, generation, 워크로드, 시드, 정책 설정이 같고 CPython 마이너 버전이 같을
때만 재개됩니다. 빌드를 넘나들거나 정책 설정을 바꾸는 옵션은 [settings.ko.md](settings.ko.md#체크포인트)에
있습니다. 재개된 실행은 중단 없이 실행했을 때와 정확히 같게 이어집니다(같은 빌드, 같은 설정).

### 체크포인트 메모리

베이스라인에서 측정한 값입니다(공정 중 LOT 약 1,400개; 실행 자체는 60일에 3.2 GB, 105일에 3.6 GB를
쓰며 WIP에 따라 증가):

| `checkpoint.background` | 저장당 실행 정지 | 저장 중 추가 메모리 |
| --- | --- | --- |
| `true`(베이스라인 기본) | 4–9초 | fork된 writer가 약 45초 동안 약 0.8 GB의 전용(private) 메모리 사용, CPU 코어 1개 추가 사용 |
| `false` | 약 40초 | 0.1 GB 미만 |

fork된 writer는 처음에는 실행의 메모리를 모두 공유합니다. Python의 참조 카운팅이 writer가 읽는 모든 객체에
값을 쓰기 때문에 저장되는 상태를 담은 메모리 페이지는 복사됩니다. 상태는 노드 단위로 기록되므로 writer가
따로 큰 구조를 만들지는 않습니다. 베이스라인에서 시스템 사용 메모리는 실행 자체보다 최대 약 0.8 GB 높아집니다.

`background: true`이면 실행은 저장할 때마다 writer가 들어갈 여유가 있는지 확인합니다. 사용 가능한
메모리(`MemAvailable`, systemd `MemoryMax`나 컨테이너 같은 cgroup 제한이 있으면 그 범위로 제한)가 이전
writer가 마지막에 쓴 전용 메모리의 1.15배 + 0.25 GB 이상이어야 합니다(첫 저장은 실행 상주 메모리의 절반 + 0.5 GB).
그렇지 않으면 그 저장은 포그라운드에서 실행되고, `checkpoint_metrics`의 해당 항목에 수치와 함께
`background_fallback`이 기록됩니다. 모든 백그라운드 저장은 `writer_private_bytes`를 기록합니다. 메모리를
읽을 수 없는 환경(`/proc`이 없는 macOS 등)에서는 writer를 fork합니다. 항상 포그라운드로 저장하려면
`"background": false`(또는 `fabctl ... --checkpoint-background off`)로 설정하세요.

## 5. fabctl로 장기 실행(systemd)

`ops/fabctl`은 하나의 장기 시뮬레이션을 체크포인트, 크래시 복구, watchdog이 있는 systemd 사용자
서비스로 실행하고, 정지·재개·되감기·새 코드로 이동을 할 수 있게 합니다:

```bash
ops/fabctl install
ops/fabctl launch baseline/config.json --name my-run
ops/fabctl status
```

실행과 시도, 모든 명령과 옵션, 크래시 후 동작은 [ops/README.ko.md](../ops/README.ko.md)를 참고하세요.

## 6. 실행 관찰과 분석

| 도구 | 용도 |
| --- | --- |
| `tools/static_capacity.py WORKBOOK.xlsx` | 실행 전: SMT2020 워크북으로 장비 그룹별 정적 부하 계산(워크북을 먼저 내려받으세요, [DATA_NOTICE.ko.md](../DATA_NOTICE.ko.md) 참고) |
| `tools/traffic_watch.py SOURCE TARGET_DAY [LIMIT_S] [--follow]` | 실행 중: 30 시뮬레이션 분마다 배송당 OHT 막힘 시간(초)과 `BREAKDOWN`/`RECOVERED` 줄. SOURCE는 실행 폴더, 시도 또는 `outputs/live-ops/current_live.json` |
| `tools/run_report.py RUN_DIR` | 언제든지: 처리량, 사이클 타임, 추세, 모든 장비 그룹의 부하, WIP 위치, 긴 다운타임; `--steady-state`는 정상 상태 평가 추가, `--csv DIR`는 표를 내보냄 |
| `tools/render_topology.py TOPOLOGY.json -o plan.svg` | 토폴로지 평면도 |

`run_report`는 작은 상태 파일(`RUN_DIR/report/state.json`)을 유지하므로 반복 호출 시 새 이벤트만 읽습니다.
실행 중인 run에서도 호출당 1초가 훨씬 안 걸리고 메모리는 약 30 MB입니다. 첫 호출은 이벤트 로그 전체를
읽습니다(70일 실행이면 몇 분). `traffic_watch`는 코어 하나의 1%도 쓰지 않습니다. 두 도구 모두 읽은 파일
범위를 페이지 캐시에서 내립니다. 실행 중인 시뮬레이션 옆에서는 `nice`와 함께 실행하세요.

## CPU 배치

`tools/measure_run.py --cpu-affinity`로 실행을 지정한 CPU에 고정할 수 있습니다(`performance` 또는 CPU ID
지정; `performance`는 WSL에서 사용할 수 없음). 기본값 `none`은 배치를 OS에 맡깁니다.
