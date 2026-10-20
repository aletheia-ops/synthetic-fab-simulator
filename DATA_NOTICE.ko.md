# 데이터 고지

[English](DATA_NOTICE.md) | **한국어**

> 이 문서는 [영문 원문](DATA_NOTICE.md)의 번역입니다. 내용이 다를 경우 영문 원문이 우선합니다.

이 저장소의 시뮬레이터 코드는 MIT 라이선스입니다(`LICENSE` 참고). 아래 파일들은 그 라이선스의 대상이
**아닙니다**. 이 파일들은 두 개의 공개 연구용 테스트베드에서 파생되었으며, 원본 워크북을 먼저 준비하지
않고도 시뮬레이터를 실행하고 결과를 재현할 수 있도록 하기 위해서만 재배포됩니다. 원 데이터에 대한 모든
권리는 원저작자에게 있습니다. 이 프로젝트는 그에 대한 어떠한 라이선스도 부여하지 않습니다.

## 파생 파일

| 파일 | 원본 | 이 프로젝트에서 바꾼 것 |
| --- | --- | --- |
| `layout/manufacturing.json` | SMT2020 Dataset 1(HVLM): 제품, 공정 경로, 장비 그룹, 공정·셋업 시간, 다운타임과 PM 데이터 | 워크북을 검증된 JSON 모델로 변환; 내용은 변경 없음 |
| `layout/reference.json` | SMAT2022 LVHM_E: 레일 네트워크, 베이, 스토커, 포트, 차량 | 검증된 JSON 물리 참조 데이터로 변환; 서비스 여유 공간 적용 |
| `layout/topologies/*.json` | SMAT2022 기하 정보 + SMT2020 장비 그룹 | Dataset 1의 장비 배치 생성, 장비 수와 랙 용량 조정, 5 m/s 차량 200대 |
| `layout/topology.svg` | 위 토폴로지 | 해당 토폴로지의 평면도 |
| `baseline/checkpoint/**` | 위 모든 파일 | 공정 경로, 장비 그룹, 토폴로지를 포함하는 시뮬레이션된 공장 상태(한 실행의 60.4일 시점) |

## 출처와 인용

이 파일이나 이 파일로 만든 결과를 사용한다면 원 저작물을 인용해 주세요:

- **SMT2020:** D. Kopp, M. Hassoun, A. Kalir and L. Mönch, "SMT2020—A Semiconductor
  Manufacturing Testbed," *IEEE Transactions on Semiconductor Manufacturing*, 33(4), 2020.
  DOI: [10.1109/TSM.2020.3001933](https://doi.org/10.1109/TSM.2020.3001933).
  데이터: <https://p2schedgen.fernuni-hagen.de/index.php/downloads/simulation>
- **SMAT2022:** K. Lee, S. Song, D. Chang and S. Park, "A New AMHS Testbed for
  Semiconductor Manufacturing," *Proceedings of the 2022 Winter Simulation Conference*.
  논문: <https://informs-sim.org/wsc22papers/339.pdf>.
  데이터: <https://github.com/kwoo-lee/SMAT2022>

BibTeX 항목은 README에 있습니다. 원본 워크북은 재배포하지 않으며, 입력 준비 과정(`fab_sim.cli prepare-*`)은
준비된 파일의 출처 정보(provenance)에 워크북의 해시를 기록합니다.

## 삭제 요청

권리자로서 이 파일 중 어떤 것이든 삭제를 원하시면 이 저장소에 이슈를 열어 주세요. 해당 파일을 내리겠습니다.
