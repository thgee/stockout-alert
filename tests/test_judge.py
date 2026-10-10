"""judge.py의 판정 규칙을 손으로 만든 작은 데이터로 확인하는 테스트."""

from datetime import date, timedelta

from stockout_alert import judge
from stockout_alert.models import (
    STATUS_ALERT,
    STATUS_SAFE,
    STATUS_UNKNOWN,
    SalesDates,
    SkuMetrics,
    UnitMetrics,
)

WINDOW = [date(2026, 9, 21) + timedelta(days=i) for i in range(14)]


def make_dates(gaps=(), spikes=()):
    recorded = [d for d in WINDOW if d not in gaps]
    used = [d for d in recorded if d not in spikes]
    return SalesDates(start=WINDOW[0], end=WINDOW[-1], last_day=WINDOW[-1], recorded=recorded, gaps=list(gaps), spikes=list(spikes), used=used)


def make_unit(**over):
    base = dict(
        unit="국내창고", channels=["coupang", "own_mall"], stock=400, has_stock_row=True, excluded=[],
        used_total=140, recorded_total=140, velocity=10.0, days_left=40.0, days_best=40.0, days_worst=40.0,
        channel_missing_dates=[], last_sale_date=WINDOW[-1],
    )
    base.update(over)
    return UnitMetrics(**base)


def make_sku(units, **over):
    base = dict(
        sku="SKU-A", name="시험 제품", lead_time_days=21, safety_days=7, threshold=28, has_inventory_rows=True,
        has_sales_rows=True, last_sale_date=WINDOW[-1], has_blocking_issue=False, window_sales_total=140,
        daily_totals={d: 10 for d in WINDOW}, dates=make_dates(), unknown_channels=[],
        warehouse_units=list(units), skipped_units=[], stock_total=400, unknown_qty=0,
    )
    base.update(over)
    return SkuMetrics(**base)


def test_safe_only_when_worst_is_enough_and_nothing_missing(rules, make_run_info):
    """막는 사고: 조건을 하나라도 빼먹고 "안전"이라고 말한다. 안전은 네 조건이 모두 맞을 때만 나온다."""
    unit = make_unit(days_best=40.0, days_worst=40.0)
    result = judge.judge_unit(unit, make_sku([unit]), make_run_info(), rules())
    assert result.status == STATUS_SAFE
    assert result.reasons == []


def test_alert_when_even_best_case_is_short(rules, make_run_info):
    """막는 사고: 빈 날이 있다는 이유로 판정 불가로 보내서, 넉넉하게 잡아도 모자란 제품의 경보를 놓친다."""
    unit = make_unit(days_left=15.0, days_best=20.0, days_worst=15.0)
    m = make_sku([unit], dates=make_dates(gaps=[WINDOW[-1]]))
    result = judge.judge_unit(unit, m, make_run_info(), rules())
    assert result.status == STATUS_ALERT
    assert [r.code for r in result.reasons] == ["BELOW_THRESHOLD"]
    assert "남은 15.0일(넉넉하게 잡아도 20.0일), 필요 일수 28일" in result.reasons[0].text


def test_sales_gap_blocks_safe(rules, make_run_info):
    """막는 사고: 늦게 도착한 판매 데이터의 빈 날을 0개로 쳐서 "안전"이라고 말한다."""
    unit = make_unit(days_best=45.0, days_worst=40.0)
    m = make_sku([unit], dates=make_dates(gaps=[WINDOW[-2], WINDOW[-1]]))
    result = judge.judge_unit(unit, m, make_run_info(), rules())
    assert result.status == STATUS_UNKNOWN
    assert [r.code for r in result.reasons] == ["SALES_GAP"]
    assert "2일(10/3~10/4)" in result.reasons[0].text


def test_spike_that_flips_verdict_is_unknown(rules, make_run_info):
    """막는 사고: 행사일 하루를 빼고 계산한 뒤, 넣으면 필요 일수에 못 미치는데도 "안전"이라고 말한다."""
    spike_day = WINDOW[2]
    totals = {d: 10 for d in WINDOW}
    totals[spike_day] = 100
    unit = make_unit(days_best=40.0, days_worst=20.0)
    m = make_sku([unit], dates=make_dates(spikes=[spike_day]), daily_totals=totals)
    result = judge.judge_unit(unit, m, make_run_info(), rules())
    assert result.status == STATUS_UNKNOWN
    assert [r.code for r in result.reasons] == ["SPIKE_CHANGES_VERDICT"]
    assert "9/23 하루 100개(평소의 10.0배)를 빼면 40.0일, 넣으면 20.0일" in result.reasons[0].text


def test_late_snapshot_blocks_safe_and_stale_blocks_everything(rules, make_run_info):
    """막는 사고: 이틀 전 재고로 "안전"이라고 말하거나, 사흘 넘은 재고로 숫자를 계산해 보여 준다."""
    unit = make_unit()
    m = make_sku([unit])
    late = judge.judge_unit(unit, m, make_run_info(snapshot_age_days=2, inventory_as_of=date(2026, 10, 4)), rules())
    assert late.status == STATUS_UNKNOWN
    assert [r.code for r in late.reasons] == ["INVENTORY_LATE"]
    stale = judge.judge_sku(m, [unit], make_run_info(snapshot_age_days=3, inventory_as_of=date(2026, 10, 3)), rules(), [])
    assert stale.status == STATUS_UNKNOWN
    assert [r.code for r in stale.reasons] == ["INVENTORY_STALE"]
    assert stale.units == []
    assert stale.days_left is None


def test_unknown_channel_blocks_safe(rules, make_run_info):
    """막는 사고: 어느 창고 판매인지 모르는 채널이 있는데도 창고별로 "안전"이라고 말한다."""
    unit = make_unit()
    m = make_sku([unit], unknown_channels=["naver"], unknown_qty=60)
    result = judge.judge_unit(unit, m, make_run_info(), rules())
    assert result.status == STATUS_UNKNOWN
    assert [r.code for r in result.reasons] == ["UNKNOWN_CHANNEL"]
    assert "채널(naver)의 판매가 최근 14일에 60개" in result.reasons[0].text


def test_product_status_is_worst_of_units(rules, make_run_info):
    """막는 사고: 한 창고가 안전하다는 이유로 다른 창고의 품절 경보를 덮는다."""
    safe_unit = make_unit(unit="FBA_US", channels=["amazon_us"], days_left=40.0, days_best=40.0, days_worst=40.0)
    alert_unit = make_unit(unit="국내창고", days_left=5.0, days_best=5.0, days_worst=5.0)
    m = make_sku([safe_unit, alert_unit])
    result = judge.judge_sku(m, [safe_unit, alert_unit], make_run_info(), rules(), [])
    assert result.status == STATUS_ALERT
    assert [r.unit for r in result.reasons] == ["국내창고"]
    assert result.days_left == 5.0
    assert result.shortest_unit == "국내창고"
    assert result.urgent is True  # 5.0일은 리드타임 21일보다 짧다


def test_no_sales_history_is_unknown_with_stock_in_text(rules, make_run_info):
    """막는 사고: 판매 기록이 없는 제품을 "속도 0이니 무한히 남았다"고 안전으로 본다."""
    m = make_sku([], has_sales_rows=False, last_sale_date=None, window_sales_total=0, stock_total=1200)
    result = judge.judge_sku(m, [], make_run_info(), rules(), [])
    assert result.status == STATUS_UNKNOWN
    assert result.reasons[0].code == "NO_SALES_HISTORY"
    assert "판매가능 재고 1,200개" in result.reasons[0].text
