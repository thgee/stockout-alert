"""슬랙 발송과 발송 기록(같은 알림을 하루에 두 번 보내지 않기)을 맡는 파일.

발송 기록은 {STATE_DIR}/sent_log.json 한 파일이다. "오늘 보낸 판정 목록을 적어 두고, 이번
목록과 같으면 안 보낸다." 발송이 성공한 뒤에만 기록하고, 임시 파일에 쓴 뒤 바꿔 끼운다.
"""

import json
import logging
import os
import time
from datetime import date, datetime
from pathlib import Path

import requests

from stockout_alert.config import (
    EVENT_SLACK_SEND_FAILED,
    EVENT_STATE_FILE_BROKEN,
    SLACK_MAX_ATTEMPTS,
    SLACK_RETRY_WAIT_SECONDS,
    SLACK_TIMEOUT_SECONDS,
    log_event,
)
from stockout_alert.models import KIND_DATA_LATE, Report, SlackSendError

logger = logging.getLogger(__name__)

SENT_LOG_NAME = "sent_log.json"


# ===== 발송 =====


def is_retryable_status(status: int) -> bool:
    """잠깐 뒤 다시 보내면 될 수 있는 응답 코드인지(429, 5xx)."""
    return status == 429 or 500 <= status <= 599


def mask_webhook_url(url: str) -> str:
    """웹훅 주소의 비밀 경로를 가린다. 로그에는 이 결과만 남긴다."""
    marker = "/services/"
    if marker in url:
        return url.split(marker)[0] + marker + "***"
    return "***"


class WebhookSender:
    """진짜 슬랙 발송. 일시 오류(시간 초과, 연결 실패, 429, 5xx)만 2초 쉬고 최대 3번 시도한다."""

    name = "webhook"

    def __init__(self, url: str, post=None, sleep=None):
        self.url = url
        self.post = post or requests.post
        self.sleep = sleep or time.sleep

    def send(self, text: str) -> int:
        """보내고 시도한 횟수를 돌려준다. 끝내 실패하면 SlackSendError를 낸다."""
        masked = mask_webhook_url(self.url)
        problem = ""
        for attempt in range(1, SLACK_MAX_ATTEMPTS + 1):
            try:
                response = self.post(self.url, json={"text": text}, timeout=SLACK_TIMEOUT_SECONDS)
            except requests.RequestException as error:  # 주소 자체는 로그에 남기지 않는다
                problem = f"연결 오류 {type(error).__name__}"
                retryable = isinstance(error, (requests.Timeout, requests.ConnectionError))
            else:
                if 200 <= response.status_code < 300:
                    logger.info("슬랙 발송 성공 (%d번째 시도, %s)", attempt, masked)
                    return attempt
                problem = f"HTTP {response.status_code}"
                retryable = is_retryable_status(response.status_code)
            if not retryable:
                break
            logger.warning("슬랙 발송 실패, 다시 시도 (%d/%d): %s", attempt, SLACK_MAX_ATTEMPTS, problem)
            if attempt < SLACK_MAX_ATTEMPTS:
                self.sleep(SLACK_RETRY_WAIT_SECONDS)
        log_event(logger, logging.ERROR, EVENT_SLACK_SEND_FAILED, f"{problem} ({masked})")
        raise SlackSendError(problem)


# ===== 발송 기록 =====


def build_send_items(report: Report) -> list[list]:
    """발송 기록에 적을 판정 목록. 남은 일수는 넣지 않는다(0.1일 변화로 [갱신]이 가지 않게)."""
    judgments = report.alerts + report.unknowns + report.safes
    items: list[list] = []
    for j in sorted(judgments, key=lambda j: j.sku):
        codes = sorted(set(reason.code for reason in j.reasons))
        items.append([j.sku, j.status, j.urgent, codes])  # JSON에서 읽으면 리스트라서 처음부터 리스트로 만든다
    return items


def load_sent_log(state_dir: Path) -> list[dict]:
    """발송 기록을 읽는다. 파일이 없으면 빈 목록. 깨졌으면 백업하고 빈 목록으로 계속한다."""
    path = state_dir / SENT_LOG_NAME
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data["entries"]
        if not isinstance(entries, list):
            raise ValueError("entries가 목록이 아님")
        return entries
    except (ValueError, KeyError, TypeError) as error:
        backup = path.with_name(f"{SENT_LOG_NAME}.broken-{datetime.now().strftime('%Y%m%d%H%M%S')}")
        path.replace(backup)
        log_event(logger, logging.ERROR, EVENT_STATE_FILE_BROKEN, f"발송 기록을 읽을 수 없어 {backup.name}으로 백업하고 빈 기록으로 계속 ({type(error).__name__})")
        return []


def entries_for_day(entries: list[dict], run_date: date, sender: str) -> list[dict]:
    """오늘, 같은 발송 방식으로 남긴 기록만 고른다."""
    result: list[dict] = []
    for entry in entries:
        if entry.get("run_date") == str(run_date) and entry.get("sender") == sender:
            result.append(entry)
    return result


def choose_action(todays: list[dict], kind: str, items: list[list]) -> str:
    """오늘 기록과 이번 판정 목록을 비교해 보낼지 정한다. 같으면 건너뛰고, 다르면 [갱신]으로 보낸다."""
    for entry in todays:
        if entry.get("kind") == kind and entry.get("items") == items:
            return "skip_already_sent"
    if kind == KIND_DATA_LATE and any(entry.get("kind") == KIND_DATA_LATE for entry in todays):
        return "skip_still_late"
    if todays:
        return "send_update"
    return "send_new"


def decide_send_action(send: bool, entries: list[dict], run_date: date, sender: str, kind: str, items: list[list]) -> str:
    """발송 판단. 드라이런이면 기록을 읽기만 하고 "실제 발송이었다면" 결과를 로그에 남긴다."""
    todays = entries_for_day(entries, run_date, sender)
    action = choose_action(todays, kind, items)
    if not send:
        logger.info("드라이런. 실제 발송이었다면: %s (오늘 기록 %d건)", action, len(todays))
        return "dry_run"
    logger.info("발송 판단: %s (오늘 기록 %d건)", action, len(todays))
    return action


def make_entry(run_date: date, kind: str, sender: str, sent_at: datetime, items: list[list]) -> dict:
    """기록 한 건."""
    return {"run_date": str(run_date), "kind": kind, "sender": sender, "sent_at": sent_at.isoformat(), "items": items}


def append_entry_and_save(state_dir: Path, entries: list[dict], entry: dict) -> None:
    """기록을 하나 더해 저장한다. 임시 파일에 쓴 뒤 os.replace로 바꿔 끼워 반쯤 쓰인 파일이 남지 않게 한다."""
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / SENT_LOG_NAME
    temp_path = path.with_suffix(".json.tmp")
    data = {"version": 1, "entries": entries + [entry]}
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, path)
