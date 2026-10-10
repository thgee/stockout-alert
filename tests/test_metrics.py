"""metrics.py의 계산 함수를 확인하는 테스트."""

from datetime import date, timedelta

from stockout_alert import metrics
from stockout_alert.config import build_rules
from stockout_alert.models import CleanData, InventoryRow, SalesRow


def test_compute_days_left_subtracts_age_days():
    """막는 사고: 재고 스냅샷이 며칠 지났는데 그만큼 안 빼서 재고를 실제보다 많게 본다."""
    # 재고 100개, 14일 동안 140개 팔림(하루 10개) -> 10일치. 경과일 2일 빼면 8일.
    assert metrics.compute_days_left(stock=100, total_sold=140, day_count=14, age_days=2) == 8.0


def test_missing_recent_days_not_filled_with_zero():
    """막는 사고: 최근 며칠 판매 기록이 없는 날을 0개로 채워 평균을 내서, 속도가 느려 보이고 재고가 넉넉해 보인다."""
    window = [date(2026, 9, 21) + timedelta(days=i) for i in range(14)]
    sales: list[SalesRow] = []
    for line_no, day in enumerate(window[:11], start=1):  # 앞 11일만 하루 12개, 뒤 3일은 기록이 없다
        sales.append(SalesRow(line_no, day, "SKU-A", "coupang", 12))
    inventory = [InventoryRow(1, date(2026, 10, 5), "SKU-A", "국내창고", "판매가능", 120)]
    clean = CleanData(skus={}, sellable_inventory=inventory, excluded_inventory=[], sales=sales, issues=[], inventory_as_of=date(2026, 10, 5))
    run_info = metrics.RunInfo(
        run_date=date(2026, 10, 6), window_start=window[0], window_end=window[-1],
        channel_last_dates={}, complete_last_date=window[-1], sales_late_days=0,
        inventory_as_of=date(2026, 10, 5), snapshot_age_days=1, report_kind="normal", header_warnings=[],
    )
    dates = metrics.make_sales_dates(clean.sales, "SKU-A", run_info, build_rules())
    unit = metrics.compute_unit("국내창고", ["coupang", "own_mall"], ["국내창고"], "SKU-A", clean, dates, run_info.snapshot_age_days)
    assert dates.gaps == window[11:]  # 빈 날 3일(10/2~10/4)을 빈 날로 기억한다
    assert unit.velocity == 12.0  # 기록 있는 11일로 나눈다. 14일로 나눈 9.4가 아니다
    assert unit.days_left == 9.0  # 재고 120개 ÷ 하루 12개 = 10일치, 경과일 1일을 빼서 9.0
