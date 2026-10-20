# Synthetic Fab Simulator

[English](README.md) | **한국어**

실제와 같은 공장 데이터를 생성하는 가상(synthetic) 반도체 팹 시뮬레이터입니다. SMT2020 Dataset 1
제조 모델 전체(583단계와 343단계 공정 경로를 가진 두 제품, 105개 장비 그룹, 셋업, 배치, 캐스케이딩,
리워크, 샘플링, 고장과 PM)를 SMAT2022 팹 레이아웃 위에서 실행합니다. 모든 캐리어는 실제 레일을 따라
OHT(overhead hoist transport) 차량으로 포트, 버퍼, 스토커를 거쳐 이동하고, 분기·합류 지점에서는 교통
제어를 받습니다. 출력은 실제 팹의 센서와 제어 시스템이 보고하는 내용과 같습니다. 공장 이벤트(LOT,
장비, 유지보수, 캐리어 기록)와 차량별 OHT 이벤트가 시뮬레이션 진행과 동시에 JSON lines로 기록됩니다.

![팹 평면도: SMAT2022 레일 네트워크 위에 공정 영역별로 색칠된 장비 1,050대](layout/topology.svg)

저장소에는 튜닝된 풀스케일 설정과 시뮬레이션 60.4일 시점의 팹 상태가 포함되어 있어, 입력을 따로
준비하지 않고도 바로 가동 중인 팹에서 데이터를 생성할 수 있습니다.

## 빠른 시작

요구 사항: Linux, WSL2 또는 macOS; CPython 3.12(그 이후 버전은 검증되지 않았으며, 베이스라인을 포함한
체크포인트는 그것을 저장한 Python 마이너 버전에서만 재개됩니다); 풀스케일 실행에는 RAM 8 GB(체크포인트는
백그라운드 writer가 저장하며, 여유 메모리가 부족하면 자동으로 포그라운드에서 저장됩니다; RAM 8 GB의 WSL2에서는
VM에 6 GB를 할당하세요, [docs/running.ko.md](docs/running.ko.md) 참고).

```bash
git clone https://github.com/aletheia-ops/synthetic-fab-simulator.git
cd synthetic-fab-simulator
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

### fabctl로 베이스라인 팹 실행(권장)

`ops/fabctl`은 systemd가 있는 Linux 또는 WSL2에서 시뮬레이션을 백그라운드 서비스로 실행합니다. 터미널을
닫아도 계속 실행되고, 30분마다 체크포인트를 저장하며, 크래시 후에는 스스로 재개하고, 체크포인트에서
깔끔하게 멈춥니다. 60.4일 시점부터 베이스라인 팹을 이어서 실행합니다:

```bash
ops/fabctl install                                   # 최초 1회: systemd 사용자 유닛 작성
ops/fabctl launch baseline/config.json --name baseline
ops/fabctl status
tail -f outputs/runs/baseline/attempts/*/progress.jsonl
ops/fabctl stop                                      # 다음 체크포인트에서 정지
ops/fabctl start                                     # 가장 최근 체크포인트에서 재개
```

이벤트는 `outputs/runs/baseline/attempts/<attempt>/simulation/events.jsonl`과 `oht.jsonl`에
기록됩니다. 요구 사항(systemd 사용자 서비스, `loginctl enable-linger`, `git`)과 모든 명령은
[ops/README.ko.md](ops/README.ko.md)에 있습니다.

### 직접 실행(모든 플랫폼, 짧은 실행)

systemd가 없거나(예: macOS) 잠깐 시험해 볼 때는 터미널에서 시뮬레이터를 직접 실행합니다. 이 경우에도
체크포인트는 저장되지만, 터미널을 닫으면 멈추고 크래시에서 자동 복구되지 않습니다:

```bash
.venv/bin/python tools/unpack_baseline.py
mkdir -p outputs/baseline
.venv/bin/python -m fab_sim.cli run --config baseline/config.json --progress 2> outputs/baseline/progress.jsonl
```

결과는 `outputs/baseline/simulation/`에 기록됩니다. 짧은 스모크 실행(축소된 가상 팹: 그룹당 장비 1대,
단일 레일 스파인, 차량 7대, 120 LOT 코호트; 5,000 settlement, 약 0.5 시뮬레이션일에서 몇 초 만에
멈춥니다):

```bash
.venv/bin/python -m tools.measure_run --config examples/smoke-run.json \
  --report outputs/smoke-measurement.json --progress
```

(출력은 절대 덮어쓰지 않습니다. 다시 실행하려면 `outputs/smoke-run/`과 리포트 파일을 먼저 지우세요.)

### 설정

실행은 설정 파일(`baseline/config.json`, `examples/smoke-run.json`)로 정해집니다: 워크로드, 장비 수,
반송 정책, 종료 시점(horizon), 시드, 체크포인트. 하나를 복사해 수정한 뒤 그 사본을 실행하세요. 모든
설정은 [docs/settings.ko.md](docs/settings.ko.md)에 있습니다. 몇몇 실행 옵션은 명령행 플래그입니다
(`--resume-from`, `--checkpoint-interval-wall-seconds`, `--progress`; fabctl에서는 `--checkpoint-*`,
`--from`, `--set`).

설치된 `fab-sim` 명령은 `python -m fab_sim.cli`와 같습니다(`fab-sim run --config ...`,
`fab-sim preflight`, `fab-sim prepare-manufacturing`, ...).

실행 중 언제든지 워크로드를 확인할 수 있습니다:

```bash
.venv/bin/python tools/run_report.py outputs/runs/baseline     # fabctl 실행, 시도(attempt) 또는 출력 디렉터리
```

## 문서

| 문서 | 내용 |
| --- | --- |
| [docs/running.ko.md](docs/running.ko.md) | 설치, 실행, 재개, 원본 워크북에서 입력 준비, `ops/fabctl`로 장기 실행, 분석 도구 |
| [docs/settings.ko.md](docs/settings.ko.md) | 반송·디스패치 정책을 포함한 모든 실행 설정 |
| [docs/outputs.ko.md](docs/outputs.ko.md) | 출력 파일과 이벤트 스키마 |
| [docs/model.ko.md](docs/model.ko.md) | 시뮬레이션 모델: 원본 데이터, 적용 방식, 스케줄링, 반송, 종료 |
| [docs/layout.ko.md](docs/layout.ko.md) | 물리 레이아웃, 장비 배치, 토폴로지 캐시 |
| [docs/capacity.ko.md](docs/capacity.ko.md) | Dataset 1의 용량과 프로덕션 설정이 바꾸는 것 |
| [baseline/README.ko.md](baseline/README.ko.md) | 60.4일 시점의 베이스라인 팹 상태 |
| [ops/README.ko.md](ops/README.ko.md) | `fabctl`로 장기 실행 |

## 저장소 구성

| 경로 | 내용 |
| --- | --- |
| `src/fab_sim/` | 시뮬레이터 |
| `layout/` | 준비된 SMT2020/SMAT2022 입력, 프로덕션 토폴로지와 평면도, 입력 준비에 쓰는 물리 프로파일 |
| `baseline/` | 프로덕션 설정과 60.4일 체크포인트 |
| `examples/` | 빠른 시작용 스모크 설정 `smoke-run.json` |
| `tools/` | 실행 진입점 `measure_run`(`cpu_affinity` 포함), `verify_checkpoint`, 분석 도구 `run_report`, `traffic_watch`, `static_capacity`, `render_topology`, `unpack_baseline` |
| `ops/` | 장기 실행용 `fabctl`과 systemd 유닛 템플릿([ops/README.ko.md](ops/README.ko.md)) |
| `tests/` | 테스트 모음 |

## 감사의 말과 인용

제조 모델은 SMT2020, 물리 레이아웃은 SMAT2022입니다. 이로부터 파생된 파일(`layout/`,
`baseline/checkpoint/`)은 이 저장소의 라이선스 대상이 아닙니다. [DATA_NOTICE.ko.md](DATA_NOTICE.ko.md)를
참고하세요. 이 시뮬레이터나 데이터를 사용한다면 다음을 인용해 주세요:

```bibtex
@article{kopp2020smt2020,
  author  = {Kopp, Denny and Hassoun, Michael and Kalir, Adar and M{\"o}nch, Lars},
  title   = {{SMT2020}---A Semiconductor Manufacturing Testbed},
  journal = {IEEE Transactions on Semiconductor Manufacturing},
  volume  = {33},
  number  = {4},
  year    = {2020},
  doi     = {10.1109/TSM.2020.3001933}
}
@inproceedings{lee2022smat2022,
  author    = {Lee, Kwanwoo and Song, Siyong and Chang, Daesoon and Park, Sangchul},
  title     = {A New {AMHS} Testbed for Semiconductor Manufacturing},
  booktitle = {Proceedings of the 2022 Winter Simulation Conference},
  year      = {2022},
  url       = {https://informs-sim.org/wsc22papers/339.pdf}
}
```

## 라이선스

시뮬레이터 코드와 문서는 [MIT License](LICENSE)로 배포됩니다. 데이터셋에서 파생된 파일은 제외되며,
[DATA_NOTICE.ko.md](DATA_NOTICE.ko.md)를 참고하세요.
