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

한전ON 개인(`INDI`) 계정의 아파트 단일·종합계약 및 주택용 직접계약 전기요금 조회를 Home Assistant 센서와 응답 액션으로 가져오는 비공식 커스텀 통합입니다. 실시간 스마트미터가 아니라 한전ON 청구/검침 페이지에서 확인되는 월별 요금 데이터 기반입니다.

Repository: https://github.com/1bobby-git/HA-Kepco-Meter

## 현재 범위

- 지원: 한전ON 개인 계정(`INDI`), 아파트 세대 단일·종합계약 및 주택용 직접계약 요금.
- 미지원: 법인 계정, 전기공사업체 계정, 인증서 로그인, OACX 간편인증 자동화, CAPTCHA/MFA 우회, CO2 실측값. Power Planner 값은 실시간 스마트미터 값이 아닙니다.
- 통신: `https://online.kepco.co.kr`의 고정된 한전ON 경로만 사용하며 TLS 검증을 끄지 않습니다.
- 버전: `v0.4.0`. 선택 고객마다 5개 논리 기기와 34개 센서 엔티티를 생성합니다.

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

## 에너지 대시보드

Energy Dashboard에는 `당월지침` 센서를 전력 사용량 소스로 추가합니다. 이 센서는 누적 검침값이며 `total_increasing` 상태 클래스를 사용합니다. 월별 사용량과 비교 사용량 센서는 한 달 단위 값이므로 누적 소스로 사용하지 않습니다.

## 옵션, 업그레이드, 재인증, 재구성

옵션에서 조정할 수 있는 값은 조회 주기, 온실가스 환산계수, 사용량 이력 응답 길이입니다. 조회 주기는 1·3·6·12·24시간 중 선택하며 기본값은 6시간입니다. 환산계수는 0보다 크고 10 이하이며 기본값은 `0.459`입니다. 이력 응답 길이는 1~24개월이며 기본값은 12개월입니다.

업데이트 후 Home Assistant Core를 완전히 재시작하세요. 기존 엔티티 ID와 unique ID는 유지합니다. 이전 설정의 상세·CO₂ 생성 토글은 마이그레이션에서 정리하며, 사용자가 직접 비활성화한 엔티티는 유지합니다. 쿠키 없는 이전 세션은 저장 비밀번호가 있으면 재로그인하고, 없으면 재인증을 요청합니다. 버전별 변경은 [변경 이력](CHANGELOG.md)을 참고하세요.

재인증은 기존 계정의 비밀번호만 다시 받습니다. 다른 한전ON 계정으로 로그인되면 항목 업데이트가 거절됩니다.

재구성은 선택 고객을 갱신합니다. 통합이 로드된 상태면 한전ON에서 고객 목록을 새로 받아오고, 실패하면 저장된 고객 목록을 기준으로 선택 화면을 엽니다. 선택에서 빠진 고객의 엔티티와 이 통합만 소유한 5개 논리 기기는 정리됩니다.

## 응답 액션

`kepco_on.get_monthly_bill`은 한 고객의 청구 상세를 반환합니다. 아파트/오피스텔과 주택용 직접계약 모두 지정한 월을 조회합니다. 주택용은 `mainChart` 이력에서 요청한 월을 고른 뒤 같은 월의 청구 상세를 결합합니다. 해당 월이 이력에 없으면 조회 오류를 반환합니다.

```yaml
action: kepco_on.get_monthly_bill
data:
  config_entry_id: !secret kepco_on_config_entry_id
  customer_id: !secret kepco_on_customer_id
  month: "202608"
response_variable: kepco_bill
```

`kepco_on.get_usage_history`는 선택 고객의 월별 사용량 이력을 반환합니다. `month`를 비우면 현재 코디네이터가 가진 최신 청구 데이터를 우선 사용합니다. 각 이력 항목은 `month`·`usage_kwh`를 포함하고, 한전ON 응답에 해당 월 청구액이 있으면 `amount_krw`도 함께 반환합니다. 같은 값은 `월별 사용량` 센서의 `amount_krw` 속성으로도 노출됩니다.

```yaml
action: kepco_on.get_usage_history
data:
  config_entry_id: !secret kepco_on_config_entry_id
  customer_id: !secret kepco_on_customer_id
response_variable: kepco_history
```

`config_entry_id`는 개발자 도구 > 액션 또는 자동화 시각 편집기에서 `config_entry_id` 선택기를 열고 KEPCO ON 항목을 선택하면 Home Assistant가 채웁니다. 수동 YAML에 넣어야 할 때는 선택기로 항목을 고른 뒤 YAML 보기로 전환해 생성된 ID를 복사하세요.

`customer_id`는 원본 한전 고객번호가 아니라 통합이 생성한 64자 안정 해시 키입니다. 가장 신뢰할 수 있는 확인 경로는 설정 > 기기 및 서비스 > KEPCO ON 항목 > 점 세 개 메뉴 > 진단 다운로드에서 진단 파일을 내려받은 뒤 `selected_customer_ids` 값을 복사하는 것입니다. 이 값은 응답 액션에 필요한 안전 식별자이며 원본 한전 고객번호나 계약번호가 아닙니다. 그래도 공개 이슈나 자동화 예제에 올릴 필요는 없습니다. 진단 다운로드가 어려운 환경에서는 설정 > 기기 및 서비스 > 엔티티에서 KEPCO ON 센서 엔티티 설정을 열고 entity registry의 unique ID 앞 64자를 확인하는 방법을 대안으로 사용할 수 있습니다.

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

## 주택용 청구 상세

주택용 직접계약은 월별 이력과 같은 청구월의 상세 응답을 결합합니다. 상세에 값이 있으면 전기요금 항목, 검침지침·검침일, 전월·전년동월 사용량과 기존 CO₂ 추정 센서에 반영합니다. 이웃 비교와 세대·공용 사용량 등 주택 응답에 없는 값은 채우지 않습니다.

상세 조회가 실패해도 월별 이력과 청구액은 유지하며, 주택용 센서의 `house_detail_status`에서 `ok`, `connection_error`, `rate_limited`, `invalid_response`를 확인할 수 있습니다. 인증 만료는 재인증 흐름으로 전달됩니다. 과거월 조회에는 현재 검침기간의 사용량을 붙이지 않습니다.

이 기능은 현재 개발 브랜치의 미배포 변경입니다. 배포 버전 표기는 `v0.4.0`을 유지하며 변경 내용은 [변경 이력](CHANGELOG.md)에 구분합니다.

## Power Planner 상태 확인

청구 조회가 정상이고 부가 사용량만 없으면 센서가 `unknown`으로 표시되며 진단 속성은 유지됩니다. 청구 조회 자체가 실패하거나 청구 스냅샷이 없으면 `unavailable`입니다.

| 상태 | 의미 |
| --- | --- |
| `ok` | 정상값 제공. 실제 0도 포함 |
| `no_data` | 유효한 값이 제공되지 않음. 반환 코드만으로 원인을 확정하지 않음 |
| `source_unit_unverified` | 예측 필드의 에너지 단위를 확인하지 못해 표시 보류 |
| `connection_error`, `rate_limited`, `invalid_response` | 부가 요청의 연결·제한·형식 오류 |

개발자 도구 > 템플릿에서 다음과 같이 확인할 수 있습니다.

```jinja
{% for s in states.sensor
   if s.attributes.get('source_field') in ['F_AP_QT', 'PREDICT_TOT'] %}
{{ s.name }}: {{ s.state }}
버전: {{ s.attributes.get('integration_version') }}
상태: {{ s.attributes.get('data_status') }}
반환 코드: {{ s.attributes.get('provider_return_code') }}
설명: {{ s.attributes.get('data_status_message') }}
변환 계수: {{ s.attributes.get('value_divisor') }}
{% else %}
대상 센서가 없습니다. 기기 화면에서 엔티티 상태를 확인하세요.
{% endfor %}
```

## 삭제

설정 > 기기 및 서비스에서 KEPCO ON 항목을 삭제합니다. 이 통합의 세션 Store와 복구 안내도 정리됩니다. 수동 설치를 제거하려면 `custom_components/kepco_on` 폴더를 삭제하고 Home Assistant를 재시작합니다.

## 개발과 기여

API 계약, 보안, 개발 환경과 검증·배포 절차는 [개발 안내](DEVELOPMENT.md)에 통합했습니다. 새 기능과 수정은 해당 동작을 검증하는 테스트와 함께 제안해 주세요. 원본 계정 정보나 캡처는 커밋하지 않습니다.

[@seojingyo](https://github.com/seojingyo)님은 PR #7에서 주택용 파서 경계 테스트를, PR #10에서 월별 청구액 노출 기능과 테스트를 기여하셨습니다. 기여 내역의 근거는 PR과 커밋 이력입니다.

## 브랜드 이미지와 라이선스

`custom_components/kepco_on/brand/`에 투명 PNG 이미지 `icon.png`(256×256), `icon@2x.png`(512×512), `logo.png`, `logo@2x.png`를 포함합니다. 설치 패키지의 이미지를 사용합니다.

MIT 라이선스를 적용합니다. 법적 조건과 저작권 고지는 [LICENSE](LICENSE) 원문을 따릅니다.
