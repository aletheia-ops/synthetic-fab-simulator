# 베이스라인 팹 상태

[English](README.md) | **한국어**

**시뮬레이션 60.40일** 시점(75,142,454 settlement)의 풀스케일 팹을 바로 이어서 실행할 수 있는 상태와,
그것을 이어 가는 설정입니다. 빈 팹이 채워질 때까지 기다리지 않고 공장 데이터를 얻으려면 여기서
시작하세요.

| | |
| --- | --- |
| 설정 | `config.json`: 원본 투입률의 93%로 운영하는 SMT2020 Dataset 1, 장비 1,050대(임플란트·리소 병목에서 원본보다 7대 많음), 5 m/s OHT 200대, [docs/settings.ko.md](../docs/settings.ko.md)에 나열된 반송·디스패치 정책(이유는 [docs/capacity.ko.md](../docs/capacity.ko.md) 참고) |
| 상태 | 공정 중인 LOT 약 1,400개; Product_3과 Product_4는 대략 투입률만큼 출고 |
| 체크포인트 | `checkpoint/day-060.40/`: `manifest.json`과 `state.json.xz`(33 MB; 압축 해제 시 625 MB) |
| 물리 다이제스트 | `391f860473035d6244f86b4a1ee71cf472d80bd63ebdb3f3f0ac6ae4f4301c22` |
| 필요 조건 | CPython 3.12(체크포인트는 Python 난수 생성기 상태를 저장합니다); `../layout/`의 입력과 토폴로지 |

이 시점의 팹은 안정적으로 가동 중입니다. 교통 마비가 없고(차량은 배송 한 건당 약 20초 막힘) WIP는
약 1,300–1,420 LOT로 유지됩니다. 여기서 이어서 실행하면 WIP는 105일까지 약 1,400–1,500 LOT 사이에
머물며 하루에 약 1 LOT씩 천천히 증가합니다. 목적에 맞는 정상 상태 여부는 이어 간 실행에
`tools/run_report.py --steady-state`를 사용해 판단하세요.

## 이어서 실행하기

감독되는 장기 실행으로(systemd가 있는 Linux 또는 WSL2; [ops/README.ko.md](../ops/README.ko.md) 참고):

```bash
ops/fabctl install
ops/fabctl launch baseline/config.json --name baseline     # 필요하면 상태 파일을 압축 해제
```

또는 터미널에서 직접:

```bash
.venv/bin/python tools/unpack_baseline.py      # state.json.xz -> state.json, 체크섬 검증
mkdir -p outputs/baseline
.venv/bin/python -m fab_sim.cli run --config baseline/config.json --progress 2> outputs/baseline/progress.jsonl
```

상태 복원은 다른 작업이 없는 머신에서 약 2분(CPU가 바쁘면 더 오래) 걸리며 최대 4.5 GB의 메모리를
씁니다. 이후 실행은 약 3.2–3.6 GB를 사용합니다. 체크포인트는 30분마다 fork된 백그라운드 writer가
저장하며(실행은 4–9초 멈춤), writer는 약 45초 동안 약 0.8 GB를 추가로 씁니다. 여유 메모리가
부족하면 해당 저장은 포그라운드에서 실행됩니다(약 40초 멈춤, 추가 메모리 0.1 GB 미만).
[docs/running.ko.md](../docs/running.ko.md#체크포인트-메모리)를 참고하세요. 이어 간 실행의
`manifest.json`은 `continuation` 아래에 출처를 기록하고(빌드가 다르면 `cross_build`에 체크포인트를 쓴
빌드가 기록됩니다), `topology_artifact.cache_hit: true`를 기록합니다.

이 상태는 이미 팹을 떠난 LOT의 이력(`/runs/production-v3/` 아래의 아카이브)을 참조하지만, 그 아카이브는
포함되어 있지 않습니다. 실행은 이를 읽지 않으며, 선택 사항인 `checkpoint.verify_history` 감사만 이를
필요로 합니다.
