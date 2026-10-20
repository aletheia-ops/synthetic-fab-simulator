# ops: long runs with fabctl

**English** | [한국어](README.ko.md)

A full-scale run produces factory data for days or weeks of wall time. `fabctl` runs it as a
systemd user service, so it survives closing the terminal, saves checkpoints, resumes by itself
after a crash and can be stopped, resumed, rewound or moved to new code without losing state.

For a short run, or to try a configuration, run the simulator directly instead
([docs/running.md](../docs/running.md)).

## Requirements

- Linux or WSL2 with systemd user services. On WSL2 set `[boot] systemd=true` in `/etc/wsl.conf`.
- `loginctl enable-linger $USER`, so the service keeps running after you log out.
- The repository installed as in the [README](../README.md) (`.venv/` in the checkout; `FAB_PYTHON`
  selects another interpreter).
- A git checkout with a `main` branch, `git` and a system `python3` on `PATH` (fabctl uses it for
  its bookkeeping): each run executes from a pinned worktree of one commit (below).

## Quick start

```bash
ops/fabctl install                                    # write the user units for this checkout (once)
ops/fabctl launch baseline/config.json --name my-run  # start a run (here: continue the baseline fab)
ops/fabctl status                                     # service, commit, attempt, newest checkpoint
ops/fabctl stop                                       # stop at the next checkpoint
ops/fabctl start                                      # resume from the newest checkpoint
```

Follow progress with `tail -f outputs/runs/my-run/attempts/*/progress.jsonl` and look at the
factory with `tools/run_report.py outputs/runs/my-run` or `tools/traffic_watch.py`
([docs/running.md](../docs/running.md#6-watching-and-analysing-a-run)).

## Runs and attempts

```
outputs/runs/my-run/
├── config.json            the configuration as launched
├── run.json               name, source configuration, attempts in order
└── attempts/
    ├── 20261020T091500Z-592a521/      one folder per start or resume: <UTC time>-<commit>
    │   ├── config.json                the attempt's effective configuration
    │   ├── process.json               commit, worktree, checkpoint it resumed from, policy changes
    │   ├── progress.jsonl             one progress record per minute
    │   └── simulation/                events.jsonl, oht.jsonl, manifest.json, checkpoints/, history/
    └── 20261021T120000Z-592a521/      the next attempt continues from the previous one's checkpoint
```

A **run** is one simulated fab history. Every `launch`, `start`, `swap` or automatic recovery opens a
new **attempt** that continues from a checkpoint, so the event streams of a run are its attempts'
streams in order (event sequence numbers continue across attempts). The run name is used as the
`run_id` in every record.

The code runs from `.worktrees/live-<commit>`, a git worktree pinned to one commit, so editing or
switching branches in your checkout never changes a running simulation.

## Commands

| Command | What it does |
| --- | --- |
| `install` | Write the systemd user units for this checkout to `~/.config/systemd/user` |
| `launch CONFIG [--name NAME] [COMMIT]` | Start a new run from a configuration on `COMMIT` (default `main`). A configuration with `checkpoint.resume_from` (such as `baseline/config.json`) continues that checkpoint |
| `status` | Service state, run, commit, attempt, newest checkpoint and stream sizes |
| `stop [--now]` | Stop after the next checkpoint (or at once with `--now`, losing the work since the last one) |
| `start [OPTIONS] [COMMIT]` | Resume the current run from its newest checkpoint, on the same commit unless one is given |
| `swap COMMIT [OPTIONS]` | `stop`, then `start` on another commit (a cross-build continuation, recorded in the manifest) |
| `verify` | Check the running attempt: import path, growing streams, no undeclared policy change |
| `export MANIFEST DEST [--commit SHA] [--link]` | Write a self-contained bundle of a checkpoint (state, inputs, topology, code, config) to continue elsewhere |

Options of `launch` and `start`:

| Option | Meaning |
| --- | --- |
| `--checkpoint-interval S` | Wall-clock seconds between checkpoints (default 1800) |
| `--checkpoint-retain-daily on\|off` | Keep one checkpoint per simulated day for two weeks, then one per week, besides the newest three |
| `--checkpoint-retain-all on\|off` | Keep every checkpoint (`start` only) |
| `--checkpoint-background on\|off` | Save from a forked process: the run pauses 4–9 s instead of about 40 s, but the writer needs memory while it writes (about 0.8 GB on the baseline); a save falls back to the foreground when that memory is not free ([memory](../docs/running.md#checkpoint-memory)) |
| `--from MANIFEST` | Resume from this older checkpoint instead of the newest (rewind; `start` only) |
| `--set KEY=JSON` | Change an execution policy setting from this resume on, e.g. `--set down_tool_output=true` (recorded as a declared policy change; `start` only) |
| `--drop-exit-targets` | Remove `execution.exit_targets`, so a run that reached them keeps going (`start` only) |

Settings given once carry over to later attempts.

## Crashes, stalls and gridlock

- **Crash.** `fab-live.service` starts `fab-live-recover.service`, which waits 60 s
  (`FABCTL_RECOVER_DELAY`) and resumes from the newest checkpoint. At most 3 recoveries in 6 hours;
  after that the run stays stopped for you to look at (`outputs/live-ops/recoveries-detail.log`).
- **Stall.** `fab-live-watchdog.timer` checks every 5 minutes. A run whose progress stops for 20
  minutes (45 while restoring a checkpoint) is killed so recovery takes over.
- **Gridlock.** If no carrier has been delivered for 3 simulated hours while transfers wait, the
  watchdog stops the run without restarting it, since a resume would return to the same jam. Inspect
  the attempt, then `start --from` an earlier checkpoint with changed settings, or `launch` anew.

## Files

| Path | Contents |
| --- | --- |
| `ops/fabctl` | The command (bash; `fabctl help` prints its usage) |
| `ops/live-exec.sh` | What the service runs: `tools/measure_run.py` on the current attempt |
| `ops/export_bundle.py` | `fabctl export` |
| `ops/systemd/*.in` | Unit templates; `install` fills in the checkout path and interpreter |
| `outputs/live-ops/current_live.json` | The current attempt (read by every command) |
| `outputs/live-ops/launch.env` | Environment of the current attempt, read by the service |
| `outputs/live-ops/recoveries*.log` | Recovery history |

`fabctl` operates one run at a time per checkout. To remove the units, stop the run, delete the
four `fab-live*` files from `~/.config/systemd/user` and run `systemctl --user daemon-reload`.
