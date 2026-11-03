# Casas Bahia 가격 복구

기존 브랜치의 `run_casas_bahia_price_recovery.bat`로 **판매가가 비어 있는 TV·REF·LDY 상품만** 다시 조회한다. 기존 전체 수집 BAT와 분리된 수동 실행 도구이다. 기본 실행은 검증된 가격을 별도 CSV에 저장하고 기존 DB 행을 UPDATE한다. INSERT·DELETE·TRUNCATE·전체 수집은 하지 않는다.

## 실행

RDP PowerShell에서 기존 수집이 끝난 뒤 실행한다. 날짜는 복구 실행 날짜가 아니라 **원래 수집 폴더 날짜**이다.

```powershell
Set-Location C:\samsung_dx_seda
.\run_casas_bahia_price_recovery.bat REF 20261101
.\run_casas_bahia_price_recovery.bat LDY 20261102
.\run_casas_bahia_price_recovery.bat TV 20261102
```

인자 없이 실행하거나 더블클릭하면 저장된 최종 CSV를 찾아 **상품군·최초 수집 시각·배치 ID·판매가 빈 항목 수**를 번호 목록으로 보여준다. 복구할 번호 하나를 선택하면 별도 처리 방식 질문 없이 바로 재조회하고 DB에 반영한다. 0은 취소이다. 선택한 배치 ID와 최초 수집 시각의 행만 처리하며, 한 파일 안의 다른 배치를 함께 복구하지 않는다. 메뉴의 빈 항목 수는 CSV 기준이고, 실제 실행에서는 DB에 이미 가격이 있으면 건너뛴다. 이전 실행에서 덮어써져 현재 최종 CSV에 남아 있지 않은 배치는 목록에 나오지 않는다.

명령 인자도 계속 사용할 수 있다. `ALL 20261102`는 해당 날짜의 TV → REF → LDY를 순서대로 처리한다. 파일이 없는 상품군은 실패로 기록하고 나머지 상품군으로 진행한다.

```powershell
# 동일한 재조회·CSV 저장을 수행하되 DB에는 접속하지 않음
.\run_casas_bahia_price_recovery.bat REF 20261101 --csv-only
```

Python은 기존 BAT와 같이 PATH의 `python`을 사용한다. 이 복구 프로세스는 모드 **3 (Chrome + API)**, 배송지 `01010-010`을 명시적으로 선택한다. 일반 Python 수집 기본값 1과 기존 수집 BAT들의 모드 선택은 변경하지 않는다. 브라우저는 상품군마다 재사용하며 종료 시 정리한다. 기존 모드 3의 초기 접속/대기 및 연속 가격 통신 실패 제한을 재사용한다. 무한 재시도하지 않으며, 재실행하면 새 세션·캐시로 확인한다.

## 입력과 보존

- 입력: `seda/data/casas_bahia/{tv|ref|ldy}/{YYYYMMDD}/output/final_output.csv`와 같은 폴더의 `seda_final_targets.csv`.
- 최종 CSV의 판매가가 빈 행만 대상이다. 원래 대상의 상품 번호·URL SKU·판매자를 찾아 재확인한다. 판매자가 없으면 상품 번호로 후보를 찾고 **원래 SKU + 후보 판매자**를 다시 검증한다.
- `crawl_strdatetime`, `batch_id`, item, sku, 상품 URL, 순위, 리뷰 등은 원본 그대로 보존한다. 현재 시각으로 최종 출력 전체를 재생성하지 않는다.
- 갱신 허용 필드는 `original_sku_price`, `final_sku_price`, `savings`, `discount_type`뿐이며, 기존 값이 있는 필드는 덮어쓰지 않는다. 할인가 전용 컬럼을 추가하지 않는다. 기존 계약대로 `final_sku_price`는 검증된 최종/결제할인가를 사용한다. 원가가 최종가와 같으면 별도 원가를 만들지 않는다.
- 원본 CSV·기존 DB 적재 manifest는 수정하지 않는다. 복구 결과 CSV는 **가격 재조회 결과**이며, DB 갱신 성공 여부는 `.log`와 `results.json`에서 별도로 확인한다. 복구 CSV를 기존 append 로더에 넣으면 안 된다.
- 상세 처리·정리와 동일한 실행 잠금을 사용하며 불완전한 상세 게시 상태에서는 진행하지 않는다.

## DB 동작

상품군별 기존 테이블 설정을 사용한다. `account_name + product + item + sku + product_url + crawl_strdatetime + batch_id`가 일치하는 DB 행이 정확히 하나여야 한다. sku 컬럼은 원래 NULL이면 NULL로 매칭하고 나머지 식별 정보는 필수이다.

재조회 전에 DB를 확인한다. 가격이 이미 있으면 API 호출을 건너뛴다. 가격 검증 후 CSV를 먼저 저장하고 그 CSV의 가격 값으로 UPDATE한다. 갱신 직전 DB 행을 잠그고, 재조회 중 가격이 바뀌었거나 기존 필드가 비어 있지 않으면 갱신하지 않는다. 실제 갱신 행수가 1이 아니면 트랜잭션을 취소한다. 한 상품 단위로 커밋하므로 중단 후 다시 실행해도 이미 갱신된 행을 추가하거나 덮어쓰지 않는다.

## 사람이 보는 결과

- 로그: `seda/casas_bahia/log/casas_bahia_price_recovery_REF_실행시각.log` (TV·LDY도 동일 형식).
- `[사람 확인 필요]`에 원본 item·sku·상품명·원래 URL·대상 옵션 번호·후보 SKU·사유를 표시한다. 403/시간 초과, 정상 빈 응답, 식별자 누락·충돌을 진단과 함께 구분한다. 다른 SKU 가격만 있으면 원래 가격은 NULL로 유지한다.
- 가격 API 결과만으로 상품 페이지 삭제를 단정하지 않는다. 페이지 존재 여부는 `미확인`이며 추가 상세페이지 탐색을 수행하지 않는다. 옵션 번호가 다르다는 사실만으로 110V/220V 같은 이름을 추정하지 않는다.
- 성공 시 원가·최종 가격·할인 표시·할인 조건의 변경 전후와, 응답에서 검증한 원가/일반 판매가/최종 결제가격을 보여준다. 가격 확인과 DB 커밋 결과를 구분한다. 복구 조회 시각은 로그와 진단에만 남기며 원래 `crawl_strdatetime`을 바꾸지 않는다.
- 결과 폴더: 원래 수집 폴더 아래 `recovery/price/실행시각_식별자/`. `final_output_before.csv`, `final_output_recovered.csv`, `results.json`, `summary.json`을 생성한다. 상품마다 체크포인트를 저장한다.
- 종료 코드: 0 = 처리 완료(또는 대상 없음), 2 = 실행 실패 또는 사람이 확인할 미해결 건 존재. 원문 예외·인증 헤더·API 키는 로그에 남기지 않는다.

실제 조회·DB 갱신은 사용자가 BAT를 실행할 때 수행한다. 오프라인 테스트는 가격 API·DB를 대체하여 시간 보존, 옵션 불일치, 재실행, 동시 변경, 중복 행, 롤백 및 CSV 보존을 검증한다.
