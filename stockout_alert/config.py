"""판정 기준 숫자와 운영에 쓰는 상수를 한 곳에 모은 파일.

규칙 값(며칠 평균을 볼지, 급증일 배수 등)을 코드 여기저기 흩어 놓지 않고 이 파일 맨
위에만 둔다. 기준을 바꾸고 싶으면 이 파일만 보면 된다.

이 파일은 models.py만 import한다. judge.py, metrics.py, cleaner.py 같은 계산 코드는
이 파일의 상수를 직접 읽지 않고, 대신 build_rules()가 상수로 만들어 주는 Rules 값만
받는다. 그래야 테스트가 Rules만 바꿔 넣어도 결과를 바꿀 수 있다.
"""

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from stockout_alert.models import ConfigError, Rules

# 하루 판매량 구하기 위해, 판매로그에서 참고할 범위
# 최근 판매량 추세를 고려하면서도, 표본수가 많아야 안정적이므로, 그 타협안을 14일로 잡음 (리드타임의 절반 정도)
VELOCITY_WINDOW_DAYS = 14

# 판매 데이터는 이틀 전 까지 들어와있음
EXPECTED_SALES_LAG_DAYS = 2

# 하루판매량 평균보다 3배 이상 벗어나면, 판매 급증일로 판단
OUTLIER_MULTIPLIER = 3.0

# 재고상태 데이터가 기준일보다 하루 전으로 기록되므로, 그 이후부터 봇이 작동할 때 까지 팔린것을 고려하여, 하루치 여유를 둠
EXPECTED_SNAPSHOT_AGE_DAYS = 1

# 경과일이 2일을 넘으면(3일 이상) 전 제품 판정 불가. 2일까지는 계산하되 안전 판정만 보류
MAX_SNAPSHOT_AGE_DAYS = 2

# 계산에 넣는 재고 상태. 입고예정·이전중은 메모로만 보여준다.
SELLABLE_STATUSES = ("판매가능",)

# 안내문이 알려 준 판매 채널 세 가지
KNOWN_CHANNELS = ("amazon_us", "coupang", "own_mall")

# 리드타임이 너무 벗어난 제품에 대한 알림을 해주기 위함.
LEAD_TIME_RANGE = (21, 28)

# 판매채널, 창고 매핑. 판정은 언제나 창고별(각 창고의 재고 ÷ 그 창고로 나가는 채널의 판매 속도)
CHANNEL_TO_UNIT = {
    "amazon_us": "FBA_US",
    "coupang": "국내창고",
    "own_mall": "국내창고",
}

WAREHOUSE_TO_UNIT = {"FBA_US": "FBA_US", "국내창고": "국내창고"}  # 창고 이름 자체를 묶음 이름으로 그대로 씀

UNIT_LABELS = {
    "FBA_US": "아마존 창고(FBA_US)",
    "국내창고": "국내창고(쿠팡·자사몰)",
}  # 메시지에 묶음 이름을 보여줄 때 쓰는 사람이 읽을 이름

TRANSFER_STATUS = "이전중"  # 재고 상태 값. 다른 창고에서 옮겨오는 중
INBOUND_STATUS = "입고예정"  # 재고 상태 값. 새로 생산돼 들어올 예정

# ===== LLM =====
LLM_MODEL = "claude-sonnet-5"  # 환경변수 LLM_MODEL로 덮어쓸 수 있다. 3~5문장 요약에 충분하고 비용이 낮다
LLM_MAX_CALLS = 3  # 재요청 포함 한 실행에서 LLM을 부르는 최대 횟수
LLM_MAX_TOKENS = 1024  # 응답 최대 토큰. 3~5문장 요약이라 짧게 잡는다
LLM_TIMEOUT_SECONDS = 30.0
LLM_RETRY_WAIT_SECONDS = 2.0

# ===== 슬랙 =====
SLACK_TIMEOUT_SECONDS = 10.0
SLACK_MAX_ATTEMPTS = 3
SLACK_RETRY_WAIT_SECONDS = 2.0  # 매번 같은 시간만 기다린다. 실패할수록 더 오래 기다리는 방식(지수 백오프)은 쓰지 않는다

# ===== 운영 표식 (Cloud Monitoring 알림이 이 글자를 보고 울린다. 바꾸지 말 것) =====
EVENT_RUN_FINISHED = "RUN_FINISHED"
EVENT_RUN_FAILED = "RUN_FAILED"
EVENT_DATA_LATE = "DATA_LATE"
EVENT_DATA_STILL_LATE = "DATA_STILL_LATE"
EVENT_SUMMARY_FALLBACK_USED = "SUMMARY_FALLBACK_USED"
EVENT_LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
EVENT_LLM_CONFIG_ERROR = "LLM_CONFIG_ERROR"
EVENT_SLACK_SEND_FAILED = "SLACK_SEND_FAILED"
EVENT_SENT_BUT_NOT_RECORDED = "SENT_BUT_NOT_RECORDED"
EVENT_STATE_FILE_BROKEN = "STATE_FILE_BROKEN"

KST = timezone(timedelta(hours=9))  # 한국 표준시. Cloud Run은 UTC로 돌기 때문에 직접 계산한다


def build_rules() -> Rules:
    """위 상수로 판정 규칙 값(Rules)을 만든다.

    judge.py, metrics.py, cleaner.py는 이 함수가 돌려준 Rules만 받아서 쓰고, 이 파일의
    상수를 직접 읽지 않는다. 테스트에서 값 하나만 바꾸고 싶으면
    `dataclasses.replace(rules, velocity_window_days=7)`처럼 쓴다.
    """
    return Rules(
        velocity_window_days=VELOCITY_WINDOW_DAYS,
        expected_sales_lag_days=EXPECTED_SALES_LAG_DAYS,
        outlier_multiplier=OUTLIER_MULTIPLIER,
        expected_snapshot_age_days=EXPECTED_SNAPSHOT_AGE_DAYS,
        max_snapshot_age_days=MAX_SNAPSHOT_AGE_DAYS,
        sellable_statuses=SELLABLE_STATUSES,
        known_channels=KNOWN_CHANNELS,
        lead_time_range=LEAD_TIME_RANGE,
        channel_to_unit=dict(CHANNEL_TO_UNIT),
        warehouse_to_unit=dict(WAREHOUSE_TO_UNIT),
    )


@dataclass(frozen=True)
class Settings:
    """실행 한 번에 쓰는 설정값 전부. load_settings()가 CLI 인자와 환경변수로 만든다."""

    run_date: date
    data_dir: Path
    output_dir: Path
    state_dir: Path
    send: bool
    slack_webhook_url: str | None = field(repr=False)  # 객체를 로그에 찍어도 주소가 안 보이게
    anthropic_api_key: str | None = field(repr=False)
    llm_model: str
    log_level: str
    rules: Rules


def today_kst() -> date:
    """한국 날짜를 직접 계산한다. Cloud Run은 UTC로 돌기 때문에 OS 시간대에 기대지 않는다."""
    return datetime.now(KST).date()


def parse_date(name: str, raw: str) -> date:
    """YYYY-MM-DD 문자열을 날짜로 바꾼다. 형식이 틀리면 ConfigError."""
    try:
        return date.fromisoformat(raw)
    except ValueError as error:
        raise ConfigError(f"{name} 날짜 형식이 올바르지 않습니다: {raw!r} (형식: YYYY-MM-DD)") from error


def parse_choice(name: str, raw: str, choices: tuple[str, ...]) -> str:
    """raw가 choices 안에 있는지 확인한다. 아니면 ConfigError."""
    if raw not in choices:
        raise ConfigError(f"{name} 값이 올바르지 않습니다: {raw!r} (허용: {', '.join(choices)})")
    return raw


def load_settings(args: argparse.Namespace, env: dict[str, str]) -> Settings:
    """CLI 인자(--run-date, --send)와 환경변수로 Settings를 만든다. 빈 값은 없는 값으로 보고 기본값을 쓴다."""
    log_level = parse_choice("log_level", (env.get("LOG_LEVEL") or "INFO").upper(), ("DEBUG", "INFO", "WARNING", "ERROR"))
    run_date = parse_date("run_date", args.run_date) if args.run_date else today_kst()
    return Settings(
        run_date=run_date,
        data_dir=Path(env.get("DATA_DIR") or "data").resolve(),
        output_dir=Path(env.get("OUTPUT_DIR") or "output").resolve(),
        state_dir=Path(env.get("STATE_DIR") or "state").resolve(),
        send=args.send,
        slack_webhook_url=env.get("SLACK_WEBHOOK_URL") or None,
        anthropic_api_key=env.get("ANTHROPIC_API_KEY") or None,
        llm_model=env.get("LLM_MODEL") or LLM_MODEL,
        log_level=log_level,
        rules=build_rules(),
    )


def check_send_ready(settings: Settings) -> None:
    """--send인데 보낼 주소가 없으면 막는다.

    cli.run()은 드라이런 결과(미리보기 파일)를 저장한 뒤에 이 함수를 부른다. 그래야 설정이
    잘못됐어도 무엇을 보내려 했는지는 파일로 남는다.
    """
    if settings.send:
        url = settings.slack_webhook_url
        if not url or not url.startswith("https://"):
            raise ConfigError("SLACK_WEBHOOK_URL이 없거나 https://로 시작하지 않습니다")


def describe_settings(settings: Settings) -> str:
    """로그에 남길 설정 요약 한 줄. 비밀값은 '설정됨/없음'만 쓰고, 폴더는 절대 경로로 쓴다."""
    webhook = "설정됨" if settings.slack_webhook_url else "없음"
    api_key = "설정됨" if settings.anthropic_api_key else "없음"
    return (
        f"run_date={settings.run_date} send={settings.send} "
        f"llm_model={settings.llm_model} data_dir={settings.data_dir} "
        f"output_dir={settings.output_dir} state_dir={settings.state_dir} "
        f"SLACK_WEBHOOK_URL={webhook} ANTHROPIC_API_KEY={api_key}"
    )


def setup_logging(level: str) -> None:
    """루트 로거에 stderr 텍스트 출력을 하나 붙인다. 외부 라이브러리 로거는 WARNING으로 고정한다."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )
    for noisy_logger_name in ("urllib3", "httpx", "anthropic"):
        logging.getLogger(noisy_logger_name).setLevel(logging.WARNING)


def log_event(logger: logging.Logger, level: int, event: str, message: str) -> None:
    """운영 표식을 메시지 맨 앞에 붙여 남긴다. Cloud Logging 알림이 이 표식 글자를 보고 운다."""
    logger.log(level, "%s %s", event, message)
