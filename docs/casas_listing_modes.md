# Casas Bahia listing modes

최종 선택 기본값: **3 (UC + API)**. Main 및 BSR listing에만 적용하며 Detail의 `SEDA_FETCH_MODE=graphql`과 기존 상세·보강·저장·업로드·DB 경로는 바꾸지 않는다.

| 번호 | 이름 | 실행 경로 | 실패 시 |
| --- | --- | --- | --- |
| 1 | REST API | Python Partner 검색 GET + 기존 가격 API | 검색 최대 3회 후 페이지 실패. Chrome/다른 모드로 전환하지 않음 |
| 2 | hybrid | 기존 REST 최대 3회 → Chrome 실제 페이지 + SSR/가격 응답 | 기존 hybrid의 제한 재접속·실패 처리 유지 |
| 3 | UC + API | 최초 Chrome 실제 페이지 검증 → 같은 Document에서 JS 검색 GET + 가격 POST | 페이지별 검색 최대 3회. 다른 모드/SSR 결과로 자동 대체하지 않음 |

## 실행과 최종 선택

```bat
rem 최종 기본값 3으로 TV -> REF -> LDY 전체 실행
run_casas_bahia_tv_ref_ldy_full.bat

rem 이번 실행만 모드 지정
run_casas_bahia_tv_ref_ldy_full.bat 1
run_casas_bahia_tv_ref_ldy_full.bat 2
run_casas_bahia_tv_ref_ldy_full.bat 3
```

배치 파일 상단 `set "SEDA_CASAS_BAHIA_LISTING_MODE=3"` 한 줄이 최종 기본 선택이다. 첫 번째 인자를 주면 해당 실행에만 덮어쓴다. 대화형 입력을 요구하지 않으므로 예약 실행에도 사용할 수 있다. 1/2/3 이외의 값은 수집 전에 거부한다.

Python 모듈을 직접 실행할 때는 `SEDA_CASAS_BAHIA_LISTING_MODE` 환경변수로 선택하며, 없으면 기본 3이다. 이 모드 변수는 listing 전용이다. 전역 `SEDA_FETCH_MODE`를 이 변수로 대체하지 않는다.

각 Casas listing 코드 수정 시 세 모드를 유지하고, 최종 선택 숫자를 명시적으로 확인·기록해야 한다. 현재 선택은 3이며 변경 요청 없이 다른 모드로 바꾸지 않는다.

## 모드 3의 정확한 범위

- 실행 조건은 성공한 로컬 TV 1→2→3→2 실험에 맞춘다. 시작 로그의 `execution_profile=successful_local_probe`로 확인한다. 세 번째 모드의 조건 정렬이지 새로운 네 번째 모드는 아니다.
- 매 listing 프로세스에 전용 새 Chrome 프로필을 사용한다. 기존 Chrome 계정 로그인·쿠키·사용자 프로필에 의존하지 않는다. 성공한 로컬 실험도 새 프로필이었다.
- Chrome은 창이 있는 방식이며 `--disable-gpu`, `--no-sandbox`, `--no-first-run`, `--no-default-browser-check`와 실험의 실행 인자를 사용한다. 모드 3에는 기존 headless 설정을 적용하지 않는다.
- 설치된 Chrome과 major가 맞는 드라이버를 복사해 전용 경로에서 사용한다. 일치하는 기존 드라이버가 없으면 해당 major를 전용 캐시에 준비한다. 기존 UC 전역 설정이나 다른 브라우저는 변경하지 않는다. 종료 시 자신이 만든 임시 프로필·드라이버 경로만 정리하며, 안전하게 종료/정리하지 못하면 해당 경로를 보존한다.
- 최초 접속 제한 45초, 접속 후 Document/가격 검증 대기 30초, 각 검색/가격 API 제한 25초, 스크립트 제한 30초로 고정한다. 모드 1·2와 detail의 설정에는 영향을 주지 않는다.
- 검색 요청의 상품 수 20, variant `q2`, region `126000`, 빈 user ID를 사용한다. 세션 ID는 해당 프로세스의 API 세션마다 새로 생성해 재사용하며 출력하지 않는다. 가격 요청 region도 `126000`이다. 요청 사본에만 적용하며 `.env`나 전역 환경변수를 수정하지 않는다.
- 이외의 검색 URL/검색어, 가격 API 우편번호·UTM 등의 기존 설정은 유지한다. 실험도 기존 프로세스 환경 자체를 비우지는 않았으므로 당시 값과 GCP의 모든 설정값까지 동일하다고 간주하지 않는다. 키·환경 파일을 열어 비교하지 않는다.
- 처음 API 검색은 초기 검증 완료 후 최소 5초, 이후 검색은 직전 검색 완료 후 최소 5초 간격이다. 실험의 수동 명령 대기시간 전체를 재현하는 것은 아니며 코드에 명시된 최소 간격을 자동 적용한다. TV/REF/LDY별 검색어와 main/BSR 정렬은 기존 수집 대상을 유지한다.
- 최초 실제 페이지는 유효한 브라우저 세션을 확인하기 위한 준비 단계다. 그 SSR 상품을 모드 3의 최종 수집 결과로 사용하지 않는다.
- 최초 접속 이후에는 같은 Chrome/Document에서 검색·가격 API만 호출한다. 페이지마다 `driver.get()`을 반복하지 않는다.
- Chrome 재사용 단위는 **각 listing 프로세스**다. Main, BSR 및 카테고리는 별도 프로세스이므로 TV/REF/LDY 전체 배치에서 Chrome이 총 한 번만 열리는 것은 아니다.
- 직접 Python REST, ZenRows 또는 모드 2로 자동 전환하지 않는다. 최초 브라우저 준비 실패도 해당 프로세스에서 명시적 실패로 유지한다.
- 검색 기본 최대 3회(최초 1 + 재시도 2), 재시도 기본 간격 3초/6초. 가격 요청은 검색 성공 시 별도 POST다. CORS OPTIONS는 브라우저가 추가할 수 있으므로 검색 상한이 전체 HTTP 요청 수와 같지는 않다.
- `SEDA_CASAS_BAHIA_SEARCH_RETRIES`는 0~2로 낮출 수 있지만 2보다 높아도 검색 3회 상한은 유지된다. 배치는 2로 지정한다.
- 각 성공 로그에 `mode=3`, 페이지, 검색·가격·총 소요시간을 기록한다. 실패는 단계, 안전한 오류 코드, HTTP 상태를 기록한다.
- 기존 브라우저 쿠키를 추출하거나 기존 사용자의 Chrome에 연결하지 않는다. 브라우저 fetch는 검증된 `credentials=same-origin` 조건을 사용한다.

## 정합성·출력 보호

- API는 성공 실험과 같이 endpoint·검색 page에 해당하는 요청 ID의 GET/POST 200과 브라우저가 읽은 JSON 응답을 확인한다. `responseReceivedExtraInfo`의 200도 근거로 인정한다. OPTIONS 200만으로 검색/가격 성공으로 판정하지 않는다.
- 각 API 호출 전후 동일 frame/loader와 호출 중 Document 이동 없음을 확인한다. 실험에 없던 API `loadingFinished` 필수 조건, 전체 query/body 일치, 응답 정확히 1개 조건은 추가 성공 판정으로 사용하지 않는다. 이 변경은 API 판정에만 해당하며 **최초 SSR Document 완료·상품·가격 검증은 유지**한다.
- 실험과 동일하게 브라우저 캐시 비활성화·Service Worker 우회 및 fetch `cache=no-store`, `credentials=same-origin`을 유지한다.
- REST `sku`를 복사본의 `idSku` 별칭으로 대응시키고 URL·상품·SKU·판매자·가격을 정확한 ID로 검증한다. 충돌이나 누락은 실패다.
- 응답이 page/sort를 생략하면 실제 요청 근거만 있음을 trace에 기록한다. 파서용 요청 page/sort를 넣은 것을 서버가 응답한 값으로 표현하지 않는다. 명시적 page/sort 불일치는 거부한다.
- 다른 페이지가 이전 페이지 전체 SKU 집합을 그대로 반복하면 실패 처리한다.
- 출력 CSV 계약과 기존 관련 상품 필터를 유지한다. 일부 페이지 실패 시 `.partial.csv`로 저장하고 완료 결과로 발행하지 않으며 downstream을 중단한다.
- raw 옆 `.mode.json`에 선택 모드와 source URL을 기록한다. 모드가 다르거나 메타데이터가 없는 raw는 재사용하지 않는다.
- manifest의 `casas_listing_mode`, `casas_listing_mode_label`, `fetch_mode`로 선택과 실제 경로를 확인할 수 있다.

## 검증 범위와 주의사항

실행 조건 정렬과 초기화 진단 변경은 오프라인 회귀 테스트로 확인한다. Chrome 준비·접속·검증 대기 소요시간과 완료/실패/취소 이벤트를 기록하고, 실패 후 문서 준비 상태만 별도로 조회한다. 키·쿠키·응답 본문을 진단 로그로 출력하지 않는다. 진단 실패가 원래 수집 판정을 바꾸지 않도록 한다.

실행 조건을 맞췄다는 사실은 GCP 실수집 성공을 의미하지 않는다. GCP 네트워크·Chrome 빌드·머신 환경까지 동일하다고 검증한 것은 아니다. 기존 최대 3회 재시도, 전체 배치 자동 진행과 TV 이외 제품군/BSR 확장은 수동 TV 실험과 구분한다.

전용 진단에서 TV 1→2→3→2 페이지의 브라우저 내부 검색·가격 호출이 모두 성공했다. 각 API 응답 합계는 약 3.81~5.88초였고 최초 실제 페이지 검증은 별도 19.061초였다. 이 시간에는 Chrome 시작·파싱·명령 대기·요청 간격이 포함되지 않는다.

**SSR 28개와 REST 27개의 상품 구성·순서가 달랐다.** 공통 17개는 가격·판매자가 같았지만 전체 결과가 동일하다고 검증된 것은 아니다. 사용자 요청으로 최종 모드 3을 선택하며, 이 차이가 해소됐다고 간주하지 않는다. 전체 페이지·BSR·REF/LDY·장시간 운영의 안정성은 개별 실측 결과와 구분한다.

모드 변경 후 재수집은 이 배치의 `--all` 경로를 사용한다. 별도 `--resume`의 기존 완료 CSV 판단은 모드 변경까지 추적하지 않으므로, 이전 모드 결과를 새 모드로 재수집하는 용도로 사용하지 않는다.

이 배치는 dry-run이 아니며 이후 업로드·DB 적재까지 진행한다. 진단 시에는 별도 출력 폴더의 listing 단계만 실행하고 전체 배치를 호출하지 않는다.

구현: `listing_modes.py`, `browser_api.py`, `browser_probe_session.py`, 기존 `listing_hybrid.py`/`browser_listing.py`, 공통 `step01_main_list.py`와 `transport.py`.
