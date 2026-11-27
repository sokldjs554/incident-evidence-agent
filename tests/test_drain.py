from fivewhys.logs.drain import DrainMiner, mask_token, tokenize


def test_mask_numbers_and_ids():
    assert mask_token("30000ms") == "<NUM>"
    assert mask_token("12.5s") == "<NUM>"
    assert mask_token("10.0.3.24:5432") == "<IP>"
    assert mask_token("550e8400-e29b-41d4-a716-446655440000") == "<UUID>"
    assert mask_token("deadbeef01") == "<HEX>"
    assert mask_token("(1234)") == "(<NUM>)"
    assert mask_token("HikariPool-1") == "HikariPool-1"  # 순수 파라미터가 아니면 유지


def test_same_pattern_merges_to_one_template():
    miner = DrainMiner()
    ids = {
        miner.add(f"Failed to fetch order ord-{n}: timeout after {n}ms")
        for n in (100, 250, 999)
    }
    assert len(ids) == 1
    (cid,) = ids
    assert miner.count(cid) == 3
    assert "<*>" in miner.template(cid) or "<NUM>" in miner.template(cid)


def test_different_patterns_get_different_templates():
    miner = DrainMiner()
    a = miner.add("Connection is not available, request timed out after 30000ms")
    b = miner.add("Disk usage 91% on /var/log volume")
    assert a != b


def test_variable_position_becomes_wildcard():
    miner = DrainMiner()
    cid = miner.add("Login failed for user alice from 10.0.0.1")
    assert miner.add("Login failed for user bob from 10.0.0.2") == cid
    template = miner.template(cid)
    assert "<*>" in template  # alice/bob 위치
    assert "<IP>" in template


def test_known_limitation_param_in_leading_tokens():
    # Drain 계열의 알려진 한계: prefix tree 키(선행 depth개 토큰)에
    # 마스킹되지 않는 파라미터가 오면 템플릿이 분화된다.
    miner = DrainMiner(depth=2)
    a = miner.add("User alice logged in")
    b = miner.add("User bob logged in")
    assert a != b


def test_tokenize_masks_inline():
    tokens = tokenize("GC pause 812ms exceeds threshold (old gen 92% used)")
    assert "<NUM>" in tokens
