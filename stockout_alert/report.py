"""판정 결과를 슬랙 메시지 글과 드라이런 화면, 출력 파일로 만드는 파일.

형식은 단순 텍스트다. 꾸밈은 *굵게*와 줄 앞 >만 쓴다. 데이터에서 온 글(제품명 등)은
escape_slack()을 거친다. 경보가 0건인 날에도 보낸다. 판정 불가가 있으면 반드시 적는다.
"""

import dataclasses
import json
import math
from datetime import date, timedelta
from pathlib import Path

from stockout_alert.config import UNIT_LABELS
from stockout_alert.models import (
    KIND_DATA_LATE,
    KIND_NORMAL,
    STATUS_ALERT,
    STATUS_SAFE,
    STATUS_UNKNOWN,
    DataIssue,
    Judgment,
    Report,
    RunInfo,
    UnitJudgment,
)

# 데이터가 늦은 날 머리말에 붙이는 안내. 두 번째 실행 시각(운영설계메모)과 맞춘다.
RECHECK_NOTICE = "오늘 11시에 다시 확인합니다. 11시에도 들어오지 않으면 이 보고가 오늘의 마지막입니다"

# 묶음 판정 보류 사유 코드 -> 창고별 줄에 붙이는 짧은 설명
HOLD_LABELS = {
    "SALES_GAP": "판매 기록이 빠진 날 있음",
    "SPIKE_CHANGES_VERDICT": "급증일에 따라 결론이 갈림",
    "INVENTORY_LATE": "재고 스냅샷 늦음",
    "UNKNOWN_CHANNEL": "모르는 채널 있음",
    "NO_RECENT_SALES": "급증일에만 판매",
}

# 맨 아래 "데이터 정리" 한 줄에 쓰는 정리 기록 문구. {n}은 건수, {skus}는 제품 목록
CLEANING_LABELS = {
    "channel_fixed": "채널 표기 {n}행 통일",
    "duplicate_removed": "중복 {n}행 제거({skus})",
    "negative_removed": "음수 {n}행 제외",
    "key_conflict_summed": "같은 날·채널 {n}행 합산({skus})",
    "inventory_duplicate_removed": "재고 중복 {n}행 제거({skus})",
    "invalid_stock": "재고 음수 {n}행, 제품 판정 불가({skus})",
    "unknown_warehouse": "모르는 창고 {n}행({skus})",
    "unknown_status": "모르는 재고 상태 {n}행({skus})",
    "unknown_channel": "모르는 채널 {n}행({skus})",
    "unknown_sku": "마스터에 없는 제품 {n}행",
    "snapshot_date_mixed": "재고 스냅샷 날짜 섞임 {n}행",
    "channel_rows_missing": "채널 행이 빠진 날 {n}건({skus})",
}


# ===== 글자 다듬기 =====


def format_days(value: float | None) -> str:
    """일수를 "7.3"처럼 쓴다. 모르면 "-"."""
    return "-" if value is None else f"{value:.1f}"


def format_qty(value: int | None) -> str:
    """수량을 "1,350"처럼 천 단위 쉼표로 쓴다. 모르면 "-"."""
    return "-" if value is None else f"{value:,}"


def format_month_day(value: date) -> str:
    """날짜를 "10/13"처럼 쓴다."""
    return f"{value.month}/{value.day}"


def escape_slack(text: str) -> str:
    """슬랙이 특별하게 읽는 &, <, >를 글자 그대로 보이게 바꾼다."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def stockout_text(run_date: date, days: float | None) -> str:
    """"지금 품절" 또는 "품절 예상 10/13". 기준일에 남은 일수(내림)를 더한 날이다."""
    if days is None:
        return "품절 예상일 모름"
    if days == 0.0:
        return "지금 품절"
    return "품절 예상 " + format_month_day(run_date + timedelta(days=math.floor(days)))


# ===== 정렬과 보고 묶기 =====


def sort_alerts(alerts: list[Judgment]) -> list[Judgment]:
    """급한 순. 가장 짧은 창고의 남은 일수, 같으면 전체 판매가능 재고가 적은 쪽, 같으면 SKU."""
    return sorted(alerts, key=lambda j: (j.days_left, j.stock_total, j.sku))


def sort_unknowns(unknowns: list[Judgment]) -> list[Judgment]:
    """SKU 순."""
    return sorted(unknowns, key=lambda j: j.sku)


def sort_safes(safes: list[Judgment]) -> list[Judgment]:
    """여유(남은 일수 - 필요 일수)가 적은 순, 같으면 SKU."""
    return sorted(safes, key=lambda j: (j.days_left - j.threshold, j.sku))


def build_report(judgments: list[Judgment], run_info: RunInfo, issues: list[DataIssue]) -> Report:
    """판정 목록을 상태별로 나누고 정렬해 Report 하나로 묶는다. 모르는 상태가 있으면 오류를 낸다."""
    alerts: list[Judgment] = []
    unknowns: list[Judgment] = []
    safes: list[Judgment] = []
    for j in judgments:
        if j.status == STATUS_ALERT:
            alerts.append(j)
        elif j.status == STATUS_UNKNOWN:
            unknowns.append(j)
        elif j.status == STATUS_SAFE:
            safes.append(j)
        else:
            raise ValueError(f"모르는 판정 상태: {j.sku} {j.status!r}")
    return Report(
        run_info=run_info, alerts=sort_alerts(alerts), unknowns=sort_unknowns(unknowns),
        safes=sort_safes(safes), data_issues=issues,
    )


# ===== 경보 한 건의 원인과 할 일 =====


def labels_of(units: list[UnitJudgment]) -> str:
    """묶음 이름을 쉼표로 잇는다. 짧은 이름을 쓰고, 채널 설명은 머리말에 한 번만 적는다."""
    return ", ".join(u.unit for u in units)


def describe_alert(j: Judgment) -> tuple[str, list[str]]:
    """(원인 한 구절, 할 일 목록). 원인은 처음 맞는 규칙 하나, 할 일은 해당하는 것을 모두 모은다."""
    alert_units = [u for u in j.units if u.status == STATUS_ALERT]
    all_alert = len(alert_units) == len(j.units)
    empty = [u for u in alert_units if u.stock == 0]
    if empty:
        cause = f"{labels_of(empty)} 지금 품절"
    elif all_alert:
        cause = "모든 창고 필요 일수 미달"
    else:
        cause = f"{labels_of(alert_units)}만 필요 일수 미달"
    todos: list[str] = []
    for u in alert_units:
        for status, qty, _days in u.excluded:
            if status == "이전중":
                todos.append(f"이전중 {format_qty(qty)}개 입고 재촉")
            elif status == "입고예정":
                todos.append(f"입고예정 {format_qty(qty)}개 도착일 확인")
        if not u.has_stock_row:
            todos.append(f"{u.unit} 재고 행 확인")
    todos.append("생산 시작" if all_alert else f"{labels_of(alert_units)}용 생산")
    return cause, todos


# ===== 메시지 줄 만들기 =====


def render_late_lines(info: RunInfo) -> list[str]:
    """판매가 늦거나 재고 스냅샷이 오래된 날 제목 아래 붙이는 줄. 보통 날에는 빈 목록."""
    lines: list[str] = []
    if info.sales_late_days > 0:
        lines.append(
            f"*[데이터 지연]* 판매 데이터 {info.sales_late_days}일 지연(완전한 마지막 날 {format_month_day(info.complete_last_date)}, "
            f"기대 {format_month_day(info.window_end)}). {RECHECK_NOTICE}"
        )
    for warning in info.header_warnings:
        if warning.startswith("재고 스냅샷"):
            lines.append(warning)
    if lines:
        lines.append("데이터가 늦은 날에는 '안전'이라고 판정하지 않습니다")
    return lines


def render_header(report: Report) -> list[str]:
    """제목, (데이터 지연 머리말), 판정 방식, 상태별 개수."""
    info = report.run_info
    last_sales_day = min(info.window_end, info.complete_last_date)
    lines = [f"*[재고 소진 경보] 기준일 {info.run_date}* (재고 {format_month_day(info.inventory_as_of)} 기준, 판매 {format_month_day(last_sales_day)}까지)"]
    lines.extend(render_late_lines(info))
    window_days = (info.window_end - info.window_start).days + 1
    warehouses = ", ".join(UNIT_LABELS.values())
    lines.append(f"판정: 창고별 (가장 짧은 창고 기준: {warehouses}) / 하루 판매량: {format_month_day(info.window_start)}~{format_month_day(info.window_end)} {window_days}일 평균")
    lines.append(f"*경보 {len(report.alerts)}건 / 판정 불가 {len(report.unknowns)}건 / 안전 {len(report.safes)}건*")
    return lines


def render_summary_block(report: Report) -> list[str]:
    """요약 제목과 본문. 요약이 없으면(LLM을 안 쓴 날) 정해진 문구를 쓴다."""
    summary = report.summary
    if summary is None:
        counts = f"오늘 경보 {len(report.alerts)}건, 판정 불가 {len(report.unknowns)}건, 안전 {len(report.safes)}건입니다. 아래 목록을 직접 확인해 주세요."
        return ["*요약* (AI 요약 사용 안 함)", f"> AI 요약을 쓰지 않았습니다(LLM 설정 없음). {counts}"]
    if summary.provider == "none":
        title = "*요약* (AI 요약 사용 안 함)"
    elif summary.source == "fallback":
        title = "*요약* (AI 요약 검증 실패로 정해진 문구)"
    elif summary.provider == "mock":
        title = f"*AI 요약* (가짜 응답: {summary.label})" if summary.label else "*AI 요약* (가짜 응답)"
    else:
        title = f"*AI 요약* (Claude {summary.label}, 검증 통과)" if summary.label else "*AI 요약* (Claude, 검증 통과)"
    return [title, "> " + escape_slack(summary.text.strip())]


def build_update_note(previous_kinds: list[str], kind: str) -> str | None:
    """오늘 앞서 보낸 것이 있을 때 맨 위에 붙이는 [갱신] 줄. 없으면 None."""
    if not previous_kinds:
        return None
    if KIND_DATA_LATE in previous_kinds and kind == KIND_NORMAL:
        return "[갱신] 앞서 보낸 데이터 지연 보고 뒤 데이터가 들어와 다시 판정한 결과입니다"
    return "[갱신] 앞서 보낸 보고 뒤 입력 데이터가 바뀌어 다시 판정한 결과입니다"


def render_unit_segment(u: UnitJudgment) -> str:
    """창고별 줄의 묶음 하나: "FBA_US 0개 0.0일 (이전중 450개 별도)"."""
    parts: list[str] = []
    for status, qty, _days in u.excluded:
        parts.append(f"{status} {format_qty(qty)}개 별도")
    if not u.has_stock_row:
        parts.append("재고 행 없음, 0으로 계산")
    if u.status == STATUS_UNKNOWN and u.reasons:
        parts.append("판정 보류: " + HOLD_LABELS.get(u.reasons[0].code, u.reasons[0].code))
    text = f"{u.unit} {format_qty(u.stock)}개 {format_days(u.days_left)}일"
    return text if not parts else f"{text} ({', '.join(parts)})"


def group_alerts(alerts: list[Judgment]) -> list[tuple[str, list[Judgment]]]:
    """경보를 지금 품절 / 곧 품절(리드타임 안) / 필요 일수 미달 세 묶음으로 나눈다. 빈 묶음은 뺀다."""
    now = [j for j in alerts if j.days_left == 0.0]
    soon = [j for j in alerts if j.days_left != 0.0 and j.urgent]
    rest = [j for j in alerts if j.days_left != 0.0 and not j.urgent]
    groups = [
        (f"*지금 품절 {len(now)}건*", now),
        (f"*곧 품절 {len(soon)}건* (리드타임 안에 바닥남)", soon),
        (f"*필요 일수 미달 {len(rest)}건*", rest),
    ]
    return [(title, group) for title, group in groups if group]


def stockout_cell(run_date: date, days: float | None) -> str:
    """표의 품절 칸(5칸). 0.0이면 "지금 ", 아니면 "10/13". 한글 두 글자는 고정폭에서 네 칸이라 공백 하나로 맞춘다."""
    if days is None:
        return "-    "
    if days == 0.0:
        return "지금 "
    return f"{format_month_day(run_date + timedelta(days=math.floor(days))):<5}"


def render_overview_rows(ordered: list[Judgment], run_date: date) -> list[str]:
    """한눈에 보는 표의 줄들. 한글은 마지막 칸(창고)에만 둬서 고정폭 글꼴에서 칸이 밀리지 않게 한다."""
    rows: list[str] = []
    for number, j in enumerate(ordered, start=1):
        days = f"{format_days(j.days_left):>4}/{j.threshold:<2}"
        rows.append(f"{number:>2}  {j.sku:<7} {days}  {stockout_cell(run_date, j.days_left)}  {j.shortest_unit}")
    return rows


def render_alert_entry(number: int, j: Judgment, run_date: date) -> list[str]:
    """경보 한 건의 세 줄: 제목(제품, 가장 짧은 창고, 품절 예상), 할 일, 창고별 숫자와 메모(인용 표시로 눈에 덜 띄게)."""
    head = f"*{number}. {escape_slack(j.name)} ({j.sku})*  {j.shortest_unit} {format_days(j.days_left)}일 / 필요 {j.threshold}일"
    if j.days_left is not None and j.days_left > 0:
        head = f"{head} · {stockout_text(run_date, j.days_left)}"
    _cause, todos = describe_alert(j)
    details = [render_unit_segment(u) for u in j.units] + [escape_slack(note) for note in j.notes]
    return [head, f"할 일: {', '.join(todos)}", "> " + " · ".join(details)]


def render_alert_lines(report: Report) -> list[str]:
    """경보 묶음: 제목, 한눈에 보는 표(코드 블록), 급한 정도별 묶음 안의 경보들. 경보 사이는 빈 줄."""
    if not report.alerts:
        return ["*경보 0건*"]
    run_date = report.run_info.run_date
    groups = group_alerts(report.alerts)
    ordered = [j for _title, group in groups for j in group]
    lines = [f"*경보 {len(ordered)}건* (급한 순. 표: 번호, SKU, 남은 일수/필요 일수, 품절 예상, 가장 짧은 창고)", "```"]
    lines.extend(render_overview_rows(ordered, run_date))
    lines.append("```")
    number = 0
    for title, group in groups:
        if lines[-1] != "":  # 앞 경보 뒤에 이미 빈 줄이 있으면 또 넣지 않는다
            lines.append("")
        lines.append(title)
        for j in group:
            number += 1
            lines.extend(render_alert_entry(number, j, run_date))
            lines.append("")
    lines.pop()  # 마지막 빈 줄은 뺀다. 묶음 사이 빈 줄은 render_slack_text가 넣는다
    return lines


def render_unknown_lines(report: Report) -> list[str]:
    """판정 불가 묶음: 제목 줄과 제품마다 한 줄(사유 문구 전부, 메모)."""
    lines = [f"*판정 불가 {len(report.unknowns)}건*"]
    for j in report.unknowns:
        text = " ".join(reason.text for reason in j.reasons)
        if j.notes:
            text = f"{text} 메모: {'. '.join(j.notes)}"
        lines.append(f"- {j.sku} {escape_slack(j.name)}: {escape_slack(text)}")
    return lines


def render_safe_lines(report: Report) -> list[str]:
    """안전 묶음: 제목 줄과 제품마다 한 줄(남은 일수, 필요 일수, 여유)."""
    title = f"*안전 {len(report.safes)}건*"
    lines = [f"{title} (여유가 적은 순)" if report.safes else title]
    for j in report.safes:
        lines.append(f"- {j.sku} {escape_slack(j.name)}: {format_days(j.days_left)}일 / 필요 {j.threshold}일 (여유 {format_days(j.days_left - j.threshold)}일)")
    return lines


def render_cleaning_line(report: Report) -> str:
    """맨 아래 한 줄: 데이터 정리 건수, 마스터 경고, 상세 파일 이름."""
    counts: dict[str, int] = {}
    skus_by_code: dict[str, list[str]] = {}
    master_warnings: list[str] = []
    for issue in report.data_issues:
        if issue.code == "lead_time_out_of_range":
            master_warnings.append(f"{issue.sku} 리드타임 {issue.detail}")
        elif issue.code in CLEANING_LABELS:
            counts[issue.code] = counts.get(issue.code, 0) + 1
            skus = skus_by_code.setdefault(issue.code, [])
            if issue.sku and issue.sku not in skus:
                skus.append(issue.sku)
    parts: list[str] = []
    for code, n in counts.items():
        parts.append(CLEANING_LABELS[code].format(n=n, skus=", ".join(sorted(skus_by_code[code]))))
    text = "_데이터 정리: " + (", ".join(parts) if parts else "없음")
    if master_warnings:
        text = f"{text}. 마스터 경고: {', '.join(master_warnings)}"
    return f"{text}. 상세: report_{report.run_info.run_date}.json_"


def render_slack_text(report: Report, update_note: str | None) -> str:
    """메시지 전체. 제목 → (갱신 줄) → 머리말 → 요약 → 경보 → 판정 불가 → 안전 → 데이터 정리 한 줄."""
    header = render_header(report)
    lines = [header[0]]
    if update_note:
        lines.append(update_note)
    lines.extend(header[1:])
    lines.append("")
    lines.extend(render_summary_block(report))
    lines.append("")
    lines.extend(render_alert_lines(report))
    lines.append("")
    lines.extend(render_unknown_lines(report))
    lines.append("")
    lines.extend(render_safe_lines(report))
    lines.append("")
    lines.append(render_cleaning_line(report))
    return "\n".join(lines)


def render_dry_run_banner(report: Report, llm_line: str) -> list[str]:
    """드라이런 화면 맨 위 두 줄: 상태별 개수와 요약 방식."""
    first = f"드라이런(보내지 않음): 경보 {len(report.alerts)} / 판정 불가 {len(report.unknowns)} / 안전 {len(report.safes)}"
    return [first, llm_line]


# ===== 출력 파일 =====


def report_to_dict(report: Report) -> dict:
    """보고 전체를 JSON으로 저장할 수 있는 dict로 바꾼다. 상태별 개수를 더한다."""
    data = dataclasses.asdict(report)
    data["counts"] = {STATUS_ALERT: len(report.alerts), STATUS_UNKNOWN: len(report.unknowns), STATUS_SAFE: len(report.safes)}
    return data


def save_run_outputs(output_dir: Path, run_date: date, text: str, data: dict) -> tuple[Path, Path]:
    """보낼 글과 상세 JSON을 output 폴더에 저장한다. 폴더가 없으면 만든다."""
    output_dir.mkdir(parents=True, exist_ok=True)
    text_path = output_dir / f"slack_message_{run_date}.txt"
    json_path = output_dir / f"report_{run_date}.json"
    text_path.write_text(text, encoding="utf-8")
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return text_path, json_path
