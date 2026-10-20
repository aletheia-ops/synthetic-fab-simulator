# ops: fabctl로 장기 실행

[English](README.md) | **한국어**

풀스케일 실행은 실제 시간으로 며칠에서 몇 주 동안 공장 데이터를 만들어 냅니다. `fabctl`은 이를 systemd
사용자 서비스로 실행합니다. 그래서 터미널을 닫아도 계속 실행되고, 체크포인트를 저장하며, 크래시 후에는
스스로 재개하고, 상태를 잃지 않고 정지·재개·되감기하거나 새 코드로 옮길 수 있습니다.

짧은 실행이나 설정을 시험해 볼 때는 시뮬레이터를 직접 실행하세요([docs/running.ko.md](../docs/running.ko.md)).

## 요구 사항

- systemd 사용자 서비스가 있는 Linux 또는 WSL2. WSL2에서는 `/etc/wsl.conf`에 `[boot] systemd=true`를 설정합니다.
- 로그아웃 후에도 서비스가 계속 실행되도록 `loginctl enable-linger $USER`.
- [README](../README.ko.md)대로 설치된 저장소(체크아웃 안의 `.venv/`; 다른 인터프리터는 `FAB_PYTHON`으로 지정).
- `main` 브랜치가 있는 git 체크아웃, 그리고 `PATH`에 있는 `git`과 시스템 `python3`(fabctl이 내부 기록에
  사용): 각 실행은 한 커밋에 고정된 worktree에서 실행됩니다(아래 참고).

## 빠른 시작

```bash
ops/fabctl install                                    # 이 체크아웃용 사용자 유닛 작성(최초 1회)
ops/fabctl launch baseline/config.json --name my-run  # 실행 시작(여기서는 베이스라인 팹을 이어서 실행)
ops/fabctl status                                     # 서비스, 커밋, 시도, 최신 체크포인트
ops/fabctl stop                                       # 다음 체크포인트에서 정지
ops/fabctl start                                      # 최신 체크포인트에서 재개
```

진행 상황은 `tail -f outputs/runs/my-run/attempts/*/progress.jsonl`로 따라가고, 공장 상태는
`tools/run_report.py outputs/runs/my-run` 또는 `tools/traffic_watch.py`로 확인합니다
([docs/running.ko.md](../docs/running.ko.md#6-실행-관찰과-분석)).

## 실행(run)과 시도(attempt)

```
outputs/runs/my-run/
├── config.json            실행 시작 시의 설정
├── run.json               이름, 원본 설정, 시도 목록(순서대로)
└── attempts/
    ├── 20261020T091500Z-592a521/      시작 또는 재개마다 하나의 폴더: <UTC 시각>-<커밋>
    │   ├── config.json                이 시도의 실제 적용 설정
    │   ├── process.json               커밋, worktree, 재개한 체크포인트, 정책 변경
    │   ├── progress.jsonl             1분마다 하나의 진행 기록
    │   └── simulation/                events.jsonl, oht.jsonl, manifest.json, checkpoints/, history/
    └── 20261021T120000Z-592a521/      다음 시도는 이전 시도의 체크포인트에서 이어 감
```

**실행(run)**은 하나의 시뮬레이션된 팹 이력입니다. `launch`, `start`, `swap` 또는 자동 복구가 일어날
때마다 체크포인트에서 이어 가는 새 **시도(attempt)**가 열리므로, 한 실행의 이벤트 스트림은 그 시도들의
스트림을 순서대로 이은 것입니다(이벤트 순번은 시도를 넘어 이어집니다). 실행 이름은 모든 기록의
`run_id`로 쓰입니다.

코드는 한 커밋에 고정된 git worktree인 `.worktrees/live-<commit>`에서 실행되므로, 체크아웃에서 파일을
수정하거나 브랜치를 바꿔도 실행 중인 시뮬레이션은 바뀌지 않습니다.

## 명령

| 명령 | 하는 일 |
| --- | --- |
| `install` | 이 체크아웃용 systemd 사용자 유닛을 `~/.config/systemd/user`에 작성 |
| `launch CONFIG [--name NAME] [COMMIT]` | `COMMIT`(기본 `main`)에서 설정으로 새 실행을 시작. `checkpoint.resume_from`이 있는 설정(예: `baseline/config.json`)은 그 체크포인트를 이어서 실행 |
| `status` | 서비스 상태, 실행, 커밋, 시도, 최신 체크포인트, 스트림 크기 |
| `stop [--now]` | 다음 체크포인트 후 정지(`--now`면 즉시 정지, 마지막 체크포인트 이후 작업은 손실) |
| `start [OPTIONS] [COMMIT]` | 현재 실행을 최신 체크포인트에서 재개, 커밋을 주지 않으면 같은 커밋 |
| `swap COMMIT [OPTIONS]` | `stop` 후 다른 커밋에서 `start`(다른 빌드로 이어 가는 것으로, manifest에 기록됨) |
| `verify` | 실행 중인 시도 점검: import 경로, 스트림 증가, 선언되지 않은 정책 변경 없음 |
| `export MANIFEST DEST [--commit SHA] [--link]` | 다른 곳에서 이어 갈 수 있도록 체크포인트의 독립 번들(상태, 입력, 토폴로지, 코드, 설정)을 작성 |

`launch`와 `start`의 옵션:

| 옵션 | 의미 |
| --- | --- |
| `--checkpoint-interval S` | 체크포인트 사이의 실제 시간(초, 기본 1800) |
| `--checkpoint-retain-daily on\|off` | 최신 3개 외에, 2주 동안은 시뮬레이션일마다 하나, 그 이전은 주마다 하나의 체크포인트를 보관 |
| `--checkpoint-retain-all on\|off` | 모든 체크포인트 보관(`start` 전용) |
| `--checkpoint-background on\|off` | fork된 프로세스에서 저장: 실행은 약 40초 대신 4–9초만 멈추지만, writer는 저장하는 동안 메모리가 필요합니다(베이스라인에서 약 0.8 GB). 그 메모리가 없으면 저장은 포그라운드로 전환됩니다([메모리](../docs/running.ko.md#체크포인트-메모리)) |
| `--from MANIFEST` | 최신 대신 이 이전 체크포인트에서 재개(되감기; `start` 전용) |
| `--set KEY=JSON` | 이번 재개부터 실행 정책 설정을 변경, 예: `--set down_tool_output=true`(선언된 정책 변경으로 기록; `start` 전용) |
| `--drop-exit-targets` | `execution.exit_targets`를 제거해 목표에 도달한 실행이 계속 진행되게 함(`start` 전용) |

한 번 지정한 설정은 이후 시도에도 이어집니다.

## 크래시, 정체, 교착

- **크래시.** `fab-live.service`가 `fab-live-recover.service`를 시작하고, 이것이 60초(`FABCTL_RECOVER_DELAY`)
  기다린 뒤 최신 체크포인트에서 재개합니다. 6시간에 최대 3회까지 복구하며, 그 이후에는 직접 확인할 수
  있도록 정지 상태로 둡니다(`outputs/live-ops/recoveries-detail.log`).
- **정체.** `fab-live-watchdog.timer`가 5분마다 점검합니다. 진행이 20분 동안(체크포인트 복원 중에는 45분)
  멈춘 실행은 종료되어 복구가 이어받습니다.
- **교착(gridlock).** 반송이 대기 중인데 3 시뮬레이션 시간 동안 배송이 한 건도 없으면, 재개해도 같은
  정체로 돌아가므로 watchdog은 실행을 재시작하지 않고 정지합니다. 시도를 확인한 뒤 설정을 바꿔 이전
  체크포인트에서 `start --from` 하거나 새로 `launch` 하세요.

## 파일

| 경로 | 내용 |
| --- | --- |
| `ops/fabctl` | 명령 본체(bash; `fabctl help`가 사용법 출력) |
| `ops/live-exec.sh` | 서비스가 실행하는 것: 현재 시도에 대한 `tools/measure_run.py` |
| `ops/export_bundle.py` | `fabctl export` |
| `ops/systemd/*.in` | 유닛 템플릿; `install`이 체크아웃 경로와 인터프리터를 채움 |
| `outputs/live-ops/current_live.json` | 현재 시도(모든 명령이 읽음) |
| `outputs/live-ops/launch.env` | 서비스가 읽는 현재 시도의 환경 |
| `outputs/live-ops/recoveries*.log` | 복구 이력 |

`fabctl`은 체크아웃당 한 번에 하나의 실행만 다룹니다. 유닛을 제거하려면 실행을 정지하고,
`~/.config/systemd/user`에서 `fab-live*` 파일 네 개를 지운 뒤 `systemctl --user daemon-reload`를 실행하세요.
