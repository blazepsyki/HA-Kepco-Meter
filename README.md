<!-- project-branding:start -->
<p align="center">
  <img src="https://raw.githubusercontent.com/1bobby-git/brands/master/custom_integrations/kepco_on/logo%402x.png" alt="한전ON 로고" width="420">
</p>
<p align="center">
  <a href="https://github.com/1bobby-git/HA-Kepco-Meter/stargazers"><img src="https://img.shields.io/github/stars/1bobby-git/HA-Kepco-Meter?style=flat-square&logo=github&label=Stars" alt="GitHub Stars"></a>
  <a href="https://github.com/1bobby-git/HA-Kepco-Meter/releases"><img src="https://img.shields.io/github/v/release/1bobby-git/HA-Kepco-Meter?style=flat-square&label=Release" alt="Latest Release"></a>
  <a href="https://github.com/1bobby-git/HA-Kepco-Meter/blob/main/custom_components/kepco_on/manifest.json"><img src="https://img.shields.io/badge/Architecture-independent-0ea5e9?style=flat-square" alt="Architecture independent"></a>
  <a href="https://github.com/1bobby-git/HA-Kepco-Meter/blob/main/LICENSE"><img src="https://img.shields.io/github/license/1bobby-git/HA-Kepco-Meter?style=flat-square&label=License" alt="License"></a>
  <a href="https://github.com/1bobby-git/HA-Kepco-Meter/commits/main"><img src="https://img.shields.io/github/last-commit/1bobby-git/HA-Kepco-Meter?style=flat-square&label=Updated" alt="Last Commit"></a>
</p>
<!-- project-branding:end -->

# 한전ON Home Assistant 커스텀 통합

이 포크의 `v0.4.1`은 주택용 상세 조회 수정사항을 HA에서 확인하기 위한 테스트 릴리스입니다. 설치와 확인 방법은 [포크 테스트 설치 문서](docs/FORK_TESTING.md)를 참고해 주세요. 원본 저장소는 `1bobby-git/HA-Kepco-Meter`입니다.

한전ON 개인(`INDI`) 계정의 아파트 단일·종합계약 및 주택용 직접계약 전기요금 조회를 Home Assistant 센서와 응답 액션으로 가져오는 비공식 커스텀 통합입니다. 실시간 스마트미터가 아니라 한전ON 청구/검침 페이지에서 확인되는 월별 요금 데이터 기반입니다.

Repository: https://github.com/1bobby-git/HA-Kepco-Meter

## 현재 범위

- 지원: 한전ON 개인 계정(`INDI`), 아파트 세대 단일·종합계약 및 주택용 직접계약 요금.
- 미지원: 법인 계정, 전기공사업체 계정, 인증서 로그인, OACX 간편인증 자동화, CAPTCHA/MFA 우회, CO2 실측값. Power Planner 값은 실시간 스마트미터 값이 아닙니다.
- 통신: `https://online.kepco.co.kr`의 고정된 한전ON 경로만 사용하며 TLS 검증을 끄지 않습니다.
- 버전: `v0.4.1`. 선택 고객마다 5개 논리 기기와 34개 센서 엔티티를 생성합니다.

## 설치

### HACS 커스텀 저장소

1. HACS > Integrations > 우측 메뉴 > Custom repositories.
2. 저장소 URL에 `https://github.com/1bobby-git/HA-Kepco-Meter` 입력.
3. Category는 Integration 선택.
4. `한전ON (KEPCO ON)` 설치 후 Home Assistant를 재시작.

### 수동 설치

`custom_components/kepco_on` 폴더를 Home Assistant 설정 디렉터리의 `/config/custom_components/kepco_on`에 복사한 뒤 Home Assistant를 재시작합니다. `/config`는 File editor 애드온, Samba share, SSH 애드온, 또는 호스트에 마운트된 설정 디렉터리로 접근할 수 있습니다. 재시작 후 통합 목록이 오래된 상태로 보이면 브라우저 캐시를 지우고 다시 열어 보세요.

## 설정

설정 > 기기 및 서비스 > 통합 추가 > KEPCO ON을 선택합니다.

1. 한전ON 아이디와 비밀번호를 입력합니다.
2. 자동 재인증이 필요하면 `비밀번호 저장`을 켭니다.
3. 조회할 고객을 선택합니다. 아파트/오피스텔은 아파트명·동·호를, 주택용 직접계약은 계약 유형과 마스킹된 고객번호를 표시합니다.

`비밀번호 저장`을 끄면 비밀번호는 저장하지 않습니다. 재시작/세션 복구를 위해 refresh token, 최소 세션 식별 정보와 한전ON 인증에 사용되는 `JSESSIONID`/`kepcoSSO` 쿠키만 private Home Assistant Store에 저장합니다. 쿠키는 한전 도메인·경로·만료 검증을 통과한 경우에만 보존하며, 요청 중 갱신된 값도 최신 스냅샷으로 다시 저장합니다. 저장된 서버 세션 자체가 만료되면 재인증이 필요할 수 있으며, `비밀번호 저장`을 켠 경우에는 자동 재로그인을 시도합니다.

이 통합은 Config Entry, Store, 백업을 자체 암호화하지 않습니다. 비밀번호 저장 여부와 관계없이 Home Assistant 호스트, `.storage`, 백업 파일을 비밀 저장소처럼 보호하세요.

## 생성되는 기기와 센서

선택한 고객마다 다음 5개 논리 기기와 총 34개 센서 엔티티를 생성합니다. 모든 센서는 기본 활성 상태입니다. 한전ON에서 값이 제공되지 않으면 해당 엔티티는 `unknown`으로 표시될 수 있습니다.

통합 허브 제목은 `1001동 101호`(가상 예시)처럼 선택 세대 위치로 표시하고, 하위 기기 이름은 `월별 사용량`, `검침/전기사용량`, `전기요금`, `이웃 전기사용량 비교`, `온실가스 배출량`으로만 표시합니다. 동·호의 앞자리 0은 표시 단계에서 제거합니다. 사용자가 설정 과정에서 별도 표시 이름을 입력한 경우에는 해당 이름을 유지합니다.

### 월별 사용량 · 6개

현재 청구월을 기준으로 최근 3개월과 전년 같은 기간 3개월을 함께 제공합니다. 엔티티의 고유 ID는 상대 월 위치를 사용하므로 청구월이 바뀌어도 엔티티가 새로 누적되지 않고 표시 이름만 갱신됩니다.

| 표시 예시 | 값 예시 |
| --- | ---: |
| 2025년 6월 | 399 kWh |
| 2026년 6월 | 371 kWh |
| 2025년 7월 | 459 kWh |
| 2026년 7월 | 406 kWh |
| 2025년 8월 | 612 kWh |
| 2026년 8월 | 573 kWh |

### 검침/전기사용량 · 12개

| 센서 | 값 예시 |
| --- | ---: |
| 전기 사용 기간 시작일 | 2026-07-01 |
| 전기 사용 기간 종료일 | 2026-07-31 |
| 검침일 | 01 |
| 당월지침 | 23,139 kWh |
| 전월지침 | 22,566 kWh |
| 당월 사용량 | 573 kWh |
| 당월 세대 사용량 | 573 kWh |
| 당월 공용 사용량 | 0 kWh |
| 전월 사용량 | 406 kWh |
| 전년동월 사용량 | 612 kWh |
| 현재 검침기간 누적 사용량 | 246.80 kWh (종합계약 호환 예시) |
| 한전 예측 사용량 | 987.65 kWh (종합계약 호환 예시) |

`검침일`, `전기 사용 기간 시작일`, `전기 사용 기간 종료일`은 Home Assistant 기기 페이지의 `센서 정보` 영역에 표시하고, 나머지 9개 엔티티는 `센서` 영역에 표시합니다.

한전ON이 `당월 세대 사용량`과 `당월 공용 사용량`을 모두 비워서 반환하면 전체 사용량을 세대 사용량으로, 공용 사용량을 `0 kWh`로 보정합니다. 한쪽 또는 양쪽의 실제 값이 있으면 한전ON 원본 값을 우선 사용하고 필요한 한쪽만 전체 사용량과의 차이로 계산합니다.

아파트(종합계약)은 사용자 재현 보고에 따른 호환 프로필로 Power Planner의 `F_AP_QT`와 `PREDICT_TOT`를 각각 1000으로 나눠 기존 두 센서에 전달합니다. 이 단위 해석은 한전 공통 규격으로 검증되지 않았으며 `conversion_basis=user_reported_combined_contract`로 명시합니다. 다른 계약의 예측값 보류는 유지합니다. 값이 없는 필드만 `unknown`이고 실제 0은 유지됩니다.

### 전기요금 · 10개

| 센서 | 값 예시 |
| --- | ---: |
| 전기요금 계 | 85,484 KRW |
| 전기요금 상세 기본요금 | 6,060 KRW |
| 전기요금 상세 전력량요금 | 87,402 KRW |
| 전기요금 상세 기후환경요금 | 5,157 KRW |
| 전기요금 상세 연료비조정요금 | 2,865 KRW |
| 전기요금 상세 출산가구할인요금 | -16,000 KRW |
| 부가가치세 | 8,548 KRW |
| 전력기금 | 2,300 KRW |
| 원단위절사금액 | 2 KRW |
| 청구금액 | 96,330 KRW |

Home Assistant 기기 화면은 기기 이름과 같은 일반 공백 접두어를 엔티티 이름에서 자동 생략할 수 있습니다. `v0.2.2`부터 전기요금 엔티티는 위 표의 전체 이름이 그대로 표시되도록 처리합니다.

금액 센서는 Home Assistant의 monetary device class 규칙에 맞춰 ISO 4217 통화 코드 `KRW`를 사용합니다. 위 기후환경요금 예시는 저장소의 한전ON 응답 샘플에 포함된 실제 값 `5,157원`을 기준으로 합니다.

### 이웃 전기사용량 비교 · 3개

| 센서 | 값 예시 |
| --- | ---: |
| 고객님 | 573 kWh |
| 해당동 | 363 kWh |
| 아파트 전체 | 284 kWh |

### 온실가스 배출량 · 3개

| 센서 | 값 예시 |
| --- | ---: |
| 당월 배출량 | 263 kg CO₂ |
| 전월 배출량 | 186 kg CO₂ |
| 전년동월 배출량 | 281 kg CO₂ |

온실가스 값은 한전ON 실측값이 아니라 각 전기사용량에 설정된 환산계수를 곱한 로컬 추정값입니다. 기본 환산계수는 `0.459 kg CO₂/kWh`입니다.

엔티티 ID는 Home Assistant가 설치 환경의 이름 충돌 상태에 따라 정합니다. 고객별 고유 ID에는 원본 고객번호나 계약번호 대신 계정·고객 정보로 만든 안정 해시를 사용합니다.

## Energy Dashboard

Energy Dashboard에는 `당월지침` 센서를 전력 사용량 소스로 추가합니다. 이 센서는 누적 검침값이며 `total_increasing` 상태 클래스를 사용합니다. 월별 사용량과 비교 사용량 센서는 한 달 단위 값이므로 누적 소스로 사용하지 않습니다.

## 옵션, 업그레이드, 재인증, 재구성

옵션에서 조정할 수 있는 값은 조회 주기, 온실가스 환산계수, 사용량 이력 응답 길이입니다. 조회 주기는 1·3·6·12·24시간 중 선택하며 기본값은 6시간입니다. 환산계수는 0보다 크고 10 이하이며 기본값은 `0.459`입니다. 이력 응답 길이는 1~24개월이며 기본값은 12개월입니다.

`v0.1.x`에서 업그레이드하면 기존 주요 엔티티의 고유 ID를 유지하면서 5개 기기로 재배치합니다. `v0.2.0`에서 `v0.2.1`로 업그레이드하면 기존 엔티티 ID와 unique ID를 유지하면서 허브와 하위 기기 이름만 정리하고, 검침일·시작일·종료일을 `센서 정보` 영역으로 이동합니다. 과거 통합 기본값 때문에 비활성화된 상세 엔티티는 자동으로 활성화하고, 사용자가 직접 비활성화한 엔티티는 그대로 둡니다. 더 이상 사용하지 않는 `청구월` 단독 엔티티와 상세/CO₂ 생성 토글 옵션은 마이그레이션 과정에서 정리됩니다. `v0.2.1`에서 `v0.2.2`로 업그레이드하면 전기요금 기기의 10개 엔티티가 표의 전체 이름으로 표시됩니다. 기존 엔티티 ID와 unique ID는 유지됩니다. `v0.3.1`부터 주택용 직접계약과 Power Planner 센서 2개를 지원하며 기존 아파트/오피스텔 엔티티 고유 ID는 유지됩니다. `v0.3.3`에서는 한전ON 로그인 응답의 선택 토큰 누락을 허용하고, 로그인·계정 유형·고객 목록 단계의 응답 오류를 구분해 표시합니다. `v0.3.10`에서는 한전ON 인증 쿠키를 제한적으로 영속화하고 최소 성공 세션 검증 응답을 정상 처리합니다. `v0.3.11`에서는 `v0.3.9`에서 넘어온 쿠키 없는 레거시 세션을 자동 재로그인하거나 Home Assistant 재인증 흐름으로 전환해 일반 설정 오류를 방지합니다. 업데이트 후 Home Assistant Core를 완전히 재시작하세요.

재인증은 기존 계정의 비밀번호만 다시 받습니다. 다른 한전ON 계정으로 로그인되면 항목 업데이트가 거절됩니다.

재구성은 선택 고객을 갱신합니다. 통합이 로드된 상태면 한전ON에서 고객 목록을 새로 받아오고, 실패하면 저장된 고객 목록을 기준으로 선택 화면을 엽니다. 선택에서 빠진 고객의 엔티티와 이 통합만 소유한 5개 논리 기기는 정리됩니다.

## 응답 액션

`kepco_on.get_monthly_bill`은 한 고객의 청구 상세를 반환합니다. 아파트/오피스텔은 지정 월을 조회하고, 주택용 직접계약은 한전ON `mainChart`의 최신 청구 이력을 사용합니다.

```yaml
action: kepco_on.get_monthly_bill
data:
  config_entry_id: !secret kepco_on_config_entry_id
  customer_id: !secret kepco_on_customer_id
  month: "202608"
response_variable: kepco_bill
```

`kepco_on.get_usage_history`는 선택 고객의 월별 사용량 이력을 반환합니다. `month`를 비우면 현재 코디네이터가 가진 최신 청구 데이터를 우선 사용합니다. 각 이력 항목은 `month`·`usage_kwh`를 포함하고, 한전ON 응답에 해당 월 청구액이 있으면(주택용 직접계약 `mainChart`) `amount_krw`도 함께 반환합니다. 같은 값은 `월별 사용량` 센서의 `amount_krw` 속성으로도 노출됩니다.

```yaml
action: kepco_on.get_usage_history
data:
  config_entry_id: !secret kepco_on_config_entry_id
  customer_id: !secret kepco_on_customer_id
response_variable: kepco_history
```

`config_entry_id`는 개발자 도구 > 액션 또는 자동화 시각 편집기에서 `config_entry_id` 선택기를 열고 KEPCO ON 항목을 선택하면 Home Assistant가 채웁니다. 수동 YAML에 넣어야 할 때는 선택기로 항목을 고른 뒤 YAML 보기로 전환해 생성된 ID를 복사하세요.

`customer_id`는 원본 한전 고객번호가 아니라 통합이 생성한 64자 안정 해시 키입니다. 가장 신뢰할 수 있는 확인 경로는 Settings > Devices & Services > KEPCO ON 항목 > 점 세 개 메뉴 > Download diagnostics에서 진단 파일을 내려받은 뒤 `selected_customer_ids` 값을 복사하는 것입니다. 이 값은 응답 액션에 필요한 안전 식별자이며 원본 한전 고객번호나 계약번호가 아닙니다. 그래도 공개 이슈나 자동화 예제에 올릴 필요는 없습니다. 진단 다운로드가 어려운 환경에서는 Settings > Devices & Services > Entities에서 KEPCO ON 센서 엔티티 설정을 열고 entity registry의 unique ID 앞 64자를 확인하는 방법을 fallback으로 사용할 수 있습니다.

## 자동화 예시

```yaml
alias: 한전ON 월 사용량 알림
triggers:
  - trigger: time
    at: "09:00:00"
conditions:
  - condition: template
    value_template: "{{ now().day == 1 }}"
actions:
  - action: kepco_on.get_usage_history
    data:
      config_entry_id: !secret kepco_on_config_entry_id
      customer_id: !secret kepco_on_customer_id
    response_variable: usage_history
  - action: notify.mobile_app_phone
    data:
      message: "한전ON 최근 이력: {{ usage_history.history | count }}개월"
```

## 문제 해결

- 로그인 실패: 한전ON 웹에서 같은 계정으로 직접 로그인되는지 확인합니다. CAPTCHA, MFA, OACX 등 조건부 챌린지가 나오면 이 통합은 우회하지 않습니다.
- 세션 만료/복원 실패: `v0.3.11`은 refresh token과 허용된 `JSESSIONID`/`kepcoSSO` 인증 쿠키를 함께 복원합니다. `v0.3.9`에서 넘어온 쿠키 없는 세션은 비밀번호 저장 시 자동 재로그인하고, 비밀번호가 없으면 기존 항목을 유지한 채 재인증을 요청합니다.
- 고객 없음: 이 통합은 개인 아파트 세대 계약만 지원합니다.
- 일부 세대만 unavailable: 한 세대의 청구 조회 실패는 다른 세대 센서와 분리됩니다.
- 월 조회 실패: 응답 액션의 `month`는 `YYYYMM`이고 현재월보다 미래이거나 최근 24개월 범위 밖이면 거절됩니다.
- 프로토콜 변경 수리 이슈: 한전ON 응답 구조가 바뀌면 원본 응답 없이 안전한 오류 분류만 수리 이슈로 표시됩니다.
- `login bootstrap content type changed`가 보이는 경우: 기존 `v0.1.0` 또는 오래된 설치본에서 로그인 bootstrap 응답을 지나치게 엄격하게 검사했을 수 있습니다. HACS에서 `v0.1.1` 이상으로 업데이트한 뒤 Home Assistant를 완전히 재시작하고, 설치된 통합 버전이 `0.1.1` 이상인지 확인한 다음 다시 설정하세요.

설치된 매니페스트 버전 확인:

```bash
cat /config/custom_components/kepco_on/manifest.json
```

과거 로그인 bootstrap 오류 검색:

```bash
grep -F "login bootstrap content type changed" /config/home-assistant.log* 2>/dev/null
```

문제가 계속되면 공개 이슈에는 비밀번호, 쿠키, 토큰, 원본 고객번호, 계약번호, HAR, raw capture를 올리지 마세요. 공유 가능한 자료는 검토 후 민감값을 지운 로그와 `login-schema.safe.json`뿐입니다.

## 안전한 로그인 스키마 캡처

개발자가 한전ON 로그인 스키마 변경을 확인해야 할 때만 사용합니다. 이 도구는 원본 요청/응답 본문, 헤더, 쿠키, HAR, trace, screenshot을 저장하지 않고 안전 메타데이터만 `login-schema.safe.json`에 씁니다. 해당 파일은 Git에서 무시됩니다.

```powershell
npm run capture:login-schema
```

도구는 임시 Chrome 프로필을 만들고 사용자가 직접 정상 로그인하도록 기다립니다. CAPTCHA, MFA, OACX를 자동화하거나 우회하지 않습니다. 종료 시 OS 임시 폴더 아래의 도구 전용 프로필만 삭제합니다.

## 삭제

Home Assistant에서 통합 항목을 삭제하면 이 통합이 소유한 항목이 제거됩니다. Home Assistant 백업, `.storage`, 로그, 외부 비밀 파일에 남은 비밀번호나 세션 정보는 별도 보관 정책에 따라 직접 관리해야 합니다.

## 릴리스 체크

릴리스는 `main`의 Tests가 성공하고 같은 커밋의 HACS/Hassfest 검증까지 성공한 경우에만 자동 생성됩니다. 매니페스트 버전, Git 태그 `v{version}`, GitHub 릴리스 제목과 `kepco_on-v{version}.zip` 자산을 일치시킵니다.

## 라이선스

[MIT License](LICENSE)를 따릅니다.

## English Summary

This is an unofficial Home Assistant custom integration for KEPCO ON individual apartment billing. It exposes monthly billing sensors and response actions, not real-time meter telemetry. Store credentials only if you accept Home Assistant storage and backup risk.

## 통합 업데이트 0.3.4

- TCP/HTTP 분할 수신을 응답 전체로 오인하는 단일 read(n) 처리 수정
- 기존 2 MiB 제한·요청 타임아웃·호스트/경로 제한·재시도·인증 실패 구분은 유지
- 로그인 준비 페이지와 JSON 응답 모두 EOF까지 크기 제한을 지켜 수신
- 이 패치는 이전 로그인 장애의 모든 원인이 해결됐다는 의미는 아니며 실제 계정 검증이 필요

변경 내용, 검증 범위 및 롤백 방법: [최적화 문서](docs/OPTIMIZATION_2026-09-06.md). 펌웨어·브리지 앱은 변경하지 않으며, 운영 HA 설치·실기기 검증은 별도입니다.


## v0.3.5: 종합계약과 파워플래너 진단

- 아파트(종합계약), 아파트(종합계약/나)를 단일계약과 같은 청구 조회 경로로 허용합니다. 종합계약 청구 동작은 사용자 보고에 근거하며 이번 작업에서 실계정으로 재검증하지 않았습니다.
- 아파트 최신 청구 조회에도 파워플래너를 연결합니다. 2026-09-06 확인한 공식 MYM001D00.xml의 dataInit처럼 custNo에는 세대계약번호 SI_CUST_NO를 사용하고 chgYmd에는 전체 계약변경일을 전달합니다. 아파트 전체 CUST_NO를 대신 보내지 않습니다.
- 현재 검침기간 누적 사용량은 F_AP_QT의 유효한 숫자만 사용합니다. RETURN_CD가 제공되면서 00 이외이면 값을 채우지 않습니다. 코드 없는 기존 응답은 숫자 유효성을 검사합니다. 90 등의 정확한 서버 원인(AMI 미지원, 데이터 지연 등)은 코드만으로 단정하지 않습니다.
- PREDICT_TOT는 공식 XML에서 예상 전기요금으로 설명되며 실제 kWh 단위가 검증되지 않았습니다. 기존의 무조건적인 kWh 매핑을 중단하고 한전 예측 사용량 엔티티의 ID는 유지합니다. 이 센서가 사용 불가로 표시되는 것은 수치 조작이나 임의 단위 변경을 막기 위한 조치입니다.
- 두 엔티티의 속성 data_status, data_status_message, return_code에서 미제공/연결 실패/요청 제한/형식 오류/단위 미확인을 구분합니다. 현재 사용량에 과거 청구월·기간을 붙이지 않습니다.
- 부가 조회가 실패해도 월별 청구 사용량·요금은 유지합니다. 인증 실패는 기존 재인증 흐름에 전달합니다. 명시적인 과거월 조회에는 현재 사용량을 섞지 않습니다.
- 서버 null을 0으로, 청구 사용량을 현재 사용량으로, 예측 요금을 예측 kWh로 대체하지 않습니다. 실제 계약에 값이 제공되는지는 업데이트 후 확인이 필요합니다.

업데이트: HACS에서 0.3.5를 내려받고 Home Assistant를 재시작합니다. 기존 통합을 삭제하거나 sed 수정을 다시 적용할 필요가 없습니다. 이 GitHub 배포는 운영 HA에 직접 설치하거나 재시작하지 않습니다.


## 한전ON v0.3.6 — 종합계약 요청 복원 및 1차 진단 개선

- 사용자 성공 재현에 따라 정확히 `아파트(종합계약)`인 고객은 파워플래너에 `custNo=customer_number`, `housCntrNo=house_contract_number`, `chgYmd=""`를 전달합니다. 임의 고객번호나 추가 엔드포인트를 사용하지 않습니다.
- 이 계약의 현재 사용량 `F_AP_QT`는 사용자 보고에 근거한 Wh 프로필로 처리해 한 번만 1000으로 나눕니다. 단일계약·종합계약/나·주택용은 기존 요청 및 단위 처리를 유지합니다. 값 크기로 단위를 추측하지 않으며 서버 전체의 공통 규격으로 확정한 것은 아닙니다.
- 예측 사용량의 `PREDICT_TOT` 에너지 단위는 아직 검증되지 않았습니다. v0.3.5의 표시 보류를 유지하며, 이 릴리스가 두 센서 모두 숫자를 보장하지는 않습니다.
- 기존 `return_code`에 같은 값의 `provider_return_code` 별칭을 추가하고 `integration_version`, `request_variant`, `value_divisor`를 표시합니다. 필드 오류가 발생해도 안전하게 검증된 반환 코드를 보존합니다.
- 부가 조회 실패는 월별 청구 데이터를 중단하지 않습니다. 과거월 조회에는 현재 사용량을 섞지 않고, 인증 만료와 취소는 기존 흐름에 전달합니다. 도메인·설정·고객 및 엔티티 ID는 유지합니다.

### 적용 및 다음 확인

HACS에서 0.3.6으로 업데이트 후 Home Assistant를 재시작합니다. 통합을 삭제하거나 종합계약 허용 sed 명령을 다시 실행할 필요가 없습니다.
두 센서의 `data_status`, `return_code` 또는 `provider_return_code`, `data_status_message`, `integration_version`을 확인해 회신합니다. 속성 회신을 받은 뒤 요청 결과와 예측 단위에 관한 후속 수정을 별도 진행합니다. 비밀번호·토큰·쿠키·고객번호·원본 응답은 공개하지 마세요.

이번 변경의 근거는 사용자 재현 코드와 계정 동작 보고입니다. 실제 사용자 HA/계정에 직접 접속하거나 예측 단위를 검증하지 않았습니다. 실제 서버 미제공 값을 0이나 과거 청구량으로 대체하지 않습니다.
최소 Home Assistant 버전은 2026.8.3입니다. 문제가 생기면 HACS 재다운로드에서 0.3.5로 되돌릴 수 있습니다.


## v0.3.7: 진단 속성 표시 복구

0.3.5/0.3.6에서는 값이 없는 파워플래너 센서를 unavailable로 표시해 HA가 사용자 정의 속성을 숨겼습니다. 청구 조회가 정상인 경우 이제 누락값은 unknown으로 표시하고 진단 속성은 유지합니다. 전체/고객별 청구 조회 실패는 기존처럼 unavailable입니다. 숫자 생성이나 예측 단위 확정은 하지 않았습니다.

[상태 확인 템플릿과 해석](docs/POWER_PLANNER_DIAGNOSTICS.md). 0.3.7 업데이트 후 Home Assistant를 재시작하며 통합 재등록은 필요 없습니다.


## 한전ON v0.3.8

### 종합계약의 두 파워플래너 센서 복원

- 정확히 아파트(종합계약)에 대해 사용자가 정상 동작을 확인한 요청 조합을 유지합니다: custNo=customer_number, housCntrNo=house_contract_number, chgYmd="".
- 같은 호환 프로필에서 F_AP_QT와 PREDICT_TOT를 각각 1000으로 나누어 기존 현재/예측 사용량 센서에 전달합니다. 종합계약 예측값을 항상 None으로 버리던 처리를 제거합니다. 추가 옵션이나 템플릿 입력은 필요 없습니다.
- 변환은 사용자 재현 보고에 기반합니다. PREDICT_TOT의 물리적 의미와 단위가 한전의 모든 계약에서 같다고 확인한 것은 아니며, 공개 화면의 요금 설명과의 차이는 남아 있습니다. conversion_basis 속성에 user_reported_combined_contract를 표시합니다. 숫자가 그럴듯하다는 이유로 단위를 자동 추정하지 않습니다.
- 두 필드의 파싱과 진단을 분리합니다. 한쪽 null 또는 비정상 숫자는 다른 정상값이나 청구 데이터를 지우지 않습니다. 실제 0은 보존하고 음수, bool, NaN/Infinity는 거부합니다. 서버 실패 코드를 무시하거나 값을 조작하지 않습니다.
- HA 내부에는 나눗셈 결과 정밀도를 유지하고 표시 권장 소수점만 2자리로 지정합니다. 정상 청구 상태의 누락값은 unknown과 진단 속성을 함께 게시합니다.
- 단일계약·종합계약/나·주택용의 요청/변환/예측 정책은 유지합니다. 기존 도메인, 설정, 34개 센서 및 unique_id를 유지하고 과거월에는 현재 사용량을 섞지 않습니다. 인증 만료와 취소는 정상 전파됩니다.

### 적용과 검증 범위

HACS에서 0.3.8로 업데이트한 후 Home Assistant 전체 재시작을 수행합니다. 통합 삭제, sed 재수정, 진단값 선제 회신은 필요 없습니다. 현재 값이 없는 경우에만 개별 data_status를 확인합니다.

테스트는 합성 응답을 사용하는 파서·API·실제 HA 상태 머신·Jinja 템플릿 회귀입니다. 운영 HA와 실제 계정에는 접근하지 않았으므로 실계정에서의 완전 동작이나 한전 서버 데이터 제공을 보장하지 않습니다. 사용자 원본 응답/계정 정보/실제 사용량은 커밋하지 않습니다. 기존 기록이나 통계는 변경하지 않습니다.

문제가 생기면 HACS 재다운로드에서 0.3.7을 선택할 수 있습니다. 최소 Home Assistant 버전은 2026.8.3입니다.
