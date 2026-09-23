# Casas Bahia listing 자동 진단 ZIP

별도 진단 배치를 실행하지 않는다. 기존 전체 수집 배치를 평소처럼 실행한다.

```bat
run_casas_bahia_tv_ref_ldy_full.bat
```

TV·REF·LDY의 **main / BSR listing 단계가 끝날 때마다** 해당 실행의 진단 ZIP을 자동 생성한다.
페이지 실패나 정상적인 Ctrl+C 중단 때도 종료 처리를 통해 가능한 진단 자료를 남긴다.
ZIP 위치는 `C:\samsung_dx_seda\seda\casas_bahia\log\`이며,
파일 이름은 `casas_listing_TV_main_<UTC시각>_<식별자>.zip` 형태다.
완료 로그의 `diagnostic_zip=...`에 표시된 파일만 전달하면 된다.
모드 3은 같은 상품군의 Main과 BSR이 Chrome을 공유하지만 ZIP은 여전히 단계별로 생성된다.

ZIP에는 허용한 정보로만 생성한 `REPORT.md`, `report.json`, `events.jsonl`이 들어간다.
증거 이벤트는 실행 중 `log\diagnostics\<실행식별자>\events.jsonl`에 즉시 기록한다.
기존 로그 폴더 전체를 압축하거나 환경파일/HAR/프로필을 수집하지 않는다.
강제 프로세스 종료·전원 종료 때는 종료 처리가 실행되지 않아 ZIP이 없을 수 있으며,
디스크 오류로 저장이 실패하면 `diagnostic_start_failed` 또는 `diagnostic_zip_failed`를 출력한다.
진단 기록 실패만으로 원래 수집 결과를 바꾸거나 추가 요청을 보내지는 않는다.

## 기록 항목

- 실행 품목, main/BSR, 실제 모드, 기존 REST 요청 크기와 모드 3 요청 크기.
- 페이지별 결과, 필터링 후 누적 unique, 실패 페이지와 원래 재시도 trace.
- 모드 3의 실제 API 호출 순서·간격·시간, 검색 GET / 가격 POST / OPTIONS 상태 구분.
- 이미 받은 네트워크 이벤트의 완료/실패/취소, 캐시 여부, 허용한 오류·MIME·프로토콜 분류.
- 모드 3의 API 응답 상품 수, 기존 파서 기준 광고 표시, 품목 필터 통과 여부,
  숫자형 상품/SKU/판매자 ID. 제목·전체 URL·본문은 공유하지 않는다.
- 최초 브라우저 초기화 실패가 다음 페이지에 재사용됐는지, 페이지에서 실제 새 API 호출이 있었는지.
- 단계 시작 시 Chrome 재사용 여부와 정상 문서 검증 상태를 불리언으로 기록한다.
- API 403 원본 이력, 같은 Chrome의 SSR 복구 결과·탐색 횟수·실제 수집 방식(`uc_api+browser_ssr`)을 기록한다.
- SSR 실패 후 다음 페이지에서 실제 탐색을 시도하는 상태는 단순한 초기화 오류 재사용과 구분한다.
- 모든 페이지 성공 여부와 최소 수량 충족에 따른 후속 단계 허용 여부를 구분한다.

모드 1·2도 단계별 집계 ZIP을 만든다. 상세 브라우저 API 관측은 모드 3에만 적용되므로
다른 모드에서 API 이벤트가 없다고 실제 요청이 없었다고 해석하면 안 된다.
원래 콘솔 로그는 그대로 출력하며, 진단을 위해 추가 HTTP/CDP 호출이나 `get_log` 호출을 하지 않는다.
진단 기록 자체는 기본 모드·요청 개수·타임아웃·간격·재시도·상세 수집 방식을 변경하지 않는다.
모드 3의 403→SSR 복구 및 Main→BSR Chrome 재사용 정책은 `casas_listing_modes.md`를 따른다.
SSR 페이지의 탐색 과정에서 사이트가 보내는 요청은 collector API 호출 개수와 다르다.
Main/BSR API 타임라인의 경과시간과 호출 번호는 각각의 진단 단계 시작 기준이다.
통계 계산과 파일 기록에 걸리는 소량의 시간은 추가될 수 있다.

## 실패 페이지가 있을 때의 진행 기준

| 단계 | 필터링·중복 제거 후 누적 unique | 진행 |
| --- | --- | --- |
| main | 300 이상 | 실패 페이지가 있어도 성공분으로 BSR 진행 |
| BSR | 100 이상 | 실패 페이지가 있어도 성공분으로 detail 진행 |
| 각 단계 | 실패 페이지가 있고 기준 미달 | 기존처럼 중단 |

이 기준 자체로 페이지를 조기 종료하지 않는다. 기존에 별도로 설정한 페이지 범위/unique target 규칙은 유지한다.
main과 BSR의 수는 따로 계산하며 다른 품목·retailer의 수를 합산하지 않는다.
실패가 없는 실행은 기존 완료 조건을 유지한다.

기준을 충족한 불완전 수집은 실패 내역과 `complete=false`를 유지하면서
`accepted_with_failures=true`, `downstream_allowed=true`로 표시한다.
현재 성공분을 후속 단계용 `main_occurrences.csv`와 증거용 `main_occurrences.partial.csv`에 저장한다.
이전 실행의 final CSV가 있다면 현재 실패가 있는 실행에서는 `main_occurrences.previous.<시각>.csv`로 보존한다.
키 값이 아니라 기존 listing 로그의 `unique` 계산과 같은 `product_identity` 기준을 사용한다.

## 보안 및 원인 해석

키 값·쿠키·인증 헤더·세션 ID·IP·원시 본문·사용자 경로를 ZIP에 넣지 않는다.
기존 공통 설정 로더 외에 환경파일을 열거나 전체 환경변수를 조회/복사하지 않는다.
허용한 숫자/불리언/오류 코드만 공유 파일에 쓰고, ZIP 작성 시 한 번 더 정제한다.

HTTP 403은 요청 거부 사실이지 IP 차단/세션 만료/호출 빈도 중 특정 원인의 증명은 아니다.
광고 표시 개수는 관측값이며 서버의 페이지 계산 규칙까지 증명하지 않는다.
실제 화면과 API의 상품 구성 차이는 같은 시점의 상품 ID 비교가 필요하다.

별도 진단 실행 파일 `run_casas_bahia_listing_diagnostic.bat`과 독립 수집 CLI는 사용하지 않으며 제거했다.
