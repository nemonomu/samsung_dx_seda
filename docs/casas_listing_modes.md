# Casas Bahia listing modes

2026-10-29 운영 BAT 기본값: **3 (Chrome + API)**. Python 직접 실행의 미설정 기본값은 **1**이다. Mode 1은 **9월 22일 이전 최신본 `1290145`의 REST 수집 동작**을 보존하며 명시적으로 인자 `1`로 실행한다. Mode 1-1/2/4, CSV·DB 형식과 공통 번역은 유지한다. [Mode 1 복원 계약](casas_listing_mode1_legacy.md), [Mode 1-1 계약](casas_listing_rest_url_first.md), [Mode 4 계약](casas_listing_url_first.md)을 참조한다.

## 2026-10-29 후속 복구 정책

아래 항목은 이 문서의 초기 모드 3 구현 설명 중 재접속 횟수·가격 실패 판정에 우선한다.

- 최초 홈페이지 확인 후 목록이 두 번 403이면 같은 Chrome에서 60초(30초마다 진행 출력)를 기다리고 한 번 더 접속한다. 최대 문서 탐색은 홈페이지 포함 4회이다. 다른 오류나 홈페이지 실패는 무한 재시도하지 않는다.
- 모드 3의 목록은 기존 상품군 필터를 가격 승인 전에 적용한다. 비대상 가구의 가격 불일치가 TV 수집을 막지 않는다. 페이지·정렬·중복 SKU·URL 일치 검증 및 기존 필터 후 순위는 유지한다.
- 대상 상품의 정확한 가격이 없거나 SKU가 다르면 원래 상품·SKU·URL을 보존하고 가격을 보류한다. 서로 독립적으로 식별된 두 가격이 같은 상품·판매자·결제 조건에서 같을 때만 기존 원본 SKU 가격을 사용한다. 한쪽 가격만으로 같다고 추정하지 않는다.
- 비교 로그에는 확인된 상품명·상품/SKU·판매자·가격·URL과 SKU 조회용 URL을 남긴다. 응답에 없는 다른 변형의 이름·실제 URL은 null이다. 원시 요청·쿠키·인증 값은 기록하지 않는다.
- `casas_mode3_price_pending`은 최종 CSV에서도 가격·절약액을 공란으로 유지하며 기존 DB 로더가 SQL NULL로 적재한다. 기본 append 방식과 컬럼은 변경하지 않는다.
- 상세 source는 기존 캐시/정상 ZenRows 결과를 사용하고 실패 시 Chrome API로 복구한다. 리뷰는 기존 직접 요청/ZenRows 실패 뒤 Chrome으로 복구한다. 추천·할인은 직접 요청 실패 뒤 Chrome으로 복구한다.
- 복구 Chrome은 프로세스당 하나를 재사용한다. 요청은 25초 제한, 같은 종류의 브라우저 요청 3회 연속 실패 시 해당 종류의 복구만 중단한다. 할인 보완도 3회 연속 최종 실패 시 남은 행을 보존하고 중단 사유를 남긴다.
- 리뷰는 최대 10페이지, 중복 페이지와 lastPage를 확인한다. 후속 페이지 실패 시 앞서 검증한 리뷰·요약을 보존하고 부분 성공을 표시한다. 이미 본문이 충분해도 요약이 없으면 요약을 요청한다.
- 기존 에너지·모델 등 필드 추출 방식은 변경하지 않는다. 운영 파일 실행 중 업데이트는 하지 않는다.
- 추천 API의 브라우저 실수집과 양수 할인 배지 실수집은 아직 미확인이다. 모의 응답 시험과 실제 RDP 결과를 구분한다. 작업·검증 기록은 [체크리스트](casas_mode3_recovery_work.md)를 참조한다.

| 번호 | 이름 | 실행 경로 | 실패 시 |
| --- | --- | --- | --- |
| 1 | REST API (1290145 복원) | 당시 Partner 검색 GET + 상품 ID 우선 가격 연결 | 상품 목록을 받으면 가격 오류만으로 실패시키지 않음. 재시도는 기존 환경 설정(기본 총 3회). Chrome 전환 없음 |
| 1-1 | REST API URL-first | Mode 1과 같은 검색 설정 + 정확한 SKU별 가격 연결 | 가격 미확인 상품도 URL·순위 유지. Detail에서 누락 가격·판매자 검증 및 보완. Listing Chrome/ZenRows 전환 없음 |
| 2 | hybrid | 기존 REST 최대 3회 → Chrome 실제 페이지 + SSR/가격 응답 | 기존 hybrid의 제한 재접속·실패 처리 유지 |
| 3 | UC + API | 홈페이지 → 목록 문서 준비 → 같은 Document에서 JS 검색 GET + 가격 POST | API의 기존 최대 3회 후 최종 HTTP 403이면 같은 Chrome으로 SSR 수집 복구. 초기 준비 실패는 SSR 수집으로 전환하지 않음 |
| 4 | UC + API URL-first | 전용 새 Chrome → 브라우저 검색 API, 검색 403 시 같은 Chrome SSR | URL 확보 시 가격·판매자 누락으로 실패시키지 않음. Detail의 남은 누락 가격만 기존 REST로 보완 |

## 실행과 최종 선택

```bat
rem 운영 BAT 기본값 3으로 TV -> REF -> LDY 전체 실행
run_casas_bahia_tv_ref_ldy_full.bat

rem 이번 실행만 모드 지정
run_casas_bahia_tv_ref_ldy_full.bat 1
run_casas_bahia_tv_ref_ldy_full.bat 1-1
run_casas_bahia_tv_ref_ldy_full.bat 2
run_casas_bahia_tv_ref_ldy_full.bat 3
run_casas_bahia_tv_ref_ldy_full.bat 4
```

배치 파일 상단 `set "SEDA_CASAS_BAHIA_LISTING_MODE=3"`이 운영 기본 선택이다. 첫 번째 인자를 주면 해당 실행에만 덮어쓴다. 대화형 입력을 요구하지 않으므로 예약 실행에도 사용할 수 있다. 허용 값은 1, 1-1, 2, 3, 4이다.

Python 모듈을 직접 실행할 때는 `SEDA_CASAS_BAHIA_LISTING_MODE` 환경변수로 선택하며, 없으면 기본 1이다. 이 변수는 listing 방식과 해당 모드의 Detail 가격 보완 여부를 선택한다. 전역 `SEDA_FETCH_MODE`를 이 변수로 대체하지 않는다.

각 Casas listing 코드 수정 시 기존 모드와 Mode 1-1을 유지하고, 최종 선택 값을 명시적으로 확인·기록해야 한다. BAT 기본은 3, Python 미설정 기본은 1이다. `4 --resume-tv-detail ...` 재개 명령은 기존처럼 Mode 4 전용이다. 실행 중인 배치의 모드는 바꾸지 않는다.

## 모드 4의 추가 동작

Mode 1의 복원 범위는 별도 [복원 계약](casas_listing_mode1_legacy.md)에 기록한다. Mode 1은 raw 재사용 및 일부 페이지 실패 처리도 당시 정책을 따른다. 아래 Mode 3/4의 엄격 검증·300/100 부분 성공 기준을 복원된 Mode 1에 적용하지 않는다.

- 모드 3의 연결·Chrome 준비 조건을 기반으로 별도 모듈을 사용한다. 모드 3의 기존 구현은 변경하지 않는다.
- Listing 성공은 상품 URL·기존 필터·페이지/정렬/순서 검증으로 판단한다. 가격·판매자 누락은 가격 보류 상태로 기록한다.
- 검색 최대 3회 후 최종 403이면 같은 Chrome SSR로 전환한다. 가격만 실패하면 검색이나 SSR을 반복하지 않는다.
- SSR Document가 완료되면 확보된 상품을 사용한다. 가격 응답 부재만으로 추가 대기/탐색하지 않는다.
- Main→BSR Chrome 공유와 main 300/BSR 100 기준은 모드 3과 같다.
- 모드 4는 기존처럼 Detail 후에도 최종 가격이 빈 행에 제한된 기존 가격 REST 보완을 수행한다. 이미 있는 값은 덮어쓰지 않는다. Mode 1-1의 별도 보완 로직을 이 모드에 적용하지 않는다.
- 진단·raw metadata·manifest는 모드 4를 별도로 기록한다. 모드 1/2/3과 raw를 섞어 재사용하지 않는다.

## 모드 3의 정확한 범위 (기존 동작 유지)

- 2026-10-29 관측된 SSR의 `props.initialState.search`도 모드 3에서 읽는다. API 준비 단계는 두 구조를 직접 인식한다. SSR 수집 복구에서는 기존 `props.pageProps.initialState.search`가 없는 경우 같은 상태를 파서 경로에 연결하며, 원래 HTML·상품 순서·응답의 page/sort를 보존한다. 실제 수집 결과의 페이지·정렬·상품/SKU·판매자·가격 검증과 Mode 1/1-1/2/4의 동작, 기본 선택 **1**은 유지한다.
- 후속 수정에서는 새 Chrome에서 홈페이지를 먼저 열어 정상 Document를 확인하고 5초 후 요청한 목록으로 이동한다. HTTP 200·새 top-frame/loader·완료된 비캐시 응답·NEXT_DATA·요청 페이지/정렬·상품/SKU를 확인하되 초기 SSR 가격/판매자 응답은 API 진입 조건으로 사용하지 않는다. 초기 SSR 상품은 수집 결과에 넣지 않는다. 검색 GET과 가격 POST로 실제 수집할 상품·SKU·판매자·가격을 기존 기준대로 검증한다. RDP의 수동 경로는 20행 검증에 성공했으며, 수정된 운영 코드의 실수집은 별도 확인해야 한다.

- 실행 조건은 성공한 로컬 TV 1→2→3→2 실험에 맞춘다. 시작 로그의 `execution_profile=successful_local_probe`로 확인한다. 세 번째 모드의 조건 정렬이지 새로운 네 번째 모드는 아니다.
- 매 listing 프로세스에 전용 새 Chrome 프로필을 사용한다. 기존 Chrome 계정 로그인·쿠키·사용자 프로필에 의존하지 않는다. 성공한 로컬 실험도 새 프로필이었다.
- Chrome은 창이 있는 방식이며 `--disable-gpu`, `--no-sandbox`, `--no-first-run`, `--no-default-browser-check`와 실험의 실행 인자를 사용한다. 모드 3에는 기존 headless 설정을 적용하지 않는다.
- 설치된 Chrome과 major가 맞는 드라이버를 복사해 전용 경로에서 사용한다. 일치하는 기존 드라이버가 없으면 해당 major를 전용 캐시에 준비한다. 기존 UC 전역 설정이나 다른 브라우저는 변경하지 않는다. 종료 시 자신이 만든 임시 프로필·드라이버 경로만 정리하며, 안전하게 종료/정리하지 못하면 해당 경로를 보존한다.
- 각 초기 문서 접속 제한 45초, 접속 후 Document 준비 대기 30초(SSR 수집 복구에서는 가격 검증 포함), 각 검색/가격 API 제한 25초, 스크립트 제한 30초로 고정한다. 모드 1·2와 detail의 설정에는 영향을 주지 않는다.
- 검색 요청의 상품 수 20, variant `q2`, region `126000`, 빈 user ID를 사용한다. 세션 ID는 해당 프로세스의 API 세션마다 새로 생성해 재사용하며 출력하지 않는다. 가격 요청 region도 `126000`이다. 요청 사본에만 적용하며 `.env`나 전역 환경변수를 수정하지 않는다.
- 이외의 검색 URL/검색어, 가격 API 우편번호·UTM 등의 기존 설정은 유지한다. 실험도 기존 프로세스 환경 자체를 비우지는 않았으므로 당시 값과 GCP의 모든 설정값까지 동일하다고 간주하지 않는다. 키·환경 파일을 열어 비교하지 않는다.
- 처음 API 검색은 초기 검증 완료 후 최소 5초, 이후 검색은 직전 검색 완료 후 최소 5초 간격이다. 실험의 수동 명령 대기시간 전체를 재현하는 것은 아니며 코드에 명시된 최소 간격을 자동 적용한다. TV/REF/LDY별 검색어와 main/BSR 정렬은 기존 수집 대상을 유지한다.
- 최초 실제 페이지는 브라우저 준비 단계이며, 정상 API 경로에서는 그 SSR 상품을 결과로 사용하지 않는다. HTTP 403 복구 경로에서 별도로 검증된 SSR 결과만 수집 결과로 채택한다.
- 정상 API 경로는 같은 Chrome/Document를 유지하고 페이지마다 `driver.get()`을 반복하지 않는다. HTTP 403 복구 때만 해당 페이지로 실제 이동한다.
- 전체 배치의 모드 3은 각 상품군의 **Main → main_targets → BSR**을 단일 listing worker에서 순차 실행한다. Main 종료 시 Chrome을 닫지 않으며 같은 Chrome으로 BSR 정렬 API를 요청한다. CSV·manifest·진단 ZIP·unique 집계는 단계별로 분리한다. BSR 종료 또는 worker 실패/중단 시 소유 Chrome을 정리하고, detail·DB 등은 기존 별도 프로세스로 실행한다. TV/REF/LDY 간에는 Chrome을 공유하지 않는다.
- 특정 단계만 선택한 실행도 선택된 순서·범위를 유지한다. 모드 1·2 및 Magalu의 프로세스 구조는 변경하지 않는다. 개별 `step01_main_list`/`step03_bsr_list` 모듈을 따로 실행하면 그 프로세스 종료 시 Chrome을 닫는다.
- 직접 Python REST 또는 ZenRows로 자동 전환하지 않는다. 모드 2 전체를 다시 호출하지 않고 **Chrome 실제 페이지 + SSR/가격 응답 검증 부분만** 재사용하므로 Python REST 최대 3회가 추가되지 않는다.
- 검색 또는 가격 API의 기존 재시도를 소진한 **최종 상태가 HTTP 403**일 때 SSR로 전환한다. 중간 403 뒤 API가 회복되면 SSR을 사용하지 않는다. 초기 목록 Document가 403이면 같은 Chrome에서 3초 후 한 번만 다시 준비한다. 홈페이지 실패 또는 초기 준비 최종 실패는 API를 호출하지 않고 실패를 재사용하며, 매 페이지 SSR 수집 복구를 반복하지 않는다. API 진입 후의 403 복구 정책은 유지한다.
- SSR 복구는 모드 2와 같은 탐색 정책(최초 탐색 + 기존 복구 가능 오류에 한해 3초 후 1회 재탐색)을 사용한다. 브라우저를 새로 만들거나 재시작하지 않으며, 모드 3의 접속 45초·응답 검증 30초 제한은 유지한다. 실제 페이지·정렬·상품/SKU/판매자·가격 검증에 실패하면 성공 처리하지 않는다.
- SSR 성공 후 현재 Document 식별자와 검색 대기 기준을 갱신하고 다음 페이지는 API를 우선 시도한다. SSR 실패로 검증되지 않은 문서에 머무르면 다음 페이지도 같은 Chrome의 SSR 복구를 시도하며, 정상 문서가 검증되기 전에는 그 문서에서 API를 호출하지 않는다. 이 상태는 Main→BSR에도 유지된다.
- API와 SSR에서 이미 채택한 페이지들의 전체 SKU 집합 반복 검증을 공유하되 Main/BSR 정렬별로 분리한다. 결과의 실제 방식은 `uc_api` 또는 `uc_api+browser_ssr`로 기록하며 기존 403 이력과 SSR 결과를 모두 남긴다.
- 검색 기본 최대 3회(최초 1 + 재시도 2), 재시도 기본 간격 3초/6초. 가격 요청은 검색 성공 시 별도 POST다. CORS OPTIONS는 브라우저가 추가할 수 있으므로 검색 상한이 전체 HTTP 요청 수와 같지는 않다.
- `SEDA_CASAS_BAHIA_SEARCH_RETRIES`는 0~2로 낮출 수 있지만 2보다 높아도 검색 3회 상한은 유지된다. 배치는 2로 지정한다.
- 각 성공 로그에 `mode=3`, 페이지, 검색·가격·총 소요시간을 기록한다. 실패는 단계, 안전한 오류 코드, HTTP 상태를 기록한다.
- 기존 브라우저 쿠키를 추출하거나 기존 사용자의 Chrome에 연결하지 않는다. 브라우저 fetch는 검증된 `credentials=same-origin` 조건을 사용한다.

## 정합성·출력 보호

- API는 성공 실험과 같이 endpoint·검색 page에 해당하는 요청 ID의 GET/POST 200과 브라우저가 읽은 JSON 응답을 확인한다. `responseReceivedExtraInfo`의 200도 근거로 인정한다. OPTIONS 200만으로 검색/가격 성공으로 판정하지 않는다.
- 각 API 호출 전후 동일 frame/loader와 호출 중 Document 이동 없음을 확인한다. 실험에 없던 API `loadingFinished` 필수 조건, 전체 query/body 일치, 응답 정확히 1개 조건은 추가 성공 판정으로 사용하지 않는다. **초기 Document 완료·상품 검증과 실제 수집할 API/SSR 결과의 가격 검증을 분리**하며, 초기 문서의 가격 누락만으로 API 진입을 막지 않는다.
- 실험과 동일하게 브라우저 캐시 비활성화·Service Worker 우회 및 fetch `cache=no-store`, `credentials=same-origin`을 유지한다.
- REST `sku`를 복사본의 `idSku` 별칭으로 대응시키고 URL·상품·SKU·판매자·가격을 정확한 ID로 검증한다. 충돌이나 누락은 실패다.
- 응답이 page/sort를 생략하면 실제 요청 근거만 있음을 trace에 기록한다. 파서용 요청 page/sort를 넣은 것을 서버가 응답한 값으로 표현하지 않는다. 명시적 page/sort 불일치는 거부한다.
- 다른 페이지가 이전 페이지 전체 SKU 집합을 그대로 반복하면 실패 처리한다.
- 출력 CSV 계약과 기존 관련 상품 필터를 유지한다. 실패 페이지가 있어도 필터링 후 누적 unique가 main 300개 / BSR 100개 이상이면 성공분으로 후속 단계를 진행한다. 실패 내역과 `complete=false`는 유지하고 `accepted_with_failures=true`, `downstream_allowed=true`로 구분하며 final CSV와 `.partial.csv`를 함께 저장한다. 실패가 있고 기준 미달이면 기존처럼 downstream을 중단한다. 이 기준 자체가 페이지 조기 종료 조건은 아니다.
- 기존 배치의 각 main/BSR listing 종료 시 `seda/casas_bahia/log/`에 진단 ZIP을 자동 생성한다. 별도 진단 배치는 필요하지 않으며 모드 3의 기존 요청에 대한 관측만 추가한다. 자세한 항목과 보안 범위는 `docs/casas_listing_diagnostics.md`를 참조한다.
- raw 옆 `.mode.json`에 선택 모드와 source URL을 기록한다. Mode 1-1/2/3/4는 모드가 다르거나 메타데이터가 없는 raw를 재사용하지 않는다. Mode 1은 명시적으로 raw 재사용을 요청하면 과거처럼 재파싱하므로, 다른 모드의 raw를 섞어 사용하지 않는다.
- manifest의 `casas_listing_mode`, `casas_listing_mode_label`, `fetch_mode`로 선택과 실제 경로를 확인할 수 있다.

## 검증 범위와 주의사항

실행 조건 정렬과 초기화 진단 변경은 오프라인 회귀 테스트로 확인한다. Chrome 준비·접속·검증 대기 소요시간과 완료/실패/취소 이벤트를 기록하고, 실패 후 문서 준비 상태만 별도로 조회한다. 키·쿠키·응답 본문을 진단 로그로 출력하지 않는다. 진단 실패가 원래 수집 판정을 바꾸지 않도록 한다.

실행 조건을 맞췄다는 사실은 GCP 실수집 성공을 의미하지 않는다. GCP 네트워크·Chrome 빌드·머신 환경까지 동일하다고 검증한 것은 아니다. 기존 최대 3회 재시도, 전체 배치 자동 진행과 TV 이외 제품군/BSR 확장은 수동 TV 실험과 구분한다.

전용 진단에서 TV 1→2→3→2 페이지의 브라우저 내부 검색·가격 호출이 모두 성공했다. 각 API 응답 합계는 약 3.81~5.88초였고 최초 실제 페이지 검증은 별도 19.061초였다. 이 시간에는 Chrome 시작·파싱·명령 대기·요청 간격이 포함되지 않는다.

**SSR 28개와 REST 27개의 상품 구성·순서가 달랐다.** 공통 17개는 가격·판매자가 같았지만 전체 결과가 동일하다고 검증된 것은 아니다. 사용자 요청으로 최종 모드 3을 선택하며, 이 차이가 해소됐다고 간주하지 않는다. 전체 페이지·BSR·REF/LDY·장시간 운영의 안정성은 개별 실측 결과와 구분한다.

모드 변경 후 재수집은 이 배치의 `--all` 경로를 사용한다. 별도 `--resume`의 기존 완료 CSV 판단은 모드 변경까지 추적하지 않으므로, 이전 모드 결과를 새 모드로 재수집하는 용도로 사용하지 않는다.

이 배치는 dry-run이 아니며 이후 업로드·DB 적재까지 진행한다. 진단 시에는 별도 출력 폴더의 listing 단계만 실행하고 전체 배치를 호출하지 않는다.

구현: `listing_modes.py`, `browser_api.py`, `browser_probe_session.py`, `listing_worker.py`, 기존 `listing_hybrid.py`/`browser_listing.py`, 공통 `orchestrator.py`/`retailer_runner.py`/`step01_main_list.py`와 `transport.py`.
