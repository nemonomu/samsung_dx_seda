# REST API + SSR Document parsing hybrid

> This document describes retained listing mode 2. The current default is mode 4 (URL-first); see `casas_listing_modes.md` for selection and scope. Mode 2 retains the original behavior described here. Mode 4 is specified separately in `casas_listing_url_first.md`.

적용일: 2026-09-23. Casas Bahia main/BSR listing 전용.

## 실행 계약

1. 페이지마다 기존 Partner 검색 REST GET을 최대 3회 호출한다(최초1회 + 재시도2회). 과거 환경값이5회 이상이어도 코드 상한3회가 우선한다. 재시도 기본 대기는3초/6초다.
2. REST의 상품·가격·상품ID·판매자 검증이 성공하면 기존 파서를 사용하고 Chrome을 열지 않는다. 기존 가격 POST는 검색 GET3회 상한과 별개다.
3. REST3회가 실패하면 전용 Chrome으로 같은 listing URL에 접속한다. 브라우저가 받은 실제 Document의 NEXT_DATA 및 같은 navigation의 완료된 가격 POST 응답을 병합한다.
4. 요청 page와 명시된 정렬, 상품/SKU/판매자 식별자, 파싱 결과를 검사한다. Chrome에서는 캐시·service worker와 이전 navigation 응답을 제외한다. 기존 제품군 관련성 필터는 보존한다.
5. Chrome에서 가격 응답 누락/미완료/HTTP 실패가 발생하면3초 뒤 한 번만 새 navigation으로 재접속한다(최대2회). 서로 다른 navigation의 Document/가격은 섞지 않는다. 페이지/정렬/ID 구조 불일치 등은 재접속으로 숨기지 않는다.
6. 성공한 경우에만 기존 listing 출력 컬럼과 순위 계산 경로에 전달한다. Chrome까지 실패한 페이지는 즉시 실패 로그를 남긴다.
7. 한 페이지라도 미해결이면 `main_occurrences.partial.csv`와 `complete=false` manifest를 남기고 실패 종료하여 downstream을 막는다. 이전 완료 CSV가 있다면 `main_occurrences.previous.<timestamp>.csv`로 보존한다.

이 방식은 성공을 보장하는 무한 재시도가 아니다. REST/Chrome 양쪽이 실패하면 명시적으로 미완료 처리한다.

## Chrome 버전/세션

- 설치된 Chrome 실행 파일의 실제 버전을 감지한다. Windows에서는 파일 버전 정보를 사용하므로 버전 확인을 위해 사용자 Chrome 창을 띄우지 않는다.
- 감지 major를 `version_main`으로 전달하여 UC가 일치하는 드라이버를 준비한다. 고정153/148 설정이나 버전 불일치 강행을 사용하지 않는다.
- 실행 후 브라우저/드라이버 major도 대조한다. 드라이버 다운로드 불가나 버전 확인 실패는 명시적 실패다.
- listing 전용 새 세션을 페이지 사이 재사용하고 단계 종료 시 닫는다. 사용자 프로필/기존 Chrome 프로세스는 종료하지 않는다.
- 기본은 실제 검증된 창 있는 Chrome(headed)이다. GCP 무인/headless 환경 성공은 별도 검증 대상이다.

| 선택 설정 | 의미 |
|---|---|
| `SEDA_CASAS_BAHIA_CHROME_PATH` | 사용할 Chrome 실행 파일 지정. 생략하면 설치 경로 탐색 |
| `SEDA_CASAS_BAHIA_BROWSER_HEADLESS` | 기본0. 1이면 headless 사용(운영 환경별 별도 검증 필요) |
| `SEDA_CASAS_BAHIA_BROWSER_WAIT_SECONDS` | Document/가격 검증 대기 상한, 기본30초. 검증 완료 시 조기 반환 |

## 범위와 파일

- listing 단계가 `casas_listing_hybrid` 모드를 지정한다. 기존 global `SEDA_FETCH_MODE=graphql` 및 상세 실행 경로를 변경하지 않는다.
- listing에서 ZenRows 호출 없음. 키/.env 직접 열람·수정 없음.
- 연결: `seda/step01_main_list.py` → `seda/transport.py` → `seda/casas_bahia/listing_hybrid.py`.
- 기존 REST: `seda/casas_bahia/search_api.py`; Chrome/가격 응답 검증: `seda/casas_bahia/browser_listing.py`.
- 배치: `run_casas_bahia_tv_ref_ldy_full.bat` 그대로 실행. 검색 재시도는2(총3회)로 설정.

## 검증 기록

- 핵심 테스트60개 통과: hybrid25 + Chrome30 + 기존 실패로그5.
- Magalu listing 회귀38개 통과: deferred retry10 + transport fallback28.
- orchestrator18 + 기존 Casas discount10개 회귀 통과. 최종 코드로 선택한 관련 테스트126개를 한 번에 재실행하여 실패0/오류0 확인.
- Chrome 단위검사에서 실제 설치 major에 맞춘 드라이버 생성, 불일치 거부, 전용 드라이버 중복 종료 방지 검증.
- 저장 HAR 실제 원천28개/가격 응답1batch를 새 Chrome 파싱·병합 함수로 재처리해 기존 파서28개 확인.
- 실제 네트워크 dry-run은 TV main1~3페이지, 별도 출력 폴더, 상세/DB/업로드/ZenRows 실행 차단.
- 첫 dry-run (`C:\samsung_dx_seg\casas_hybrid_work\dry_run_20260923_01`): 페이지별REST3회 전부403. Chrome153/driver153 자동일치. page1가격연결 미완료, page2/3 각20개 성공. 총40행을partial에 보존하고 exit1/complete=false. 실패를 성공으로 기록하지 않음.
- 후속 URL 비교 (`first_page_comparison.json`): 동일 Chrome에서 `/tv/b`, `/tv/b?page=1` 모두 원천28/가격28 정확매칭 성공. 첫 실패의 정확한 누락ID는 당시기록에 없어 확정하지 않으며 URL 차이를 원인으로 단정하지 않음.
- 실패 진단에 가격 batch/상품/offer/누락ID/판매자불일치 개수를 추가. 비밀 헤더/쿠키/키 값은 기록하지 않음. 회복 가능한 응답 실패에 한하여 Chrome1회 재접속을 추가.
- 최종 dry-run (`C:\samsung_dx_seg\casas_hybrid_work\dry_run_20260923_02`): page1=28행, page2=20행, page3=20행, 총68행/고유상품66개. 세 페이지 모두 Partner GET3회 실패 후 Chrome SSR 성공. `complete=true`, 실패페이지0, exit0. 이번 최종 실행은 각 페이지 Chrome첫접속에서 완료(추가 재접속의 회복 동작은 단위검사로 검증).
- 최종 CSV: `main/parsed/main_occurrences.csv`; 상세 시도/가격 매칭 근거: `dry_run_result.json`; 로그: `dry_run.log`.
- 이번 dry-run은3페이지 한정이다. 전체20페이지·BSR·REF/LDY·GCP의 성공을 의미하지 않는다. 첫 실패의 특정원인(IP/지문/URL/광고/시간)은 확정하지 않는다.

## 아직 검증하지 않은 범위

- TV main20페이지 전체, BSR 실제 정렬/연속페이지, REF/LDY 실제 SSR 응답.
- GCP/headless 무인 운영과 Chrome 향후 업데이트 후 드라이버 다운로드 환경.
- 상세 모델SKU 품질 및 기존 연간전력사용량 단위 문제는 이번 listing 수정 범위 밖이다.
