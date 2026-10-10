"""명령줄 진입점.

main()이 설정을 읽고 run()을 부른다. run()은 데이터 읽기 → 정리 → 날짜 계산 → 재고 계산 → 판정 → 보고 만들기 →
발송 판단 → AI 요약 → 글 만들기 → 저장 순서로 돌고, 드라이런이면 화면에 보여 주고 끝낸다.
--send일 때만 실제로 보내고 발송 기록을 남긴다.
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime

from dotenv import load_dotenv

from stockout_alert import cleaner, judge, loader, metrics, notifier, report, summary
from stockout_alert.config import (
    EVENT_DATA_STILL_LATE,
    EVENT_LLM_UNAVAILABLE,
    EVENT_RUN_FAILED,
    EVENT_RUN_FINISHED,
    EVENT_SENT_BUT_NOT_RECORDED,
    EVENT_SUMMARY_FALLBACK_USED,
    KST,
    Settings,
    check_send_ready,
    describe_settings,
    load_settings,
    log_event,
    setup_logging,
)
from stockout_alert.models import (
    ConfigError,
    DataLoadError,
    Report,
    SentButNotRecorded,
    SlackSendError,
    SummaryResult,
)

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1  # 2는 argparse가 사용법 오류에 자동으로 쓴다


def build_arg_parser() -> argparse.ArgumentParser:
    """명령줄 옵션 두 개(--run-date, --send)를 정의한 ArgumentParser를 만든다. 폴더와 로그 수준은 환경변수로 받는다."""
    parser = argparse.ArgumentParser(
        prog="python -m stockout_alert",
        description="제품별 남은 판매 가능 일수를 계산해 슬랙으로 알리는 재고 소진 경보 프로그램.",
    )
    parser.add_argument(
        "--run-date",
        metavar="YYYY-MM-DD",
        default=None,
        help="기준일. 주지 않으면 오늘(KST). 데이터에서 계산하지 않고 항상 이 값을 쓴다",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="실제로 슬랙에 발송한다. 주지 않으면 드라이런(터미널·파일 출력만)",
    )
    return parser


def fix_console_encoding() -> None:
    """Windows 콘솔에서 한글이 깨지지 않도록 표준 출력·오류 스트림 인코딩을 맞춘다.

    Windows는 파이썬 표준 출력 인코딩을 시스템 코드 페이지(한국어 Windows는 cp949)로
    잡는다. Git Bash 터미널은 UTF-8을 기대하므로 그대로 두면 한글이 깨진다.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def build_today_report(settings: Settings) -> Report:
    """읽기 → 정리 → 날짜 계산 → 계산 → 판정 → 보고."""
    rules = settings.rules
    raw = loader.load_all(settings.data_dir)
    clean = cleaner.clean_data(raw, rules)
    run_info = judge.assess_run(settings.run_date, clean, rules)
    metrics_list = metrics.compute_all_metrics(clean, run_info, rules)
    issues = clean.issues + metrics.collect_channel_gap_issues(metrics_list)
    judgments = judge.judge_all(metrics_list, run_info, rules, issues)
    return report.build_report(judgments, run_info, issues)


def choose_llm_mode(settings: Settings) -> tuple[str, str]:
    """요약에 쓸 LLM을 고른다. 키가 있으면 진짜, 없으면 가짜. (모드, 터미널과 로그에 쓸 한 줄)을 돌려준다."""
    if settings.anthropic_api_key:
        return "anthropic", f"요약: 진짜 LLM({settings.llm_model})"
    if settings.send:  # 키 없이 실제 채널로 보낼 때는 가짜 문장을 내보내지 않는다
        return "unavailable", "요약: API 키 없이 실제 발송이라 가짜 요약 대신 정해진 문구 사용"
    return "mock", "요약: 키 없음 → 가짜 응답"


def create_llm_client(settings: Settings, the_report: Report, mode: str) -> summary.AnthropicLlmClient | summary.MockLlmClient:
    """모드에 맞는 LLM 클라이언트(진짜 또는 가짜)를 만든다."""
    if mode == "anthropic":
        return summary.AnthropicLlmClient(settings.anthropic_api_key, settings.llm_model)
    return summary.MockLlmClient(the_report)


def make_summary(settings: Settings, the_report: Report) -> tuple[SummaryResult, str]:
    """요약을 만든다. LLM을 쓸 수 없는 상황이면 부르지 않고 정해진 문구를 쓴다."""
    mode, line = choose_llm_mode(settings)
    logger.info(line)
    if mode == "unavailable":
        log_event(logger, logging.WARNING, EVENT_LLM_UNAVAILABLE, line)
        log_event(logger, logging.ERROR, EVENT_SUMMARY_FALLBACK_USED, line)
        return SummaryResult(summary.fallback_text(the_report, unavailable=True), "fallback", "none", 0, [line]), line
    client = create_llm_client(settings, the_report, mode)
    result = summary.summarize(the_report, summary.build_fact_sheet(the_report), client, mode)
    result.label = settings.llm_model if mode == "anthropic" else "키 없음"
    return result, line


def deliver(settings: Settings, the_report: Report, now: datetime) -> str:
    """발송 판단 → 요약 → 글 만들기 → 저장 → (드라이런이면 화면 출력) → 발송 → 기록."""
    run_info = the_report.run_info
    items = notifier.build_send_items(the_report)
    entries = notifier.load_sent_log(settings.state_dir)
    sender = notifier.WebhookSender(settings.slack_webhook_url or "")
    action = notifier.decide_send_action(settings.send, entries, settings.run_date, sender.name, run_info.report_kind, items)
    if action == "skip_already_sent":
        logger.info("오늘 같은 판정 목록을 이미 보냈으므로 건너뜀 (LLM 호출 없음)")
        return "skipped_already_sent"
    if action == "skip_still_late":
        log_event(logger, logging.ERROR, EVENT_DATA_STILL_LATE, "지연 보고를 이미 보냈고 데이터가 아직 늦어 보내지 않음")
        return "skipped_still_late"
    the_report.summary, llm_line = make_summary(settings, the_report)
    update_note = None
    if action == "send_update":
        todays = notifier.entries_for_day(entries, settings.run_date, sender.name)
        update_note = report.build_update_note([entry.get("kind", "") for entry in todays], run_info.report_kind)
    text = report.render_slack_text(the_report, update_note)
    text_path, json_path = report.save_run_outputs(settings.output_dir, settings.run_date, text, report.report_to_dict(the_report))
    logger.info("미리보기 저장: %s, %s", text_path, json_path)
    if not settings.send:
        for line in report.render_dry_run_banner(the_report, llm_line):
            print(line)
        print("-" * 40)
        print(text)
        return "dry_run"
    check_send_ready(settings)  # 미리보기를 저장한 뒤에 검사한다
    return send_and_record(settings, sender, text, entries, run_info.report_kind, items, now)


def send_and_record(settings: Settings, sender: notifier.WebhookSender, text: str, entries: list[dict], kind: str, items: list[list], now: datetime) -> str:
    """실제로 보내고, 성공한 뒤에만 기록한다. 기록 저장에 실패하면 SentButNotRecorded를 낸다."""
    attempts = sender.send(text)
    entry = notifier.make_entry(settings.run_date, kind, sender.name, now, items)
    try:
        notifier.append_entry_and_save(settings.state_dir, entries, entry)
    except OSError as error:
        log_event(logger, logging.ERROR, EVENT_SENT_BUT_NOT_RECORDED, f"발송 기록 저장 실패 ({type(error).__name__})")
        raise SentButNotRecorded(str(error)) from error
    logger.info("발송 완료 (%d번째 시도), 기록 저장", attempts)
    return "sent"


def run(settings: Settings, now: datetime) -> str:
    """정상 흐름을 실행하고 RUN_FINISHED에 쓸 status를 돌려준다."""
    logger.info(describe_settings(settings))
    if settings.slack_webhook_url and not settings.send:
        logger.info("웹훅이 있지만 --send가 없어 보내지 않음")
    the_report = build_today_report(settings)
    return deliver(settings, the_report, now)


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    """명령줄 진입점.

    env가 None일 때만 .env 파일을 읽는다(실제 실행). 테스트는 언제나 env를 직접 넘겨서
    실제 환경변수나 .env 파일에 기대지 않는다.
    """
    args = build_arg_parser().parse_args(argv)
    fix_console_encoding()
    if env is None:
        load_dotenv(override=False)  # 이미 있는 환경변수는 덮어쓰지 않는다
        env = dict(os.environ)
    setup_logging((env.get("LOG_LEVEL") or "INFO").upper())

    started = time.monotonic()
    exit_code, status = EXIT_FAILED, "failed_unexpected"
    try:
        settings = load_settings(args, env)
        status = run(settings, datetime.now(KST))
        exit_code = EXIT_OK
    except ConfigError as error:
        log_event(logger, logging.ERROR, EVENT_RUN_FAILED, f"설정 오류: {error}")
        status = "failed_config"
    except DataLoadError as error:
        log_event(logger, logging.ERROR, EVENT_RUN_FAILED, f"입력 데이터 오류: {error}")
        status = "failed_input"
    except SlackSendError as error:
        log_event(logger, logging.ERROR, EVENT_RUN_FAILED, f"슬랙 발송 실패: {error}")
        status = "failed_send"
    except SentButNotRecorded:
        log_event(logger, logging.ERROR, EVENT_RUN_FAILED, "발송 뒤 기록 실패: 다음 실행에서 중복 발송될 수 있음")
        status = "sent_not_recorded"
    except Exception:
        logger.exception("%s 예상 못 한 오류", EVENT_RUN_FAILED)
    finally:
        elapsed = time.monotonic() - started
        log_event(
            logger,
            logging.INFO,
            EVENT_RUN_FINISHED,
            f"exit_code={exit_code} status={status} elapsed={elapsed:.1f}s",
        )
    return exit_code
