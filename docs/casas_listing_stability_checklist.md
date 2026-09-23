# Casas Bahia listing 안정화 검증 체크리스트

작성일: 2026-09-23
검증 산출물 폴더: `C:\samsung_dx_seg\casas_listing_verification`

## 최신 구현 업데이트 (2026-09-23)

사용자 요청에 따라 **REST API + SSR Document parsing hybrid**를 main/BSR listing에 연결했다. 아래 단계표는 기존 원천 검증 기록이며, 현재 구현/실행 규칙은 `docs/casas_listing_hybrid.md`를 우선 참조한다.

- REST검색 최대3회 → 실패 시 실제 ChromeSSR/브라우저 가격 응답. Chrome/driver major 자동일치. 복구 가능한 응답 실패만 Chrome1회 추가 재접속 허용.
- 첫 실제dry-run은 page1가격매칭 실패, page2/3 각20개 성공. 40행을partial로 남기고 exit1로 종료한 사실을 보존했다.
- 진단 보강 후 최종dry-run TV main1~3페이지는28/20/20, 총68행으로 완료, `complete=true`/exit0. REST는 각3회 전부403, Chrome은 각첫접속에서 성공. URL/가격/판매자 검증 및 기존 출력컬럼/순위 유지.
- 최종 관련 테스트126개 통과. 상세/DB/업로드/ZenRows는 이번 실행에서 수행하지 않았다.
- 최종 증거: `C:\samsung_dx_seg\casas_hybrid_work\dry_run_20260923_02\dry_run_result.json`.
- 아직 TV20페이지 전체/BSR 실시간정렬/REF·LDY 실제SSR/GCP 무인환경은 미검증이다.

## 판정 원칙

- `통과 / 실패 / 진행 중 / 미실행 / 선행 조건 미충족`만 사용한다. 추정 원인을 결과로 기록하지 않는다.
- 각 실행은 대상, 전송 방식, 시각, 횟수, 실제 HTTP 상태, 상품 수, 검증 결과를 기록한다.
- HAR 재처리 성공, 실시간 1회 성공, 반복 수집 성공을 구분한다.
- HTTP 200만으로 성공 처리하지 않는다. 상품 데이터, 요청 페이지, ID와 URL, 필수 보강값을 검증한다.
- 서버 재획득 검증에서는 캐시를 끄고, Document와 가격 응답 모두 현재 최상위 frame/loader와 연결되어 완료됐는지 확인한다. 이전 회차 응답이나 캐시 성공은 새 서버 응답 성공 횟수로 세지 않는다.
- 키 값과 파생값 및 `.env`는 직접 열람/검색/출력/복사/변경하지 않는다. 키 사용이 필요한 경우 승인된 기존 환경 로딩·중앙 클라이언트만 사용한다.
- 관련 작업 전 `rg -n "ZenRows-related|Never directly read|Never open|Never create|Approved loading contract" AGENTS.md docs/zenrows_api_key_policy.md`를 실행한다.
- HAR의 인증 헤더·쿠키·세션·사용자 식별값을 보고서/로그/새 산출물에 저장하지 않는다.
- Listing 출처 변경이 전역 fetch mode나 기존 Detail/DB 처리까지 바꾸지 않도록 한다.

## 단계별 통과 기준

| 단계 | 확인 방법 / 통과 기준 | 상태 | 증거 / 남은 일 |
|---|---|---|---|
| 1. 정상 원천 확인 | 제공 HAR의 실제 `/tv/b` Document 200, SSR 상품 배열 및 URL/ID 확인 | 통과 | `casas_main_all_260923.har`: Document 906,894자, 상품/URL/내부 SKU 28개 |
| 2. 기존 파서 호환 | 동일 HAR에서 SSR과 tracking의 상품 ID·상품명·순서 대조, 누락/중복 확인 | 통과 | 기존 파서 28행, tracking의 28개와 순서 일치; 제조사 모델 SKU의 정확성 검증과는 별개 |
| 3. 가격/판매자 계약 | SSR→가격 요청 형태를 실제 HAR와 비교. 응답을 SKU/상품 ID로 병합하고 가격·판매자 대조 | 통과 | 오프라인 요청 변환66검사 통과. repeat_run_04 7회 모두 해당 navigation의 가격POST200, 기존 병합 후 원천 상품 전체 가격/판매자/ID 일치. 직접 Python 가격 POST 및 운영 코드 연결은 이 통과 범위에 포함하지 않음 |
| 4. 자동 브라우저 기동 | 설치 Chrome과 드라이버 주요 버전 일치, 세션 생성 성공 | 통과 | Chrome 153 + 드라이버 153 시험 성공. 기존 자동 버전 결정의 운영 수정은 미실행 |
| 5. 실제 Document 1회 획득 | 자동 브라우저 실제 HTTP 200 + SSR 상품 배열 + 유효 URL/ID | 통과 | repeat_run_04에서 현재 최상위 frame/loader의 완료된 실제 HTTP 응답 본문을 사용함. DOM page_source만으로 판정하지 않음. 이전 headless403과 조건/시각이 달라 성공 원인은 미확정 |
| 6. 반복·페이지 전환 | 캐시를 끈 같은 구성에서 page 1 5회, page 2/3 전환; 현재 navigation의 실제 Document/가격 응답, 페이지·URL/ID 검증 | 통과 | repeat_run_04: 1페이지5/5(27,28,28,28,27개), 2페이지20개, 3페이지20개. 총7/7 HTTP200·원천/파싱 수·ID순서·가격/판매자 일치. 로컬 headed의 이 실행만 검증, 전체20페이지/GCP/다른 세션·날짜의 안정성은 미검증 |
| 7. 기존 Detail 호환 | 검증한 listing 표본을 기존 상세 처리에 전달하고 ID/판매자 일치, 필드 누락 비교 | 진행 중 | 서로 다른 seller 표본3개 ProductSource direct HTTP200/ID일치/기존TV병합 후 식별자보존. 정확 제조사모델 검증0/3이며 전체 배송/픽업/유사/리뷰/할인 live 조합은 미실행 |
| 8. 실패 처리·저장 | 403/빈 상품/누락 보강/다른 페이지를 성공으로 기록하지 않음. 페이지별 결과 즉시 로그, 제한 재시도, 부분 결과 구분 | 미실행 | 기존 5회 설정·페이지 실패 로그 외 SSR 운영 처리는 아직 연결하지 않음 |
| 9. 운영 적용 | TV 20페이지 검증, REF/LDY 각 원천/페이지/필드 검증 후 배치 연결. 기존 상세/출력/DB 계약 회귀 확인 | 선행 조건 미충족 | 원천 형식 및 전송 안정성 확인 전 전역 모드 변경 금지 |

## 추가 운영 승인 조건

- [ ] `discount_type`/할인·광고 배지/리뷰 등 각 필드의 기존 원천과 SSR+후속 응답 원천을 대조한다. 원천에 없는 값을 채우거나 광고 전체 플래그로 대체하지 않는다. 할인은 오프라인 계약 검증만 통과(아래 기록); 실시간 후처리는 미실행.
- [ ] 로컬 headed 브라우저 결과를 GCP 무인/headless 수집 성공으로 간주하지 않는다. 실제 운영 환경에서 별도 반복 검증한다.
- [ ] 드라이버153 고정은 이번 진단 조건이다. Chrome 업데이트 이후 버전 불일치 시 명시적 오류/자동 호환 처리를 검증한다.
- [ ] 페이지당 상품 수는 원천 배열과 비교한다. 27/28 등 특정 수를 모든 페이지의 고정 통과 기준으로 사용하지 않는다.
- [ ] 한 번의 실행과 여러 날짜/세션의 안정성을 구분한다. 요청 간격과 성공의 인과관계를 이번 표본으로 단정하지 않는다.

## 3단계 세부 작업

- [x] SSR만 파싱하면 가격 0/28, seller_id 8/28임을 확인.
- [x] HAR 가격 요청: seller 미지정 상품 ID 20개 + seller 명시 `(idSku, idLojista)` 8개임을 확인.
- [x] 기존 함수에 SSR을 그대로 전달하면 상품 ID 28개 + SKU 조합 0개가 생성됨을 확인.
- [x] 저장된 가격 응답을 기존 정규화/병합 함수로 처리하여 가격·판매자·URL 내부 SKU 28개 일치 확인.
- [x] SSR 전용 요청 변환을 검증 폴더에 구현하고 실제 HAR와 동일하게 검증. 기존 Partner 입력 코드 미변경. 66검사 통과, 잘못된 입력54건 거부.
- [x] 실제 브라우저 가격 POST 응답과 상품/판매자 매칭 검증. 현재 navigation의 완료된 응답만 사용.
- [x] 진단 행의 seller는 가격 응답과 일치하며 기본 판매자 일괄 대체 없이 채움. SSR seller가 명시된 경우 동일 seller인지도 확인.
- [ ] 운영 연결에서도 판매자 미확인 값을 기본 판매자로 일괄 확정하지 않도록 실패/보류 정책 검증.

## 현재 확인된 사실과 한계

- Partner API가 27개를 반환하고 파싱/저장한 성공 사례가 있다. 전체 페이지·반복 실행의 안정성 증명은 아니다.
- 제공 All HAR의 SSR 목록은 실제 화면 노출 목록 28개와 일치한다.
- SSR 상품에는 가격이 없고 후속 가격 API가 가격/판매자를 채운다.
- SSR의 `hasSponsorProducts` 및 tracking의 `IsSponsored`는 이 캡처에서 전체 상품이 true다. 개별 광고 여부 판정에 그대로 사용하지 않는다.
- 현재 검증된 개별 광고 표시(`tagName`/`advertasingEvents`)는 8개다.
- 403 차단의 구체 원인(IP, 시간, 브라우저 조건 등)은 미확정이다. 현재 결과로 특정 원인이나 해제 간격을 단정하지 않는다.
- 수분 간격 5회 전부 403이었던 결과는 해당 실행에서 성공하지 못했다는 의미다. 일반적인 cooldown 존재/부재를 증명하지 않는다.

## 증거 파일

- 원본 HAR: `C:\samsung_dx_seda\references\casas_main_all_260923.har`
- 오프라인 보고서: `C:\samsung_dx_seg\casas_main_all_260923_audit.json`
- 오프라인 미리보기: `C:\samsung_dx_seg\casas_main_all_260923_preview.csv`
- 실제 접근의 새 실행 결과는 이 디렉터리에 상태·개수 위주 JSON으로 저장한다.
- 자동 headed 1회: `headed_document_result.json` (네비게이션 명령 1회, 브라우저 내부 Document 200 응답 기록 2개; HTTP 응답 수와 실행 명령 수를 구분).
- SSR 가격 요청 오프라인 검증: `price_contract_verification.json`.
- Destaque 할인 후처리 오프라인 검증: `destaque_contract_verification.json` (저장 응답 주입166검사 통과, 실시간 호출 증거 아님).
- 현재 navigation/캐시 배제 후 반복 검증: `repeat_run_04/result.json` 및 회차별 CSV.
- 직접 ProductSource+기존TV병합 표본 검증: `detail_source_result.json`.

## 이번 진행 기록

- 체크리스트 생성. 1~3단계 저장 증거를 재검증한 뒤, 5단계에서 기존 headless 실패와 비교할 독립된 브라우저 실행을 1회 수행한다. 새로운 결과가 없으면 성공으로 승격하지 않는다.
- 자동 headed 1회 수집에 성공하여 5단계를 통과 처리. 같은 구성을 사용한 반복 및 페이지 전환 검증 시작.
- `repeat_run_01/result.json`: 새 세션에서도 실제 Document body 200/SSR page1/27개 확인. 가격 응답 미포착으로 1회에서 중단(5회 성공 아님).
- `price_observation_run_02`: 30초 관찰에서 Document200/28개 및 가격POST200 확인. 브라우저가 받은 실제 응답을 기존 병합 함수에 전달해 가격/판매자/상품ID28개 일치. 직접 Python 가격 POST를 성공시킨 시험은 아님.
- `repeat_run_03`: 원천 Document 본문과 parser의 상품ID 순서 및 가격/판매자 일치 검증. 다만 가격 응답의 현재 navigation 연결 조건 누락 및 4회차 Document 캐시를 발견하여 최종 반복 성공으로 채택하지 않음.
- navigation 판정 함수 단위검사 4건 통과: 현재 회차만 허용, 이전 loader/다른 frame/식별자 누락은 거부.
- `repeat_run_03/sequence_05_page_01.csv`: 가격 병합 후28행의 상품명/URL/원가/최종가/판매자28, savings26, sku_status8, discount_type0. 미보강 listing 진단 산출물이므로 전체 수집 항목 호환은 미통과다.
- 코드 확인: `step08_detail_enrichment._merge_casas_bahia_apis`는 ProductSource/배송/픽업/유사/리뷰를 처리한다. Destaque 할인은 별도 `listing_discount_backfill` 후처리이므로 기존 Detail만 유지했다고 할인 처리까지 검증된 것은 아니다.
- Destaque 오프라인: SSR28/가격offer28/Destaque응답28의 (내부SKU,판매자) 정확 일치, 누락0/추가0. 28응답 모두 캡처 당시200. 배지20개 중 쿠폰19개(5%3개/10%16개), 나머지1개는 `Lançamentos` 출시 표시로 할인 공란. 기존 후처리 기본/force 모두19개 보강·9개 할인없음. 광고 `sku_status` 및 기타 필드 보존. 166검사 통과. 기존 모듈 전체 실행이 아닌 선택된 함수의 저장 응답 주입 검증이다.
- `repeat_run_04`: 2026-09-23 19:25:09~19:30:19 KST, Chrome153 headed, 캐시 비활성화·service worker 우회, page1 다섯 번 후 page2/3. 모든 최종 Document loadingFinished 및 raw body 확인; SSR query.page/URL page 일치. 가격 request와 response 모두 같은 최상위 frame/loader이고 POST200 완료. 7/7 통과. page2는 page1과1개, page3은 page1 및 page2와각1개 상품 중복이며 전체 동일 페이지 반복은 아님.
- `repeat_run_03`은 예비 결과로만 보존: 4/5회차의 캐시 응답 및 가격 응답 navigation 결합 미검증 때문에 최종 반복 횟수에서 제외.
- 브라우저 진단 종료 후 UC 소멸자의 `quit()`에서 WinError6 경고가 출력됨. 결과JSON은7회 완료·success=true로 저장됨. 운영 도입 전 브라우저 생명주기/종료 경고 처리도 검증 대상.
- 상세 표본3개는 캐시/ZenRows를 차단하고 direct1회씩만 호출, redirect 금지, DB main 미실행. 모두200, 반환 내부SKU/상품ID와 listing ID일치. 제조사 모델용 `detail['sku']`는3개 모두 공란이고 기존 병합은 listing 모델명을 유지; 정확모델 확인 성공으로 확대해석하지 않는다.
- 상세 기존 파서 및 병합 결과에서 `estimated_annual_electricity_use`에 `80 W`/`130 W`가 저장되는 동작을 확인. 연간 사용량 컬럼과 단위 계약 검증 필요. 이번에 임의 수정/환산하지 않음.

## 다음 진행 순서

1. 7단계 계속: 기존 상세/배송/픽업/유사/리뷰와 Destaque 실시간 후처리를 소수 표본으로 각각 검사. 결과 원천과 최종 필드를 대조하고 제조사SKU·연간전력사용량 품질 문제는 원천 근거로 별도 판정한다.
2. 8단계: SSR 연결부를 운영에 넣기 전 403/빈목록/다른page/가격부분누락/ID불일치의 명시적 실패·즉시로그·제한재시도·부분파일 구분을 검사한다. 기존 global fetch mode/detail/DB 동작을 임의 변경하지 않는다.
3. 9단계: TV main20페이지와 전체순위/중복 처리 검증 후 BSR 정렬·브랜드별목록의 기존 계약 확인. REF/LDY는 각각 원천/가격/모델/필드 계약을 별도 검증한다.
4. 실제 운영 대상 GCP/무인 환경과 여러 세션·시점에서 재검증한 뒤 배치에 listing-only로 연결하고 출력/DB 계약 회귀를 확인한다. 로컬 headed 성공만으로 무인 운영 성공을 선언하지 않는다.
