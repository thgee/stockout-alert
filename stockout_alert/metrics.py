"""제품·묶음(창고 하나 또는 전체)의 판매 속도와 남은 일수를 계산하는 파일.

평범한 for 반복문과 한 단계짜리 dict만 쓴다. defaultdict, 람다(정렬 제외), 중첩
컴프리헨션, 튜플 키 dict는 쓰지 않는다. 판정(안전/경보/판정불가)은 이 파일에서 하지
않는다. judge.py가 이 파일의 결과를 받아서 판정한다.
"""

import statistics
from datetime import date, timedelta

from stockout_alert.models import (
    BLOCKING_ISSUE_CODES,
    CleanData,
    DataIssue,
    InventoryRow,
    Rules,
    RunInfo,
    SalesDates,
    SalesRow,
    SkuInfo,
    SkuMetrics,
    UnitMetrics,
)


def compute_window(run_date: date, rules: Rules) -> tuple[date, date]:
    """계산 기간을 정한다. 예: 제품 A. 끝 = 기준일 - 지연일, 시작 = 끝에서 창 길이만큼 전."""
    end = run_date - timedelta(days=rules.expected_sales_lag_days)
    start = end - timedelta(days=rules.velocity_window_days - 1)
    return start, end


def product_daily_totals(sales: list[SalesRow], sku: str, start: date, last_day: date) -> dict[date, int]:
    """제품 A의 모든 채널(모르는 채널 포함)을 합친 날짜별 판매량. 기록 없는 날은 키가 없다."""
    totals: dict[date, int] = {}
    for row in sales:
        if row.sku != sku or row.sale_date < start or row.sale_date > last_day:
            continue
        if row.sale_date in totals:
            totals[row.sale_date] = totals[row.sale_date] + row.qty
        else:
            totals[row.sale_date] = row.qty
    return totals


def unit_daily_totals(sales: list[SalesRow], sku: str, channels: list[str], start: date, last_day: date) -> dict[date, int]:
    """제품 A의 창고 B 묶음에 속한 채널만 합친 날짜별 판매량."""
    totals: dict[date, int] = {}
    for row in sales:
        if row.sku != sku or row.channel not in channels or row.sale_date < start or row.sale_date > last_day:
            continue
        if row.sale_date in totals:
            totals[row.sale_date] = totals[row.sale_date] + row.qty
        else:
            totals[row.sale_date] = row.qty
    return totals


def find_gap_dates(totals: dict[date, int], start: date, end: date) -> list[date]:
    """start부터 end까지 하루씩 보며 기록이 없는 날을 모은다."""
    gaps: list[date] = []
    day = start
    while day <= end:
        if day not in totals:
            gaps.append(day)
        day = day + timedelta(days=1)
    return gaps


def find_spike_dates(totals: dict[date, int], multiplier: float) -> list[date]:
    """가운데 값(중앙값)의 multiplier배를 넘는 날을 급증일로 본다. 중앙값이 0 이하면 없음."""
    if not totals:
        return []
    middle = statistics.median(totals.values())
    if middle <= 0:
        return []
    spikes: list[date] = []
    for day, qty in totals.items():
        if qty > middle * multiplier:
            spikes.append(day)
    return spikes


def sum_over_dates(totals: dict[date, int], dates: list[date]) -> int:
    """dates를 돌며 totals에 있는 날의 값만 더한다. 없는 날은 "그 채널 0개 판매"로 본다."""
    total = 0
    for day in dates:
        if day in totals:
            total = total + totals[day]
    return total


def find_channel_missing_dates(unit_totals: dict[date, int], recorded: list[date]) -> list[date]:
    """제품 A 기록은 있는데 이 묶음 채널 행만 없는 날."""
    missing: list[date] = []
    for day in recorded:
        if day not in unit_totals:
            missing.append(day)
    return missing


def compute_days_left(stock: int, total_sold: int, day_count: int, age_days: int) -> float | None:
    """남은 일수를 0.1일 단위로 내려서 돌려준다. 속도를 모르면 None(0이나 무한대로 바꾸지 않는다)."""
    if total_sold <= 0 or day_count <= 0:
        return None
    # 정수 몫(//)만 쓰므로 소수 오차가 없다. 재고를 센 뒤 지난 날(age_days)만큼 뺀다.
    tenths = (stock * day_count * 10) // total_sold - age_days * 10
    return max(0, tenths) / 10


def unit_names(warehouse_to_unit: dict[str, str]) -> list[str]:
    """묶음 표의 값을 처음 나온 순서대로, 겹치지 않게 모은다."""
    names: list[str] = []
    for unit in warehouse_to_unit.values():
        if unit not in names:
            names.append(unit)
    return names


def channels_of_unit(unit: str, channel_to_unit: dict[str, str]) -> list[str]:
    """묶음 표에서 이 묶음(창고 B)에 속한 채널 이름."""
    result: list[str] = []
    for channel, mapped_unit in channel_to_unit.items():
        if mapped_unit == unit:
            result.append(channel)
    return result


def warehouses_of_unit(unit: str, warehouse_to_unit: dict[str, str]) -> list[str]:
    """묶음 표에서 이 묶음(창고 B)에 속한 창고 이름."""
    result: list[str] = []
    for warehouse, mapped_unit in warehouse_to_unit.items():
        if mapped_unit == unit:
            result.append(warehouse)
    return result


def stock_of_unit(rows: list[InventoryRow], sku: str, warehouses: list[str]) -> tuple[int, bool]:
    """제품 A, 묶음에 속한 창고들의 판매가능 재고 합과, 재고 행이 하나라도 있었는지."""
    total = 0
    has_row = False
    for row in rows:
        if row.sku == sku and row.warehouse in warehouses:
            total = total + row.qty
            has_row = True
    return total, has_row


def excluded_of_unit(rows: list[InventoryRow], sku: str, warehouses: list[str]) -> list[tuple[str, int]]:
    """제품 A, 묶음에 속한 창고들의 계산 제외 재고(상태, 수량) 목록."""
    result: list[tuple[str, int]] = []
    for row in rows:
        if row.sku == sku and row.warehouse in warehouses:
            result.append((row.status, row.qty))
    return result


def find_unknown_channels(
    sales: list[SalesRow], sku: str, start: date, last_day: date, known_channels: tuple[str, ...]
) -> list[str]:
    """제품 A의 계산 기간 안 판매가 있는, 묶음 표에 없는 채널 이름(겹치지 않게)."""
    result: list[str] = []
    for row in sales:
        if row.sku != sku or row.sale_date < start or row.sale_date > last_day or row.channel in known_channels:
            continue
        if row.channel not in result:
            result.append(row.channel)
    return result


def last_sale_date_of(sales: list[SalesRow], sku: str, channels: list[str] | None) -> date | None:
    """제품 A(채널 목록을 주면 그 채널만)의 파일 전체 마지막 판매일. 기록이 없으면 None."""
    last: date | None = None
    for row in sales:
        if row.sku != sku or (channels is not None and row.channel not in channels):
            continue
        if last is None or row.sale_date > last:
            last = row.sale_date
    return last


def make_sales_dates(sales: list[SalesRow], sku: str, run_info: RunInfo, rules: Rules) -> SalesDates:
    """제품 A를 기준으로 날짜 묶음(기록 있는 날, 빈 날, 급증일, 쓰는 날)을 정한다.

    빈 날을 0으로 채우지 않는다(README 데이터 문제 D5). 빈 날은 "넉넉하게 잡은 일수"를 계산할 때만 0개로 친다.
    """
    start = run_info.window_start
    last_day = min(run_info.window_end, run_info.complete_last_date)
    totals = product_daily_totals(sales, sku, start, last_day)
    recorded = sorted(totals.keys())
    gaps = find_gap_dates(totals, start, last_day)
    spikes = find_spike_dates(totals, rules.outlier_multiplier)
    used: list[date] = []
    for day in recorded:
        if day not in spikes:
            used.append(day)
    return SalesDates(start=start, end=run_info.window_end, last_day=last_day, recorded=recorded, gaps=gaps, spikes=spikes, used=used)


def compute_unit(unit: str, channels: list[str], warehouses: list[str], sku: str, clean: CleanData, dates: SalesDates, age_days: int) -> UnitMetrics:
    """묶음 하나(예: 제품 A의 창고 B)의 판매 합과 일수 세 개(기본/넉넉/빠듯)를 구한다."""
    unit_totals = unit_daily_totals(clean.sales, sku, channels, dates.start, dates.last_day)
    used_total = sum_over_dates(unit_totals, dates.used)
    recorded_total = sum_over_dates(unit_totals, dates.recorded)
    stock, has_row = stock_of_unit(clean.sellable_inventory, sku, warehouses)
    if not has_row:  # 판매가능 행이 없어도 입고예정·이전중 행이 있으면 재고 파일에 이 제품은 있는 것이다
        has_row = stock_of_unit(clean.excluded_inventory, sku, warehouses)[1]
    used_count = len(dates.used)
    velocity = used_total / used_count if used_total > 0 else None
    days_left = compute_days_left(stock, used_total, used_count, age_days)  # 기본: 빈 날·급증일 다 뺌
    days_best = compute_days_left(stock, used_total, used_count + len(dates.gaps), age_days)  # 넉넉하게: 빈 날 0개로
    days_worst = compute_days_left(stock, recorded_total, len(dates.recorded), age_days)  # 빠듯하게: 급증일 넣음
    excluded: list[tuple[str, int, float | None]] = []
    for status, qty in excluded_of_unit(clean.excluded_inventory, sku, warehouses):
        excluded.append((status, qty, compute_days_left(qty, used_total, used_count, 0)))  # 들어오면 며칠분
    missing = find_channel_missing_dates(unit_totals, dates.recorded)
    last_sale = last_sale_date_of(clean.sales, sku, channels)
    return UnitMetrics(unit, channels, stock, has_row, excluded, used_total, recorded_total, velocity, days_left, days_best, days_worst, missing, last_sale)


def _has_blocking_issue(issues: list[DataIssue], sku: str) -> bool:
    """제품 A에 ROW_PARSE_ERROR·invalid_stock 같은, 판정 자체를 막는 정리 기록이 있는지."""
    for issue in issues:
        if issue.sku == sku and issue.code in BLOCKING_ISSUE_CODES:
            return True
    return False


def compute_sku_metrics(info: SkuInfo, clean: CleanData, run_info: RunInfo, rules: Rules) -> SkuMetrics:
    """제품 하나의 계산 결과를 만든다. 날짜 → 창고 묶음 고르기 → 제품 전체 합계 순서. 판정은 아직 안 한다."""
    sku = info.sku
    dates = make_sales_dates(clean.sales, sku, run_info, rules)
    product_totals = product_daily_totals(clean.sales, sku, dates.start, dates.last_day)
    window_sales_total = sum_over_dates(product_totals, dates.recorded)
    unknown_channels = find_unknown_channels(clean.sales, sku, dates.start, dates.last_day, rules.known_channels)

    warehouse_units: list[UnitMetrics] = []
    skipped_units: list[tuple[str, int]] = []
    for unit in unit_names(rules.warehouse_to_unit):
        channels = channels_of_unit(unit, rules.channel_to_unit)
        warehouses = warehouses_of_unit(unit, rules.warehouse_to_unit)
        unit_metrics = compute_unit(unit, channels, warehouses, sku, clean, dates, run_info.snapshot_age_days)
        if unit_metrics.recorded_total == 0:
            stock, _ = stock_of_unit(clean.sellable_inventory, sku, warehouses)
            skipped_units.append((unit, stock))
            continue
        warehouse_units.append(unit_metrics)

    all_warehouses = list(rules.warehouse_to_unit.keys())
    stock_total, _ = stock_of_unit(clean.sellable_inventory, sku, all_warehouses)
    unknown_totals = unit_daily_totals(clean.sales, sku, unknown_channels, dates.start, dates.last_day)
    unknown_qty = sum_over_dates(unknown_totals, dates.recorded)

    has_inventory_rows = stock_of_unit(clean.sellable_inventory + clean.excluded_inventory, sku, all_warehouses)[1]
    last_sale_date = last_sale_date_of(clean.sales, sku, None)

    return SkuMetrics(
        sku=sku, name=info.name, lead_time_days=info.lead_time_days, safety_days=info.safety_days,
        threshold=info.lead_time_days + info.safety_days, has_inventory_rows=has_inventory_rows,
        has_sales_rows=last_sale_date is not None, last_sale_date=last_sale_date,
        has_blocking_issue=_has_blocking_issue(clean.issues, sku),
        window_sales_total=window_sales_total, daily_totals=product_totals, dates=dates,
        unknown_channels=unknown_channels, warehouse_units=warehouse_units, skipped_units=skipped_units,
        stock_total=stock_total, unknown_qty=unknown_qty,
    )


def compute_all_metrics(clean: CleanData, run_info: RunInfo, rules: Rules) -> list[SkuMetrics]:
    """마스터에 적힌 순서대로 제품마다 계산한다."""
    result: list[SkuMetrics] = []
    for info in clean.skus.values():
        result.append(compute_sku_metrics(info, clean, run_info, rules))
    return result


def collect_channel_gap_issues(metrics_list: list[SkuMetrics]) -> list[DataIssue]:
    """묶음의 channel_missing_dates를 channel_rows_missing 경고로 모은다."""
    issues: list[DataIssue] = []
    for metrics in metrics_list:
        for unit in metrics.warehouse_units:
            for day in unit.channel_missing_dates:
                issues.append(DataIssue(code="channel_rows_missing", sku=metrics.sku, issue_date=day, detail=unit.unit, action="해당 채널 0개로 계산"))
    return issues
