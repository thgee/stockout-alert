"""AI 요약에 나온 제품명과 숫자가 실제 계산 결과에 있는 값인지 코드로 확인하는지 보는 테스트. 네트워크는 쓰지 않는다."""

from stockout_alert import summary

GOOD = (
    "(테스트용 가짜 요약) 2026-10-06 기준 경보 7건, 판정 불가 1건, 안전 0건입니다. "
    "가장 급한 제품은 「젤 패드 60매」(남은 0.0일), 「아이크림 20ml」(남은 0.0일), 「비타민 세럼 30ml」(남은 0.0일)입니다. "
    "나머지 경보 4건은 아래 목록에 있습니다. "
    "판정 불가 1건은 「바디 로션 300ml」(판매 기록 없음) 때문에 담당자 확인이 필요합니다. "
    "아래 목록에서 제품별 원인과 할 일을 확인해 주세요."
)
TOP_THREE = "가장 급한 제품은 「젤 패드 60매」(남은 0.0일), 「아이크림 20ml」(남은 0.0일), 「비타민 세럼 30ml」(남은 0.0일)입니다."


def test_wrong_number_rejected(real_report):
    """막는 사고: 요약이 사실 표에 없는 숫자(999.9)를 말하는데 그대로 나간다."""
    facts = summary.build_fact_sheet(real_report())
    text = GOOD.rsplit(" ", 8)[0] + " 「젤 패드 60매」은 남은 일수 999.9일입니다."
    result = summary.check_summary(text, facts)
    assert not result.ok
    assert "숫자 999.9은(는) 사실 표에 없습니다." in result.problems


def test_unknown_product_rejected(real_report):
    """막는 사고: 요약이 없는 제품 이름을 지어내는데 그대로 나간다."""
    facts = summary.build_fact_sheet(real_report())
    text = GOOD.rsplit(" ", 8)[0] + " 「없는 제품 100ml」은 남은 일수 0.0일입니다."
    result = summary.check_summary(text, facts)
    assert not result.ok
    assert "「없는 제품 100ml」은(는) 사실 표에 없는 제품명입니다." in result.problems


def test_rounded_integer_allowed_but_other_integer_not(real_report):
    """막는 사고: "25일"(24.7의 반올림)을 틀렸다고 하거나, "24일"을 맞다고 한다."""
    facts = summary.build_fact_sheet(real_report())
    head = f"경보 7건, 판정 불가 1건입니다. {TOP_THREE} 나머지 경보 4건은 아래 목록에 있습니다."
    assert summary.check_summary(f"{head} 「수분 선크림 50ml」은 남은 25일입니다.", facts).ok
    result = summary.check_summary(f"{head} 「수분 선크림 50ml」은 남은 24일입니다.", facts)
    assert not result.ok
    assert "숫자 24은(는) 사실 표에 없습니다." in result.problems
