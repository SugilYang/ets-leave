# ETS 연차 자동반영

다우오피스 **휴가 신청서**를 매일 06:00에 읽어 실적공수 앱에 '연차'로 자동 입력합니다.

## 설정 (한 번만)
1. Apps Script(실적공수) 스크립트 속성에 `LEAVE_TOKEN` 추가 — 임의의 긴 문자열 (예: 32자 영숫자)
2. 이 저장소 Settings → Secrets and variables → Actions 에 5개 등록
   - `DAOU_URL` — 다우오피스 주소 (예: https://ets.daouoffice.com)
   - `DAOU_ID` / `DAOU_PW` — 다우 로그인 계정 (휴가신청서 열람 권한 있는 계정)
   - `GAS_URL` — 실적공수 웹앱 배포 URL (…/exec)
   - `LEAVE_TOKEN` — 1번과 같은 값
3. Actions 탭 → "연차 자동반영" → Run workflow 로 수동 실행해 확인

## 동작
- 공수 입력 대상자가 기안한 휴가신청서만 열람 (결재 완료 문서는 `seen_docs.json`에 기억해 재열람 안 함)
- 어제 ~ 2주 뒤 휴가일을 연차 8h / 반차 4h / 반반차 2h 로 입력
- 주말·공휴일·이미 입력된 날·결재 상신된 주는 건너뜀
- 결과는 관리자 다우 알림으로 통보
