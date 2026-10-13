# 주택용 직접계약의 센서 값 누락 재현

2026-10-13 공개 화면·API 비교 조사 결과는 [원인 조사 문서](SENSOR_ROOT_CAUSE.md)에 기록했습니다. 주택용 상세 조회의 누락과 별도 파워플래너 사이트의 조회 경로 차이를 확인했습니다. 아래 합성 재현 결과만으로 실제 서버 미제공을 판정할 수 없습니다.

## 확인한 현상과 수정 내용

사용자 환경은 통합 0.4.0, Home Assistant 2026.10.1이며 계약 유형은 주택용 직접계약입니다. 제공된 진단 파일에는 마지막 갱신 성공, 고객 1명, 청구 데이터 1건, 청구 오류 0건으로 기록되어 있습니다. 사용자는 청구금액 이외의 요금 항목, 작년 월별 사용량, 검침 지침과 일부 사용량 센서가 `unknown`이라고 보고했습니다.

진단 파일의 `parsed_fields`는 dataclass에 정의된 필드 이름입니다. 실제 서버에서 값을 받은 필드 목록이 아니므로, 이 목록으로 센서 값 존재 여부를 판단할 수 없습니다.

수정된 주택용 조회는 `/my/memo/mainChart`의 24개월 이력과 `/my/charge/view/`의 같은 청구월 상세 데이터를 결합합니다. 상세 응답의 검침 지침·검침일·전월 및 전년동월 사용량과 요금 목록을 기존 `KepcoBill` 필드에 연결합니다. 기존 엔티티 ID와 34개 센서 구성은 유지합니다.

월별 사용량 센서는 이력에서 상대 월을 찾습니다. 작년 같은 기간의 이력이 없거나 해당 월의 사용량이 비어 있으면 `unknown`입니다. 전월·전년동월 사용량은 상세 응답을 우선하고 해당 값이 비어 있으면 청구월 기준 이력에서 보충합니다. 이에 의존하는 CO₂ 추정 센서도 값을 얻습니다. Power Planner 처리는 이번 수정의 대상에서 제외했습니다.

이력 요청의 `months`는 기존 `13`에서 상세 화면과 같은 `24`로 늘렸습니다. 실제 서버가 반환하는 이력 범위는 계정과 명의변경 시점 등에 따라 별도 확인이 필요합니다.

상세 요청 실패 시 기존 월별 데이터는 유지합니다. 주택용 센서 속성 `house_detail_status`는 `ok`, `connection_error`, `rate_limited`, `invalid_response`로 상세 조회 결과를 나타냅니다. 다른 청구월의 상세 응답은 섞지 않으며 실제 0과 음수 할인 금액을 보존합니다. `ok`는 모든 선택 항목이 제공되었다는 뜻은 아닙니다. 요금 제목 매핑과 응답 형식은 실제 계정에서도 확인해야 합니다.

## 로컬 합성 응답 재현

저장소 루트에서 실행합니다.

```powershell
.\.venv\Scripts\python.exe -m tools.reproduce_sensor_values --demo --output sensor-reproduction.safe.json
```

도구는 24개월 이력과 검침·요금 상세 정보를 포함한 주택용 합성 응답을 수정된 API 클라이언트에 전달합니다. 실제 한전 서버에는 접속하지 않습니다. 기존 파서와 `KepcoOnSensor`의 `native_value`, `available`, 진단 속성을 평가합니다. 합성 자료에서 30개 센서의 값 연결을 확인하며 Power Planner 2개와 건물·단지 평균 2개는 비어 있습니다.

이 합성 응답은 사용자 계정의 원본 응답을 복제한 자료가 아닙니다. 공개 화면 스키마에 맞춘 테스트 입력으로 값이 기존 엔티티에 전달되는지 검증하는 자료입니다. `tests/fixtures/house_bill_detail.json`도 실제 캡처가 아닌 합성 상세 응답입니다.

## 실제 한전ON 조회

```powershell
.\.venv\Scripts\python.exe -m tools.reproduce_sensor_values --output sensor-reproduction.safe.json
```

일반 로컬 터미널에서 실행하고 아이디·비밀번호를 직접 입력한 뒤 고객을 선택합니다. 비밀번호는 화면에 표시되지 않습니다. 인증 상태는 프로세스 메모리에만 유지하며 자동 재로그인용 비밀번호를 저장하지 않습니다. 조회 대상은 선택한 고객 한 명의 청구 이력·상세 데이터이며 현재 클라이언트의 Power Planner 요청도 그대로 수행합니다. 운영 Home Assistant의 설정과 센서는 수정하지 않습니다.

저장되는 결과는 응답 필드의 존재 여부와 타입, 파싱된 값의 존재 여부, 센서 값의 존재 여부, 숫자 0 여부, Power Planner 진단 속성입니다. 비밀번호, 토큰, 쿠키, 고객번호, 주소, 실제 사용량·청구금액, 원본 요청·응답은 저장하지 않습니다. 고객 선택을 위한 위치 표시는 로컬 터미널에만 나타납니다.

`responses` → `parsed_fields`·`parsed_charge_fields` → `sensors`를 비교하면 서버 응답의 누락, 파서가 채우지 않는 필드, 센서의 표시 보류를 구분하는 데 도움이 됩니다. 실패 시 `failed_stage`와 예외 종류만 기록합니다.

## Home Assistant 상태와 비교

이 도구는 실제 Home Assistant 상태 머신에 센서를 등록하지 않습니다. `expected_state`는 기존 센서 속성 평가에 따른 예상 상태입니다. HACS 설치, 엔티티 등록, 통계, 화면 표시까지 재현한 결과로 해석하면 안 됩니다.

실제 HA의 개발자 도구 → 상태에서 같은 센서를 확인하고, Power Planner 두 센서는 다음 템플릿으로 진단 속성을 확인합니다.

```jinja
{% for s in states.sensor
   if s.attributes.get('source_field') in ['F_AP_QT', 'PREDICT_TOT']
   or '한전 예측 사용량' in s.name
   or '현재 검침기간 누적 사용량' in s.name %}
{{ s.entity_id }}
state = {{ s.state }}
integration_version = {{ s.attributes.get('integration_version', '속성 없음') }}
data_status = {{ s.attributes.get('data_status', '속성 없음') }}
provider_return_code = {{ s.attributes.get('provider_return_code', '속성 없음') }}
request_variant = {{ s.attributes.get('request_variant', '속성 없음') }}
{% else %}
대상 센서를 찾지 못했습니다. 실제 엔티티 ID를 개발자 도구에서 확인하세요.
{% endfor %}
```

저장소 기본 테스트 의존성은 Home Assistant 2026.8.3입니다. 결과의 `home_assistant_library_version`을 운영 환경의 2026.10.1과 비교해야 합니다. API·센서 속성 재현 결과만으로 HA 버전 차이에 따른 상태 발행 문제까지 배제할 수 없습니다.
