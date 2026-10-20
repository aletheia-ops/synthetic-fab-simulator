# 레이아웃 입력

[English](README.md) | **한국어**

SMAT2022 LVHM_E 레이아웃 위의 SMT2020 Dataset 1을 위해 준비된 입력입니다. 원본 워크북에서
파생되었으며, 출처·인용·이용 조건은 [DATA_NOTICE.ko.md](../DATA_NOTICE.ko.md)를 참고하세요.

| 파일 | 내용 |
| --- | --- |
| `manufacturing.json` | SMT2020 Dataset 1(HVLM): 제품, 공정 경로, 장비 그룹, 공정, 셋업, 배치, 유지보수, 투입. `fab_sim.cli prepare-manufacturing`으로 준비 |
| `reference.json` | SMAT2022 LVHM_E 물리 참조: 레일, 베이, 스토커, 포트, 차량 유형. `fab_sim.cli prepare-topology`와 `layout/physical-profile.json`으로 준비 |
| `topologies/<sha256>.json` | 프로덕션 토폴로지: `smat_area_flow_v1`로 배치한 장비 1,050대, 차량 200대. 위 두 파일과 `baseline/config.json`의 `generation` 설정으로 생성 |
| `topologies/requests/<key>.json` | 같은 설정의 실행이 토폴로지를 새로 생성하지 않고 찾을 수 있게 하는 등록 정보 |
| `topology.svg` | 프로덕션 토폴로지의 평면도(`tools/render_topology.py`) |

이 파일들을 수정하지 마세요. 파일의 해시는 베이스라인 체크포인트 식별 정보의 일부이며, 수정된 파일은
조용히 사용되지 않고 거부됩니다. 다른 입력을 쓰려면 새 파일을 준비하고([docs/running.ko.md](../docs/running.ko.md)
참고) 설정이 그 파일을 가리키게 하세요.
