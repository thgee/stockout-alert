"""테스트 전체가 같이 쓰는 준비 도구."""

from datetime import date
from pathlib import Path

import pytest

from stockout_alert import cleaner, judge, loader, metrics, report
from stockout_alert.config import build_rules
from stockout_alert.models import RunInfo


@pytest.fixture
def real_report():
    """실제 과제 데이터로 Report를 만드는 도구. 요약 검증 테스트가 쓴다."""

    def _make(run_date: date = date(2026, 10, 6)):
        rules = build_rules()
        clean = cleaner.clean_data(loader.load_all(Path("data")), rules)
        run_info = judge.assess_run(run_date, clean, rules)
        metrics_list = metrics.compute_all_metrics(clean, run_info, rules)
        issues = clean.issues + metrics.collect_channel_gap_issues(metrics_list)
        judgments = judge.judge_all(metrics_list, run_info, rules, issues)
        return report.build_report(judgments, run_info, issues)

    return _make


@pytest.fixture
def rules():
    """기본 판정 규칙(Rules)을 돌려주는 도구.

    값 하나만 바꾸고 싶으면 `dataclasses.replace(rules(), velocity_window_days=7)`처럼 쓴다.
    """

    def _rules():
        return build_rules()

    return _rules


@pytest.fixture
def make_run_info():
    """RunInfo를 손으로 만드는 도구. assess_run() 없이도 metrics.py를 시험할 수 있다.

    기본값은 이번 과제 데이터(기준일 2026-10-06, 재고 2026-10-05, 판매 2026-10-04까지)와 같다.
    """

    def _make(
        run_date=date(2026, 10, 6),
        window_start=date(2026, 9, 21),
        window_end=date(2026, 10, 4),
        complete_last_date=date(2026, 10, 4),
        snapshot_age_days=1,
        inventory_as_of=date(2026, 10, 5),
    ):
        return RunInfo(
            run_date=run_date, window_start=window_start, window_end=window_end,
            channel_last_dates={}, complete_last_date=complete_last_date,
            sales_late_days=max(0, (window_end - complete_last_date).days),
            inventory_as_of=inventory_as_of, snapshot_age_days=snapshot_age_days,
            report_kind="normal", header_warnings=[],
        )

    return _make
