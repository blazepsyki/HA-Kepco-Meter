# 포크 테스트 릴리스 설치

포크: [blazepsyki/HA-Kepco-Meter](https://github.com/blazepsyki/HA-Kepco-Meter)

테스트 브랜치: `test/house-bill-detail`

포크의 기본 브랜치도 테스트 브랜치로 설정합니다.

테스트 버전: `0.4.1`. GitHub에서는 prerelease로 표시합니다.

## HACS 설치

1. 기존 HA 백업을 확보합니다.
2. HACS의 커스텀 저장소에 `https://github.com/blazepsyki/HA-Kepco-Meter`를 추가하고 종류를 Integration으로 선택합니다.
3. 포크 저장소의 다운로드 또는 다시 다운로드 화면에서 테스트 릴리스 `v0.4.1`을 선택합니다. 보이지 않으면 버전 선택 화면에서 beta/prerelease 표시 옵션을 켭니다. HACS 버전에 따라 메뉴 이름이 다를 수 있습니다.
4. 같은 `kepco_on` 도메인을 사용하는 기존 저장소와 포크를 동시에 관리하면 같은 폴더에 파일을 설치합니다. HACS에서 원본 저장소 등록을 제거해야 하는 경우에는 Home Assistant의 한전ON 통합 항목을 삭제하지 말고, 포크 설치와 재시작을 이어서 진행합니다.
5. 설치 후 Home Assistant Core를 완전히 재시작합니다.
6. 센서 속성 `integration_version` 또는 다운로드한 진단 파일의 통합 버전이 `0.4.1`인지 확인합니다.

## ZIP 파일로 수동 설치

HACS에서 포크를 선택하기 어려우면 [테스트 릴리스](https://github.com/blazepsyki/HA-Kepco-Meter/releases/tag/v0.4.1)의 `kepco_on-v0.4.1.zip`을 사용합니다.

1. ZIP 내부의 `custom_components/kepco_on`을 HA 설정 디렉터리의 `/config/custom_components/kepco_on`에 덮어씁니다.
2. Home Assistant 설정 검사를 실행합니다.
3. Home Assistant Core를 완전히 재시작합니다.

기존 통합 항목, 계정 설정과 엔티티는 유지합니다. 새 계정 등록이나 통합 삭제·재등록은 필요하지 않습니다.

## 시험 항목

- 주택용 전기요금의 기본요금·전력량요금·기후환경요금·연료비조정요금·부가세·기금 등 값이 웹 상세 화면과 일치하는지 확인합니다.
- 당월 지침·전월 지침·검침일·전월 및 전년동월 사용량을 확인합니다.
- 작년 월별 사용량과 전월·전년동월 CO₂ 추정 센서의 값이 있는지 확인합니다.
- 주택용 센서의 `house_detail_status`를 확인합니다. `ok`는 상세 응답 처리 성공, `connection_error`는 연결 실패, `rate_limited`는 요청 제한, `invalid_response`는 응답 형식 또는 청구월 불일치입니다.
- `ok`여도 서버에 해당 항목이 없거나 요금 제목이 매핑과 다르면 일부 값이 비어 있을 수 있습니다. 실제 계정 응답은 아직 검증하지 않았습니다.

Power Planner 2개 센서와 건물·단지 평균 조회는 이번 수정 범위에 포함되지 않았습니다.

## 원본으로 복구

HACS에서 원본 `1bobby-git/HA-Kepco-Meter`의 `v0.4.0`을 다시 다운로드하거나 원본 파일로 `custom_components/kepco_on`을 교체한 뒤 Home Assistant Core를 재시작합니다. 원본도 같은 도메인과 엔티티 ID를 사용합니다.
