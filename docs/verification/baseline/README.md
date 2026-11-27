# 변경 전 검증

원본 커밋은 `a3b2d4c1bf4ff71a95830353f01b0f76d7d4ec71`이다. 시나리오는 `db_pool_exhaustion`, 시드는 42, 엔진은 `scripted`다.

- [CLI 출력](cli.txt). `fivewhys demo --scenario db_pool_exhaustion --seed 42 --engine scripted`를 실행했다. 로그 2,328건과 변경 이벤트 2건을 적재했고, 5턴에 `order-service`와 `v2.4.0` 배포를 원인으로 지목했다. 자동 채점의 로그 증거 정밀도는 1.0이었다.
- [터미널 이미지](before.png). 원본 커밋의 소스를 `git archive`로 추출하고 `script(1)`의 의사 터미널에서 동일한 명령을 실행했다. 재실행 출력과 보관한 CLI 출력이 일치함을 확인했다. Chromium에서 호출 명령과 실제 출력을 고정폭 글꼴로 렌더링했다.
- [동기 API 응답](api.json). `POST /analyze`에 `{"scenario":"db_pool_exhaustion","seed":42,"engine":"scripted"}`를 전송했다.
- [라우트 확인](routes.json). 원본 FastAPI 애플리케이션의 `GET /` 응답은 404였다. OpenAPI에 표시된 사용자 경로는 `/analyze`, `/reports/{report_id}`, `/scenarios`였다.
- [테스트 출력](tests.txt). 원본 테스트 69개가 통과했다.
