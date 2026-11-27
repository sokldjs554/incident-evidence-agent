# 장애 보고서: 주문 API 커넥션 풀 고갈 (2025-11-14)

## 요약
2025-11-14 20:12 ~ 20:57 (45분) 동안 주문 조회/생성 API의 오류율이 84%까지 상승했다.
원인은 당일 배포에 포함된 비인덱스 컬럼 조건의 slow query로, DB 커넥션 점유 시간이
길어지며 애플리케이션의 HikariPool connection pool이 고갈되었다.

## 영향
- 주문 API 5xx 오류율 최대 84%, 추정 매출 손실 약 1,200만 원
- api-gateway에서 order-service 대상 upstream timeout(504) 연쇄 발생

## 타임라인 (KST)
- 19:40 order-service v1.8.0 배포 (주문 검색 조건 추가)
- 20:12 mysql slow query 로그 급증 시작 (execution time 10s 이상)
- 20:16 HikariPool "Connection is not available, request timed out" 발생 시작
- 20:20 api-gateway 504 급증, 장애 선언
- 20:49 v1.7.9로 롤백 완료
- 20:57 오류율 정상화

## 근본 원인
신규 쿼리가 인덱스 없는 컬럼(customer_ref)을 조건으로 사용해 풀스캔이 발생했다.
쿼리 하나당 DB 커넥션을 10초 이상 점유하면서 풀(최대 20개)이 고갈되었고,
커넥션을 얻지 못한 요청이 타임아웃되며 게이트웨이까지 연쇄되었다.

## 5 Whys
1. 왜 사용자 요청이 실패했나? — api-gateway가 order-service 응답을 받지 못해 504를 반환했다.
2. 왜 order-service가 응답하지 못했나? — DB 커넥션 풀이 고갈되어 요청이 대기하다 타임아웃됐다.
3. 왜 커넥션 풀이 고갈됐나? — 개별 쿼리의 커넥션 점유 시간이 10초 이상으로 급증했다.
4. 왜 쿼리가 느려졌나? — 신규 쿼리가 인덱스 없는 customer_ref 컬럼으로 풀스캔을 유발했다.
5. 왜 이 쿼리가 배포됐나? — 배포 전 실행계획(EXPLAIN) 검토와 슬로우 쿼리 게이트가 없었다.

## 재발 방지 대책
- [완료] customer_ref 컬럼 인덱스 추가
- [완료] 커넥션 풀 사용률 80% 초과 시 알람 추가
- [진행] CI에 신규 쿼리 EXPLAIN 자동 검토 게이트 도입
- [진행] slow query 발생량 배포 전후 비교 대시보드 구축
