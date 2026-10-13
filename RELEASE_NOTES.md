## 한전ON v0.4.1

- `1bobby-git/HA-Kepco-Meter`를 포크한 `blazepsyki/HA-Kepco-Meter`의 HA 시험용 prerelease입니다.
- 주택용 `/my/charge/view/`의 요금 목록, 당월·전월 지침, 검침일, 전월·전년동월 사용량을 기존 센서에 연결합니다.
- 월별 이력을 24개월로 조회하고 전월·전년동월 사용량을 이력에서 보충합니다. 해당 값을 사용하는 CO₂ 추정 센서도 함께 갱신됩니다.
- 상세 응답의 청구월을 검사하고, 상세 조회 실패 시 월별 데이터는 유지합니다. 주택용 센서의 `house_detail_status` 속성으로 상세 조회 결과를 확인할 수 있습니다.
- 기존 엔티티 ID와 34개 센서 구성은 유지합니다. Power Planner 항목의 조회·파싱 로직은 변경하지 않았습니다.
- 공개 화면 스키마와 합성 응답으로 검증했습니다. 실계정 응답과 운영 HA 적용은 사용자의 시험이 필요합니다.
- 설치 방법과 복구 방법은 [포크 테스트 설치 문서](docs/FORK_TESTING.md)를 참고해 주세요.

## 한전ON v0.4.0

- 주택용 직접계약의 월별 청구액을 새로 노출합니다. `월별 사용량`(이력) 센서에 해당 월 청구액이 있으면 `amount_krw` 속성이 추가됩니다.
- `kepco_on.get_usage_history` 서비스 응답의 각 이력 항목에도 청구액이 있으면 `amount_krw` 필드가 함께 반환됩니다. 청구액이 없는 월은 기존 `month`·`usage_kwh` 형태를 그대로 유지합니다.
- 청구액은 한전ON `mainChart` 응답의 `afterMny`에서 가져오며, 기존 엔티티 ID·고유 ID·서비스 이름과 응답 구조는 모두 호환됩니다.
- 기여자 [@seojingyo](https://github.com/seojingyo)의 PR #10을 병합했습니다.
- HACS에서 `v0.4.0`으로 업데이트한 뒤 Home Assistant를 완전히 재시작하세요.
