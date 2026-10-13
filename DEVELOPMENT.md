# 개발 및 검증

설치·설정·센서 사용법은 [README.md](README.md), 배포 이력은 [CHANGELOG.md](CHANGELOG.md)에 정리합니다. 이 문서는 개발 환경, API 계약, 개인정보 처리, 검증 및 릴리스 절차를 다룹니다.

## 코드 구조

| 파일 | 역할 |
| --- | --- |
| `api.py` | 고정 호스트 HTTP 전송과 고객·청구·Power Planner 조회 |
| `auth.py`, `session_store.py` | 로그인, 세션 검증, 인증 정보 저장·복원 |
| `parser.py`, `models.py` | 응답 검증과 불변 데이터 모델 |
| `coordinator.py` | 선택 고객의 순차 갱신과 고객별 오류 관리 |
| `config_flow.py`, `__init__.py` | 설정, 재인증, 마이그레이션, 로드·해제 |
| `sensor.py`, `services.py` | 센서 상태와 응답 액션 |
| `diagnostics.py`, `repairs.py` | 제한된 진단 요약과 복구 안내 |

기존 도메인 `kepco_on`, 엔티티·기기 식별자, 응답 액션 이름을 유지합니다. 설정 마이그레이션과 세션 복구 코드는 이전 설치본을 지원하므로 단순히 오래됐다는 이유로 제거하지 않습니다.

## 개발 환경과 임시 파일

Python 3.14 이상을 사용합니다. 검증 의존성은 `requirements_test.txt`에 고정되어 있습니다. 가상 환경, 캐시, coverage, 캡처 결과와 임시 작업 파일은 `.local/`에 모읍니다.

| 경로 | 내용 |
| --- | --- |
| `.local/venv/` | Python 가상 환경 |
| `.local/pycache/` | Python bytecode 캐시 |
| `.local/pytest/`, `.local/mypy/`, `.local/ruff/` | 검증 도구 캐시 |
| `.local/.coverage`, `.local/htmlcov/` | coverage 데이터와 HTML 보고서 |
| `.local/npm/` | npm 다운로드 캐시 |
| `.local/login-schema.safe.json` | 로그인 스키마 메타데이터 |
| `.local/release-notes.md` | 변경 이력에서 추출한 배포 안내 |

`node_modules/`는 Node의 기본 모듈 탐색 경로를 유지합니다. 캡처용 브라우저 프로필은 운영체제 임시 디렉터리에 생성하고 도구 종료 시 삭제합니다. Home Assistant의 `.storage`는 설치 환경의 경로이므로 이동하지 않습니다. 루트 `.gitignore`에는 `.local/`과 함께 기존 경로 및 민감한 파일의 실수 커밋을 막는 규칙을 유지합니다.

Windows PowerShell:

```powershell
py -3.14 -m venv .local/venv
$env:PYTHONPYCACHEPREFIX = "$PWD/.local/pycache"
.local/venv/Scripts/python -m pip install -r requirements_test.txt
npm ci
.local/venv/Scripts/python -m ruff format --check .
.local/venv/Scripts/python -m ruff check .
.local/venv/Scripts/python -m mypy
.local/venv/Scripts/python -m pytest --cov=custom_components.kepco_on --cov-report=term-missing
npm run test:login-schema
npm audit --audit-level=moderate
git diff --check
```

Linux:

```bash
python3.14 -m venv .local/venv
export PYTHONPYCACHEPREFIX="$PWD/.local/pycache"
.local/venv/bin/python -m pip install -r requirements_test.txt
npm ci
.local/venv/bin/python -m ruff format --check .
.local/venv/bin/python -m ruff check .
.local/venv/bin/python -m mypy
.local/venv/bin/python -m pytest --cov=custom_components.kepco_on --cov-report=term-missing
npm run test:login-schema
npm audit --audit-level=moderate
git diff --check
```

테스트의 `hass` fixture는 실제 Home Assistant 상태 머신과 필요한 registry를 준비합니다. 별도의 플랫폼 전용 pytest 플러그인은 사용하지 않습니다. 인증과 전송 테스트는 로컬 HTTP 응답을, 파서·센서 테스트는 합성 fixture를 사용합니다. 상태 발행 테스트는 실제 센서 플랫폼과 Jinja 템플릿까지 확인합니다. branch coverage 기준은 95%이며 타입 선언 외의 동작 코드를 제외하지 않습니다.

## 한전ON 요청 계약

현재 구현은 한전ON 웹 화면의 내부 요청을 재현합니다. 외부 개발자용 공개 API 명세에 따른 연동은 아닙니다. 기준 호스트는 `https://online.kepco.co.kr`로 고정하며 TLS 검증을 유지합니다.

| 용도 | 메서드·경로 | 주요 요청 또는 응답 |
| --- | --- | --- |
| 로그인 준비 | GET `/MYM001D00` | 세션 쿠키 준비. HTTP 200 필요 |
| 개인 로그인 | POST `/cyb/me/login/indi/api` | `dma_loginData`의 `userId`, `pwdVal`, `autoFlag: "N"`. 응답은 `dma_loginData2`가 있으면 해당 객체 사용 |
| 세션 검증 | POST `/sessionCheck` | `refreshToken`, `userId`, `mbrsNm`. `result: true`만 반환해도 기존 인증 정보 유지. 복원 세션 설정 시, 그리고 비밀번호 미저장 상태에서 조회 중 세션 만료가 감지되면 재인증 요청 전에 한 번 실행 |
| 계정 유형 | POST `/isCorp` | `userClNm: "INDI"`만 지원 |
| 고객 목록 | POST `/my/indi/info/myPageCustNoList` | `dma_search`. 고객·세대 계약번호와 계약변경일 조회 |
| 아파트 청구 | POST `/my/charge/pay/aptBillDetail` | `custNo`, `housCntrNo`, `yymm`, `yyyymm`, `searchType: "DETAIL"` |
| 주택 청구 이력 | POST `/my/memo/mainChart` | `SI_CUST_NO`를 `custNo`로 사용. `months: "13"`, 계약변경일의 앞 6자리 |
| Power Planner | POST `/my/memo/powerPlanner` | 계약 유형별 고객·계약 식별자와 사용량 필드 조회 |

JSON 요청은 `Content-Type: application/json; charset=UTF-8`를 사용하고 인증된 요청에 `refreshToken` 헤더를 보냅니다. 화면에 대응하는 `submissionid`도 전달합니다. API별 요청 필드는 `api.py`에서 관리합니다. 통합에서 호출하지 않는 first-login, SSO, 보조 고객 목록 경로는 허용 목록에 두지 않습니다. 로그인 스키마 도구는 조사 목적으로 이들 인증 경로 일부를 관찰할 수 있습니다.

전송 타임아웃은 30초, 응답 크기 상한은 2 MiB입니다. 응답은 EOF까지 제한을 지키며 읽습니다. 네트워크 오류와 HTTP 500·502·503은 최대 3회 시도하고 재시도 사이에 1초·2초 대기합니다. HTTP 429는 `Retry-After`를 최대 30초 반영한 뒤 제한 오류로 전달합니다. HTTP 401·403과 로그인 HTML은 세션 만료로 처리합니다. 리다이렉트와 다른 호스트 응답은 거부합니다.

공개 화면 정의의 참고 경로는 `/ui/me/login/indi/MEM001D01.xml`, `/ui/my/indi/MYM001D00.xml`, `/ui/my/charge/MYM053D50.xml`입니다. 이 경로와 서버 동작은 변경될 수 있습니다. 저장소의 구현·회귀 검증과 실제 계정의 동작 확인을 구분합니다.

## 주택 청구 이력과 값 보존

`mainChart`의 `dlt_chart`에서 월별 `jojYmFilter`, `kwh`, `afterMny`, `gigan`을 읽습니다. 최신 월을 선택하고, 같은 응답의 13개월 이력에 있는 전월·전년동월 사용량을 비교값으로 사용합니다. 이력에 해당 월이 없으면 값은 `unknown`입니다. 과거월 조회에는 현재 검침기간의 Power Planner 값을 붙이지 않습니다.

## Power Planner 계약별 처리

| 계약 | 요청 | 값 처리 |
| --- | --- | --- |
| 정확히 `아파트(종합계약)` | `custNo=customer_number`, `housCntrNo=house_contract_number`, `chgYmd=""` | 사용자 동작 보고에 기반하여 `F_AP_QT`, `PREDICT_TOT` 각각 한 번만 1000으로 나눔 |
| 단일계약·종합계약/나·주택용 | `custNo=house_contract_number`, `chgYmd=change_ymd` | 현재 사용량은 기존 단위로 처리. 예측값은 단위 미확인으로 표시 보류 |

종합계약의 변환은 사용자 보고에 따른 호환 처리입니다. 한전 전체 계약의 공통 단위로 확인된 것은 아닙니다. 공개 화면은 `PREDICT_TOT`를 예상 요금으로 설명한 이력이 있으므로 수치 크기로 에너지 단위를 추론하지 않습니다. `conversion_basis`와 `value_divisor` 속성에 변환 근거를 표시합니다.

`RETURN_CD`가 없거나 `00`일 때만 값 파싱을 진행합니다. 실제 0은 유지하고 누락·잘못된 숫자·음수·NaN·Infinity는 구분합니다. 한 필드의 오류가 다른 정상 필드나 청구 데이터를 지우지 않습니다.

## 개인정보 처리와 조사 도구

Config Entry에는 아이디, 선택 고객·계약번호, 계정 해시, 옵션과 선택적인 비밀번호를 저장합니다. 원본 식별자는 향후 조회 요청에 필요하며 엔티티 식별자로 쓰지 않습니다. Private Store에는 refresh token, 최소 세션 식별 정보, 선택적인 토큰과 검증된 `JSESSIONID`·`kepcoSSO` 쿠키를 저장합니다. 초기 로그인 handoff는 Store로 소비한 뒤 Config Entry에서 제거합니다.

비밀번호 저장을 꺼도 세션 복구 자료는 저장됩니다. Config Entry, Store와 Home Assistant 백업은 이 통합이 자체 암호화하지 않습니다. 진단은 허용된 요약과 응답 액션에 필요한 고객 해시만 반환하고 원본 설정·세션·청구 내용을 반환하지 않습니다.

로그인 스키마 변경을 조사할 때만 다음 명령을 실행합니다.

```bash
npm run capture:login-schema
```

도구는 임시 Chrome 프로필로 정상 로그인을 기다리고 `.local/login-schema.safe.json`에 경로·키·타입·길이 등 메타데이터만 기록합니다. 원본 요청·응답, 쿠키 값, HAR, trace, screenshot은 저장하지 않습니다. 사용자명·비밀번호와 일치하는 값이나 의심스러운 비밀값을 검사한 뒤 기록합니다. 같은 출력 파일이 이미 있으면 덮어쓰지 않습니다. CAPTCHA·MFA·OACX·인증서 로그인을 자동화하지 않습니다.

실제 캡처를 테스트 fixture로 자동 변환하지 않습니다. 새 fixture는 필요한 응답 구조만 남긴 합성값으로 작성하고 `tests/test_ci_metadata.py`의 개인정보 검사를 통과해야 합니다. 이슈에는 버전, 오류 범주, 해당 API 용도, 발생 시각과 검토한 안전 메타데이터만 공유합니다.

## 릴리스와 실제 환경 확인

GitHub `Tests`는 형식·lint·타입·Python coverage·Node 테스트·의존성 검사를 실행합니다. `Validate`는 HACS와 Hassfest를 실행합니다. `Release`는 `main`의 같은 커밋에서 두 검증이 성공한 경우에만 배포하며, `main`이 진행됐거나 같은 버전이 이미 배포됐다면 생성을 건너뜁니다.

배포 버전은 `const.py`, `manifest.json`, `pyproject.toml`에서 일치해야 합니다. 해당 버전의 안내는 `CHANGELOG.md`에 작성합니다. `tools/release_notes.py`가 해당 절만 `.local/release-notes.md`로 추출하므로 별도의 배포 안내 원본은 두지 않습니다.

실제 Home Assistant에서 업데이트를 확인할 때는 설정 검사와 Core 재시작, 로그인·고객 선택, 최신·과거월 조회, 센서·두 응답 액션, 세션 복구·재인증, 선택 고객 변경과 삭제를 확인합니다. 운영 환경에 설치하기 전 백업을 확보합니다.

기존 문서에는 2026-09-01의 `v0.1.1` HAOS 2026.8.3 검증 기록이 있습니다. 이 기록은 당시 버전의 결과이며 현재 브랜치의 실계정 검증 근거로 사용하지 않습니다. 저장소 테스트 통과는 서버의 데이터 제공 여부, 장시간 세션 유지 또는 모든 계정의 동작을 증명하지 않습니다.
