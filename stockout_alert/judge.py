"""제품과 묶음을 경보/안전/판정불가로 판정하는 파일.

규칙 세 문장: 넉넉하게 잡아도 필요 일수에 못 미치면 경보. 빠듯하게 잡아도 필요 일수를 넘고 빠진 데이터가 없을 때만 안전.
그 사이는 사람이 봐야 한다(판정불가). "안전"은 judge_unit() 안의 한 줄에서만 나온다.
"""

import logging
import statistics
from datetime import date, timedelta

from stockout_alert import metrics
from stockout_alert.config import EVENT_DATA_LATE, UNIT_LABELS, log_event
from stockout_alert.models import (
    BLOCKING_ISSUE_CODES,
    REASON_MESSAGES,
    STATUS_ALERT,
    STATUS_RANK,
    STATUS_SAFE,
    STATUS_UNKNOWN,
    CleanData,
    DataIssue,
    Judgment,
    KIND_DATA_LATE,
    KIND_NORMAL,
    Reason,
    Rules,
    RunInfo,
    SalesRow,
    SkuMetrics,
    UnitJudgment,
    UnitMetrics,
)

logger = logging.getLogger(__name__)

# 메모에 쓰는 정리 기록 문구. {n}에 건수가 들어간다.
CLEANUP_NOTE_LABELS = {
    "duplicate_removed": "중복 {n}행 제거",
    "negative_removed": "음수 {n}행 제외",
    "key_conflict_summed": "같은 날·채널 {n}행 합산",
}


# ===== 문구 다듬기 =====


def fmt_date(value: date) -> str:
    """날짜를 월/일로 쓴다. 10월 4일이면 10/4."""
    return f"{value.month}/{value.day}"


def format_value(value: object) -> str:
    """사유 문구에 넣을 값을 다듬는다. 날짜는 월/일, 일수(소수)는 소수 1자리, 수량(정수)은 천 단위 쉼표."""
    if value is None:
        return "?"
    if isinstance(value, date):
        return fmt_date(value)
    if isinstance(value, float):
        return f"{value:.1f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def date_range_text(dates: list[date]) -> str:
    """날짜 목록을 "10/2~10/4"처럼 쓴다. 날이 이어지지 않으면 쉼표로 나열한다."""
    if not dates:
        return ""
    if len(dates) == 1:
        return fmt_date(dates[0])
    if (dates[-1] - dates[0]).days == len(dates) - 1:
        return f"{fmt_date(dates[0])}~{fmt_date(dates[-1])}"
    return ", ".join(fmt_date(day) for day in dates)


def make_reason(code: str, unit: str | None, **values: object) -> Reason:
    """REASON_MESSAGES[code]의 빈자리를 값으로 채워 사유 한 건을 만든다. 묶음 이름은 사람이 읽는 이름으로 바꾼다."""
    label = "제품 전체" if unit is None else UNIT_LABELS.get(unit, unit)
    filled: dict[str, str] = {}
    for key, value in values.items():
        filled[key] = format_value(value)
    text = REASON_MESSAGES[code].format(unit=label, **filled)
    return Reason(code=code, unit=unit, text=text)


# ===== 실행 한 번의 날짜 계산 =====


def find_channel_last_dates(sales: list[SalesRow], known_channels: tuple[str, ...]) -> dict[str, date]:
    """묶음 표에 있는 채널마다 판매 기록의 마지막 날."""
    last_dates: dict[str, date] = {}
    for row in sales:
        if row.channel not in known_channels:
            continue
        if row.channel not in last_dates or row.sale_date > last_dates[row.channel]:
            last_dates[row.channel] = row.sale_date
    return last_dates


def header_warnings_for(run_info_parts: dict, rules: Rules) -> list[str]:
    """머리말에 넣을 경고. 판매 데이터 지연과 재고 스냅샷 경과일에 대한 것만 여기서 만든다."""
    warnings: list[str] = []
    late_days = run_info_parts["sales_late_days"]
    if late_days > 0:
        by_channel = ", ".join(f"{channel} {fmt_date(day)}" for channel, day in run_info_parts["channel_last_dates"].items())
        warnings.append(
            f"판매 데이터가 {late_days}일 늦음: 완전한 마지막 날 {fmt_date(run_info_parts['complete_last_date'])}"
            f"(기대 {fmt_date(run_info_parts['window_end'])}). 채널별 마지막 날 {by_channel}. 그 뒤 날짜는 빈 날로 계산했습니다."
        )
        log_event(logger, logging.WARNING, EVENT_DATA_LATE, warnings[-1])
    age_days = run_info_parts["snapshot_age_days"]
    as_of = run_info_parts["inventory_as_of"]
    if age_days > rules.max_snapshot_age_days:
        warnings.append(f"재고 스냅샷이 {age_days}일 전({fmt_date(as_of)}) 것이라 전 제품 판정 불가. 이 데이터로 보려면 --run-date {as_of + timedelta(days=1)}")
    elif age_days > rules.expected_snapshot_age_days:
        warnings.append(f"재고 스냅샷이 {age_days - rules.expected_snapshot_age_days}일 늦음({fmt_date(as_of)}). 안전 판정은 보류됩니다.")
    return warnings


def assess_run(run_date: date, clean: CleanData, rules: Rules) -> RunInfo:
    """실행 한 번의 날짜를 정한다. 계산 기간, 판매 지연 일수, 재고 경과일, 보고 종류, 머리말 경고."""
    window_start, window_end = metrics.compute_window(run_date, rules)
    channel_last_dates = find_channel_last_dates(clean.sales, rules.known_channels)
    complete_last_date = min(channel_last_dates.values()) if channel_last_dates else window_start
    sales_late_days = max(0, (window_end - complete_last_date).days)
    snapshot_age_days = (run_date - clean.inventory_as_of).days
    parts = {
        "window_end": window_end, "channel_last_dates": channel_last_dates, "complete_last_date": complete_last_date,
        "sales_late_days": sales_late_days, "inventory_as_of": clean.inventory_as_of, "snapshot_age_days": snapshot_age_days,
    }
    late = sales_late_days > 0 or snapshot_age_days > rules.expected_snapshot_age_days
    return RunInfo(
        run_date=run_date, window_start=window_start, window_end=window_end,
        channel_last_dates=channel_last_dates, complete_last_date=complete_last_date,
        sales_late_days=sales_late_days, inventory_as_of=clean.inventory_as_of,
        snapshot_age_days=snapshot_age_days, report_kind=KIND_DATA_LATE if late else KIND_NORMAL,
        header_warnings=header_warnings_for(parts, rules),
    )


# ===== 제품 단위: 계산 자체가 안 되는 경우 =====


def find_blocking_reason(m: SkuMetrics, run_info: RunInfo, rules: Rules, issues_for_sku: list[DataIssue]) -> Reason | None:
    """제품 단위. 계산 자체를 할 수 없는 이유를, 처음 걸린 하나만 돌려준다. 없으면 None."""
    for issue in issues_for_sku:
        if issue.code in BLOCKING_ISSUE_CODES:
            return make_reason("ROW_PARSE_ERROR", None, detail=f"{issue.detail}, {issue.action}")
    if not m.has_inventory_rows:
        return make_reason("NO_INVENTORY", None)
    if run_info.snapshot_age_days > rules.max_snapshot_age_days:
        return make_reason("INVENTORY_STALE", None, age=run_info.snapshot_age_days, as_of=run_info.inventory_as_of)
    if not m.has_sales_rows:
        return make_reason("NO_SALES_HISTORY", None, stock=m.stock_total)
    if m.window_sales_total == 0:
        return make_reason(
            "NO_RECENT_SALES", None, window_days=rules.velocity_window_days, stock=m.stock_total,
            last_sale=m.last_sale_date or "없음",
        )
    return None


# ===== 묶음 단위: 묶음 하나 판정 =====


def make_unit_judgment(u: UnitMetrics, status: str, reasons: list[Reason]) -> UnitJudgment:
    """묶음 계산 결과(UnitMetrics)에 판정 결과를 붙여 UnitJudgment를 만든다."""
    return UnitJudgment(
        unit=u.unit, status=status, reasons=reasons, stock=u.stock, has_stock_row=u.has_stock_row,
        velocity=u.velocity, days_left=u.days_left, days_best=u.days_best, days_worst=u.days_worst, excluded=u.excluded,
    )


def biggest_spike(m: SkuMetrics) -> tuple[date, int, float]:
    """급증일 중 판매가 가장 큰 날과 그 수량, 평소 가운데 값(중앙값)."""
    middle = float(statistics.median(m.daily_totals.values()))
    best_day = m.dates.spikes[0]
    for day in m.dates.spikes:
        if m.daily_totals[day] > m.daily_totals[best_day]:
            best_day = day
    return best_day, m.daily_totals[best_day], middle


def middle_reasons(u: UnitMetrics, m: SkuMetrics, run_info: RunInfo, rules: Rules) -> list[Reason]:
    """경보도 안전도 아닌 묶음에 붙이는 사유. 해당하는 것을 모두 적는다(빈 날, 급증일, 늦은 재고, 모르는 채널)."""
    reasons: list[Reason] = []
    window_days = rules.velocity_window_days
    if m.dates.gaps:
        reasons.append(make_reason(
            "SALES_GAP", u.unit, window_days=window_days, gap_count=len(m.dates.gaps),
            gap_dates=date_range_text(m.dates.gaps), good=u.days_best, bad=u.days_worst,
        ))
    if m.dates.spikes and u.days_worst < m.threshold:
        spike_day, spike_qty, middle = biggest_spike(m)
        reasons.append(make_reason(
            "SPIKE_CHANGES_VERDICT", u.unit, spike_date=spike_day, spike_qty=spike_qty,
            ratio=spike_qty / middle, good=u.days_best, bad=u.days_worst,
        ))
    if run_info.snapshot_age_days > rules.expected_snapshot_age_days:
        reasons.append(make_reason(
            "INVENTORY_LATE", u.unit, age=run_info.snapshot_age_days, as_of=run_info.inventory_as_of, days=u.days_left,
        ))
    if m.unknown_channels:
        reasons.append(make_reason(
            "UNKNOWN_CHANNEL", u.unit, channels=", ".join(m.unknown_channels),
            qty=m.unknown_qty, window_days=window_days,
        ))
    return reasons


def judge_unit(u: UnitMetrics, m: SkuMetrics, run_info: RunInfo, rules: Rules) -> UnitJudgment:
    """묶음 하나를 판정한다. 넉넉하게 잡아도 필요 일수에 못 미치면 경보, 빠듯하게 잡아도 필요 일수를 넘고 빠진 데이터가 없을 때만 안전, 그 사이는 판정 불가.

    기대어 쓰는 사실 세 가지. (1) 판매가 0인 묶음은 metrics에서 이미 뺐으므로 days_best가 None인
    묶음은 급증일에만 팔린 묶음뿐이다. (2) days_best가 None이 아니면 days_worst도 None이 아니다.
    기록 있는 모든 날의 판매 합은 쓰는 날의 판매 합보다 작을 수 없기 때문이다. (3) 셋째 갈래에서
    사유가 비는 일은 없다. 빈 날, 늦은 재고, 모르는 채널이 다 없으면 days_worst가 필요 일수보다
    짧아지는 이유는 급증일뿐이기 때문이다.
    """
    if u.days_best is None:
        reason = make_reason(
            "NO_RECENT_SALES", u.unit, window_days=rules.velocity_window_days, stock=u.stock,
            last_sale=u.last_sale_date or "없음",
        )
        return make_unit_judgment(u, STATUS_UNKNOWN, [reason])
    if u.days_best < m.threshold:
        reason = make_reason(
            "BELOW_THRESHOLD", u.unit, days=u.days_left, good=u.days_best, threshold=m.threshold,
            lead=m.lead_time_days, safety=m.safety_days,
        )
        return make_unit_judgment(u, STATUS_ALERT, [reason])
    has_gap = len(m.dates.gaps) > 0
    inventory_late = run_info.snapshot_age_days > rules.expected_snapshot_age_days
    has_unknown_channel = len(m.unknown_channels) > 0
    if not has_gap and not inventory_late and not has_unknown_channel and u.days_worst >= m.threshold:
        return make_unit_judgment(u, STATUS_SAFE, [])  # "안전"은 프로그램 전체에서 이 한 줄에서만 나온다
    return make_unit_judgment(u, STATUS_UNKNOWN, middle_reasons(u, m, run_info, rules))


def worst_status(results: list[UnitJudgment]) -> str:
    """묶음 결과 중 가장 나쁜 상태(STATUS_RANK 값이 가장 큰 것)."""
    worst = STATUS_SAFE
    for result in results:
        if STATUS_RANK[result.status] > STATUS_RANK[worst]:
            worst = result.status
    return worst


# ===== 메모 =====


def gap_notes(m: SkuMetrics, units: list[UnitMetrics]) -> list[str]:
    """빈 날 메모: 어느 날이 비었는지, 그 날을 0개로 쳐도 묶음별 넉넉하게 잡은 일수가 얼마인지."""
    gaps = m.dates.gaps
    where = date_range_text(gaps)
    if m.dates.recorded and gaps[-1] < m.dates.recorded[0]:
        where = f"{where}, 첫 판매 {fmt_date(m.dates.recorded[0])}"
    notes = [f"판매 기록 없는 날 {len(gaps)}일({where}), 있는 {len(m.dates.used)}일로 계산"]
    if units:
        per_unit = ", ".join(f"{u.unit} {format_value(u.days_best)}일" for u in units)
        notes.append(f"없는 날을 0개로 쳐도 {per_unit}")
    return notes


def spike_note(m: SkuMetrics, units: list[UnitMetrics]) -> str:
    """급증일 메모: 어느 날 몇 개였고 평소의 몇 배인지, 그날을 넣으면 묶음별 빠듯하게 잡은 일수가 얼마인지."""
    day, qty, middle = biggest_spike(m)
    head = f"급증일 {len(m.dates.spikes)}일 중 " if len(m.dates.spikes) > 1 else ""
    per_unit = ", ".join(f"{u.unit} {format_value(u.days_worst)}일" for u in units)
    return f"{head}{fmt_date(day)} 하루 {qty:,}개(평소 가운데 값 {middle:g}개의 {qty / middle:.1f}배)는 평균에서 뺌, 넣으면 {per_unit}"


def excluded_note(unit_name: str, status: str, qty: int, days: float | None) -> str:
    """계산 제외 재고 메모. 속도를 알면 들어왔을 때 며칠분인지도 적는다."""
    if days is None:
        return f"{status} {qty:,}개 별도({unit_name})"
    return f"{status} {qty:,}개가 들어오면 {unit_name} {days:.1f}일분"


def cleanup_notes(m: SkuMetrics, issues_for_sku: list[DataIssue]) -> list[str]:
    """리드타임 범위 밖, 재고 파일의 모르는 창고·상태 행, 계산 기간 안에서 정리한 중복·음수·합산 건수."""
    notes: list[str] = []
    counts: dict[str, int] = {}
    for issue in issues_for_sku:
        if issue.code == "lead_time_out_of_range":
            notes.append(f"리드타임 {issue.detail}은 다른 제품과 달라 값 확인 필요, 마스터 값 그대로 씀")
        elif issue.code == "unknown_warehouse":
            notes.append(f"재고 파일에 모르는 창고 이름이 있음({issue.detail}개), 계산에서 뺐으니 확인 필요")
        elif issue.code == "unknown_status":
            notes.append(f"재고 파일에 모르는 재고 상태가 있음({issue.detail}개), 계산에서 뺐으니 확인 필요")
        elif issue.code in CLEANUP_NOTE_LABELS and issue.issue_date is not None:
            if m.dates.start <= issue.issue_date <= m.dates.end:
                counts[issue.code] = counts.get(issue.code, 0) + 1
    for code, n in counts.items():
        notes.append(CLEANUP_NOTE_LABELS[code].format(n=n))
    return notes


def build_notes(m: SkuMetrics, units: list[UnitMetrics], issues_for_sku: list[DataIssue]) -> list[str]:
    """담당자가 볼 메모(마침표 없이). 빈 날, 급증일, 계산 제외 재고, 뺀 묶음, 정리 기록, 모르는 채널 순."""
    notes: list[str] = []
    has_window_sales = m.window_sales_total > 0  # 기간 안 판매가 없으면 빈 날·급증일 메모는 뜻이 없다
    if has_window_sales and m.dates.gaps:
        notes.extend(gap_notes(m, units))
    if has_window_sales and m.dates.spikes:
        notes.append(spike_note(m, units))
    for unit in units:
        for status, qty, days in unit.excluded:
            notes.append(excluded_note(unit.unit, status, qty, days))
    if has_window_sales:
        for unit_name, stock in m.skipped_units:
            if stock > 0:
                notes.append(f"{unit_name} 재고 {stock:,}개, 기간 안 판매 없음, 판정 제외")
    notes.extend(cleanup_notes(m, issues_for_sku))
    if m.unknown_channels:
        notes.append(f"묶음 표에 없는 채널({', '.join(m.unknown_channels)}) 판매 {m.unknown_qty}개는 어느 창고인지 몰라 계산에서 뺌")
    return notes


# ===== 제품 하나, 전체 =====


def make_judgment(m: SkuMetrics, status: str, reasons: list[Reason], results: list[UnitJudgment], notes: list[str]) -> Judgment:
    """묶음 판정 결과를 제품 하나의 최종 Judgment로 묶는다. 가장 짧은 묶음과 긴급 여부도 여기서 정한다."""
    days_left: float | None = None
    shortest_unit: str | None = None
    urgent = False
    for result in results:
        if result.days_left is None:
            continue
        if days_left is None or result.days_left < days_left:
            days_left = result.days_left
            shortest_unit = result.unit
        if result.status == STATUS_ALERT and result.days_left < m.lead_time_days:
            urgent = True
    return Judgment(
        sku=m.sku, name=m.name, status=status, reasons=reasons, notes=notes, days_left=days_left,
        shortest_unit=shortest_unit, stock_total=m.stock_total, threshold=m.threshold, lead_time_days=m.lead_time_days,
        safety_days=m.safety_days, urgent=urgent, units=results,
    )


def judge_sku(m: SkuMetrics, units: list[UnitMetrics], run_info: RunInfo, rules: Rules, issues_for_sku: list[DataIssue]) -> Judgment:
    """제품 하나를 판정한다. 제품 상태는 묶음 결과 중 가장 나쁜 것(경보 > 판정 불가 > 안전)이다."""
    notes = build_notes(m, units, issues_for_sku)
    blocking = find_blocking_reason(m, run_info, rules, issues_for_sku)
    if blocking is not None:
        return make_judgment(m, STATUS_UNKNOWN, [blocking], [], notes)
    if not units:  # 판정할 묶음이 없으면 안전이 아니라 판정 불가다
        return make_judgment(m, STATUS_UNKNOWN, [make_reason("ZERO_UNITS", None)], [], notes)
    results: list[UnitJudgment] = []
    for u in units:
        results.append(judge_unit(u, m, run_info, rules))
    status = worst_status(results)
    reasons: list[Reason] = []
    for result in results:
        if result.status == status:
            reasons.extend(result.reasons)
    return make_judgment(m, status, reasons, results, notes)


def judge_all(metrics_list: list[SkuMetrics], run_info: RunInfo, rules: Rules, issues: list[DataIssue]) -> list[Judgment]:
    """제품마다 창고 묶음들을 판정한다."""
    judgments: list[Judgment] = []
    for m in metrics_list:
        issues_for_sku = [issue for issue in issues if issue.sku == m.sku]
        judgment = judge_sku(m, m.warehouse_units, run_info, rules, issues_for_sku)
        codes = ",".join(reason.code for reason in judgment.reasons)
        logger.info("판정 sku=%s status=%s days_left=%s reasons=%s", judgment.sku, judgment.status, judgment.days_left, codes)
        judgments.append(judgment)
    return judgments
