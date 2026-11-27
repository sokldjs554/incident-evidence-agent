# 주문 서비스의 응답 지연 · Redis 재시작 이후 상품 조회 장애

## 증상
주문 서비스의 상품 조회 응답이 느려졌다. order-service에서 캐시 미스와 DB 연결 오류가 발생했고 api-gateway는 /products 요청의 upstream timeout을 기록했다.

## 원인
redis-cache 노드를 재시작한 뒤 캐시가 비었다. 상품 조회 트래픽이 MySQL에 몰려 연결 상한에 도달했다. 조사 대상 변경은 계획된 Redis 재시작이다.

## 출처
이 비교 사례는 기존 합성 시나리오 cache_stampede(seed=42)의 변경 이벤트와 오류 로그를 한국어로 요약했다. 현재 조사 대상 db_pool_exhaustion과 별개의 시나리오다.
