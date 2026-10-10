"""AI 요약: 사실 표를 만들고, LLM(진짜 또는 가짜)에게 요약을 시키고, 돌아온 글을 검증하는 파일.

LLM이 숫자를 계산하거나 지어내지 못하게 사실 표에는 남은 일수와 필요 일수만 넣는다. 돌아온 요약은
사실 표에 있는 이름·숫자·날짜만 썼는지 검사한다. 검사에 실패하면 문제를 알려 주고 다시 묻고,
상한(3번)에 닿으면 정해진 문구로 대신한다.
"""

import logging
import math
import re
import time
from collections.abc import Callable
from datetime import date

import anthropic

from stockout_alert.config import (
    EVENT_LLM_CONFIG_ERROR,
    EVENT_SUMMARY_FALLBACK_USED,
    LLM_MAX_CALLS,
    LLM_MAX_TOKENS,
    LLM_RETRY_WAIT_SECONDS,
    LLM_TIMEOUT_SECONDS,
    log_event,
)
from stockout_alert.models import CheckResult, FactSheet, LlmCallError, LlmConfigError, Report, SummaryResult
from stockout_alert.report import describe_alert, format_days

logger = logging.getLogger(__name__)

MAX_RETRIES = 2  # 검증 실패나 일시 오류 뒤 다시 묻는 횟수. 호출 상한은 LLM_MAX_CALLS가 따로 막는다
MIN_SENTENCES = 3
MAX_SENTENCES = 5
NUMBER_TOLERANCE = 0.0001
REQUIRED_ALERT_COUNT = 3  # 급한 순 상위 몇 개를 이름과 일수로 꼭 써야 하는지

SENTENCE_SPLIT = re.compile(r"[.!?](?:\s+|$)")
BRACKET_NAME = re.compile(r"「([^」]+)」")
SKU_CODE = re.compile(r"SKU-\d+")
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
KOREAN_DATE = re.compile(r"(\d{1,2})월\s*(\d{1,2})일")
SLASH_DATE = re.compile(r"(\d{1,2})/(\d{1,2})")
NUMBER = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")
UNKNOWN_WORD = re.compile(r"판정\s?불가")

# 사유 코드의 짧은 이름. 사실 표의 판정 불가 이유와 가짜 요약에 쓴다
REASON_LABELS = {
    "ROW_PARSE_ERROR": "읽을 수 없는 행",
    "NO_INVENTORY": "재고 파일에 없음",
    "INVENTORY_STALE": "재고 스냅샷 오래됨",
    "NO_SALES_HISTORY": "판매 기록 없음",
    "NO_RECENT_SALES": "최근 판매 없음",
    "ZERO_UNITS": "판정할 묶음 없음",
    "BELOW_THRESHOLD": "필요 일수 미달",
    "SALES_GAP": "판매 기록 빠진 날",
    "SPIKE_CHANGES_VERDICT": "급증일로 결론 갈림",
    "INVENTORY_LATE": "재고 스냅샷 늦음",
    "UNKNOWN_CHANNEL": "모르는 채널",
}

SYSTEM_PROMPT = """너는 화장품 회사 재고 담당자에게 아침 재고 보고를 요약해 주는 역할을 한다.
아래 규칙을 반드시 지킨다.
1. 한국어로 3~5문장을 쓴다. 각 문장은 마침표로 끝낸다.
2. 숫자는 [사실 표]에 적힌 값을 글자 그대로 쓴다. 더하기, 빼기, 반올림, 단위 바꾸기를 하지 않는다.
3. 계산한 값(배수, 비율, 부족 일수)을 쓰지 않는다.
4. 날짜를 새로 계산하지 않는다. "며칠에 품절" 같은 날짜를 만들지 않는다.
5. 제품명은 [사실 표]의 이름을 글자 그대로 「」 안에 넣어 쓴다. 예: 「제품 이름」
6. 경보 제품은 급한 순으로 적어도 3개는 「이름」(남은 N.N일) 형식으로 쓴다. 0.0일도 숫자로 쓴다. 나머지는 개수로 써도 된다.
7. 판정 불가 제품이 있으면 "판정 불가"라는 말과 그 이유를 쓴다. 판정 불가 제품을 안전하다고 말하지 않는다.
8. [사실 표]에 없는 원인 추측이나 권장 생산량을 쓰지 않는다.
9. 요약 문장만 출력한다. 제목, 목록 기호, 마크다운 기호를 쓰지 않는다."""


# ===== LLM 클라이언트 =====


class AnthropicLlmClient:
    """실제 LLM. complete()를 부를 때마다 calls가 1씩 증가"""

    def __init__(self, api_key: str, model: str):
        self.model = model
        self.calls = 0
        # max_retries=0: SDK가 몰래 다시 보내지 않게 한다. 다시 보내기는 summarize()가 세고 상한으로 막는다.
        self._client = anthropic.Anthropic(api_key=api_key, timeout=LLM_TIMEOUT_SECONDS, max_retries=0)

    def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        """요약을 한 번 요청한다. 일시 오류는 LlmCallError, 키·모델 오류는 LlmConfigError로 바꿔 올린다."""
        self.calls += 1
        try:
            response = self._client.messages.create(
                model=self.model, max_tokens=LLM_MAX_TOKENS, system=system, messages=messages,
                # 요약은 생각이 필요 없는 단순 작업이라 생각 기능을 끈다. 켜 두면 생각에 쓴 토큰이
                # max_tokens 안에서 같이 소모되어 요약 본문이 잘릴 수 있다.
                thinking={"type": "disabled"},
            )
        except anthropic.APIStatusError as error:
            if error.status_code == 429 or error.status_code >= 500:
                raise LlmCallError(f"LLM 일시 오류 (HTTP {error.status_code})") from error
            raise LlmConfigError(f"LLM이 요청을 거부함 (HTTP {error.status_code}). 키나 모델 이름을 확인") from error
        except anthropic.APIConnectionError as error:  # 응답 시간 초과도 여기로 온다
            raise LlmCallError(f"LLM 연결 실패 ({type(error).__name__})") from error
        logger.info("LLM 토큰 사용: 입력 %d, 출력 %d, stop_reason=%s", response.usage.input_tokens, response.usage.output_tokens, response.stop_reason)
        if response.stop_reason == "max_tokens":
            logger.warning("LLM 응답이 max_tokens에서 잘렸을 수 있음")
        texts = [block.text for block in response.content if block.type == "text"]
        return "".join(texts).strip()


class MockLlmClient:
    """Mock LLM. 받은 메시지는 보지 않고 report에서 문장을 만든다. 테스트와 키 없는 드라이런에 쓴다."""

    def __init__(self, report: Report):
        self.report = report
        self.calls = 0

    def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        """판정 결과로 만든, 검증을 통과하는 요약을 돌려준다."""
        self.calls += 1
        return mock_summary(self.report, wrong=False)


def mock_summary(report: Report, wrong: bool) -> str:
    """가짜 요약 문장 4~5개. wrong이면 마지막 문장을 없는 제품과 없는 숫자로 바꾼다."""
    a, u, s = len(report.alerts), len(report.unknowns), len(report.safes)
    sentences = [f"(테스트용 가짜 요약) {report.run_info.run_date} 기준 경보 {a}건, 판정 불가 {u}건, 안전 {s}건입니다."]
    top = report.alerts[:REQUIRED_ALERT_COUNT]
    if top:
        sentences.append("가장 급한 제품은 " + ", ".join(f"「{j.name}」(남은 {format_days(j.days_left)}일)" for j in top) + "입니다.")
    else:
        sentences.append("필요 일수보다 짧은 제품은 없습니다.")
    rest = a - len(top)
    sentences.append(f"나머지 경보 {rest}건은 아래 목록에 있습니다." if rest > 0 else "다른 경보 제품은 없습니다.")
    if u > 0:
        parts = ", ".join(f"「{j.name}」({REASON_LABELS.get(j.reasons[0].code, j.reasons[0].code)})" for j in report.unknowns)
        sentences.append(f"판정 불가 {u}건은 {parts} 때문에 담당자 확인이 필요합니다.")
    last = "「없는 제품 100ml」은 남은 일수 999.9일입니다." if wrong else "아래 목록에서 제품별 원인과 할 일을 확인해 주세요."
    sentences.append(last)
    return " ".join(sentences)


# ===== 사실 표 =====


def format_number(value: float) -> str:
    """숫자를 "24.7"처럼 소수 1자리로 쓴다."""
    return f"{value:.1f}"


def round_half_up(value: float) -> int:
    """0.5는 올린다. 파이썬 round()는 20.5를 20으로 만들어 설명하기 어렵다."""
    return math.floor(value + 0.5)


def strip_names_codes_dates(text: str, names: list[str], codes: list[str]) -> str:
    """제품명, SKU 코드, 날짜를 지운 글. 그 안의 숫자(용량 "20ml", 코드의 번호)를 숫자로 세지 않기 위해서다."""
    for name in sorted(names, key=len, reverse=True):
        text = text.replace(name, " ")
    for code in codes:
        text = text.replace(code, " ")
    text = SKU_CODE.sub(" ", text)
    text = ISO_DATE.sub(" ", text)
    text = KOREAN_DATE.sub(" ", text)
    return SLASH_DATE.sub(" ", text)


def extract_numbers(text: str) -> list[tuple[str, float]]:
    """글에서 숫자를 (원래 글자, 값) 쌍으로 뽑는다. 천 단위 쉼표는 지운다."""
    found: list[tuple[str, float]] = []
    for match in NUMBER.finditer(text):
        found.append((match.group(0), float(match.group(0).replace(",", ""))))
    return found


def fact_sheet_text(report: Report) -> str:
    """LLM에게 줄 사실 표 글. 재고 수량과 하루 판매량은 넣지 않는다(계산할 재료를 없앤다)."""
    info = report.run_info
    a, u, s = len(report.alerts), len(report.unknowns), len(report.safes)
    lines = ["[기준 정보]", f"기준일: {info.run_date}", "판정 방식: 창고별 (제품 남은 일수 = 가장 짧은 창고의 값)", f"경보 {a}건, 판정 불가 {u}건, 안전 {s}건", "", "[경보] 급한 순"]
    for j in report.alerts:
        cause, _todos = describe_alert(j)
        lines.append(f"- {j.name} ({j.sku}): 남은 일수 {format_days(j.days_left)} ({j.shortest_unit}), 필요 일수 {j.threshold}, 원인: {cause}")
    lines.append(f"급한 순 상위 {REQUIRED_ALERT_COUNT}건을 뺀 나머지 경보: {max(0, a - REQUIRED_ALERT_COUNT)}건")
    lines.extend(["", "[판정 불가]"])
    for j in report.unknowns:
        labels = ", ".join(REASON_LABELS.get(reason.code, reason.code) for reason in j.reasons)
        lines.append(f"- {j.name} ({j.sku}): 이유: {labels}")
    lines.extend(["", f"[안전] {s}건"])
    return "\n".join(lines)


def build_fact_sheet(report: Report) -> FactSheet:
    """사실 표와, 요약에 써도 되는 이름·코드·숫자·날짜 목록을 만든다."""
    text = fact_sheet_text(report)
    listed = report.alerts + report.unknowns
    names = [j.name for j in listed]
    codes = [j.sku for j in listed]
    allowed_dates = [date.fromisoformat(raw) for raw in ISO_DATE.findall(text)]
    allowed_numbers: list[float] = []
    for _raw, value in extract_numbers(strip_names_codes_dates(text, names, codes)):
        allowed_numbers.append(value)
        allowed_numbers.append(float(round_half_up(value)))
    required = [(j.name, j.days_left) for j in report.alerts[:REQUIRED_ALERT_COUNT]]
    return FactSheet(
        text=text, year=report.run_info.run_date.year, product_names=names, sku_codes=codes,
        allowed_numbers=allowed_numbers, allowed_dates=allowed_dates, required_items=required,
        unknown_count=len(report.unknowns),
    )


# ===== 검증 =====


def matches_days(value: float, days: float) -> bool:
    """요약의 숫자가 남은 일수 표시값이나 그 정수 반올림과 같은지 본다."""
    return abs(value - days) < NUMBER_TOLERANCE or value == round_half_up(days)


def check_names_and_codes(text: str, facts: FactSheet) -> list[str]:
    """없는 제품명, 없는 SKU 코드, 빠진 상위 경보 제품, 빠진 "판정 불가" 언급을 찾는다."""
    problems: list[str] = []
    bracket_names = BRACKET_NAME.findall(text)
    for name in bracket_names:
        if name not in facts.product_names:
            problems.append(f"「{name}」은(는) 사실 표에 없는 제품명입니다.")
    for code in SKU_CODE.findall(text):
        if code not in facts.sku_codes:
            problems.append(f"{code}는 사실 표에 없는 SKU 코드입니다.")
    for name, _days in facts.required_items:
        if name not in bracket_names:
            problems.append(f"급한 순 상위 경보 제품 「{name}」이 요약에 없습니다.")
    if facts.unknown_count > 0 and not UNKNOWN_WORD.search(text):
        problems.append(f"판정 불가 제품이 {facts.unknown_count}건 있는데 요약에 '판정 불가'라는 말이 없습니다.")
    return problems


def check_dates(text: str, facts: FactSheet) -> list[str]:
    """사실 표에 없는 날짜(새로 계산한 품절일 등)를 찾는다."""
    problems: list[str] = []
    candidates: list[tuple[str, date | None]] = []
    for raw in ISO_DATE.findall(text):
        try:
            candidates.append((raw, date.fromisoformat(raw)))
        except ValueError:
            candidates.append((raw, None))
    for pattern in (KOREAN_DATE, SLASH_DATE):
        for match in pattern.finditer(text):
            try:
                candidates.append((match.group(0), date(facts.year, int(match.group(1)), int(match.group(2)))))
            except ValueError:
                candidates.append((match.group(0), None))
    for raw, value in candidates:
        if value is None or value not in facts.allowed_dates:
            problems.append(f"날짜 '{raw}'는 사실 표에 없는 날짜입니다. 날짜를 새로 계산하지 마세요.")
    return problems


def check_numbers(text: str, facts: FactSheet) -> list[str]:
    """사실 표에 없는 숫자와, 빠진 상위 경보 제품의 남은 일수를 찾는다."""
    problems: list[str] = []
    numbers = extract_numbers(strip_names_codes_dates(text, facts.product_names, facts.sku_codes))
    for raw, value in numbers:
        if not any(abs(value - allowed) < NUMBER_TOLERANCE for allowed in facts.allowed_numbers):
            problems.append(f"숫자 {raw}은(는) 사실 표에 없습니다.")
    for name, days in facts.required_items:
        if days is not None and not any(matches_days(value, days) for _raw, value in numbers):
            problems.append(f"「{name}」의 남은 일수 {format_number(days)}일이 요약에 없습니다. 0.0일도 숫자로 쓰세요.")
    return problems


def check_summary(text: str, facts: FactSheet) -> CheckResult:
    """요약이 사실 표만 썼는지 검사한다. 문제 문구는 재요청 메시지와 테스트가 그대로 쓴다."""
    text = text.strip()
    if not text:
        return CheckResult(ok=False, problems=["요약이 비어 있습니다."])
    problems: list[str] = []
    sentences = [s for s in SENTENCE_SPLIT.split(text) if s.strip()]
    if len(sentences) < MIN_SENTENCES or len(sentences) > MAX_SENTENCES:
        problems.append(f"문장 수가 {len(sentences)}개입니다. {MIN_SENTENCES}~{MAX_SENTENCES}문장이어야 합니다.")
    problems.extend(check_names_and_codes(text, facts))
    problems.extend(check_dates(text, facts))
    problems.extend(check_numbers(text, facts))
    unique = list(dict.fromkeys(problems))  # 같은 문구는 한 번만
    return CheckResult(ok=not unique, problems=unique)


# ===== 요청과 반복 =====


def build_first_user_message(facts: FactSheet) -> str:
    """첫 요청 글. 사실 표만 보고 쓰라고 한다."""
    return "아래 [사실 표]만 보고 오늘 아침 재고 보고 요약을 써 주세요.\n\n[사실 표]\n" + facts.text


def build_retry_message(problems: list[str]) -> str:
    """검증에서 찾은 문제를 번호로 알려 주고 전체를 다시 쓰라고 한다."""
    numbered = "\n".join(f"{i}. {problem}" for i, problem in enumerate(problems, start=1))
    return (
        "방금 쓴 요약에서 아래 문제를 찾았습니다.\n" + numbered + "\n"
        "[사실 표]에 있는 제품명과 숫자만 글자 그대로 써서 요약 전체를 다시 써 주세요. "
        "숫자를 지워서 해결하지 마세요. 처음 규칙은 그대로입니다."
    )


def fallback_text(report: Report, unavailable: bool = False) -> str:
    """LLM 요약 대신 쓰는 정해진 문구. 개수는 코드가 채운다."""
    a, u, s = len(report.alerts), len(report.unknowns), len(report.safes)
    first = "AI 요약을 쓰지 않았습니다(LLM 설정 없음)." if unavailable else "AI 요약을 검증하지 못해 정해진 문구로 대신합니다."
    return f"{first} 오늘 경보 {a}건, 판정 불가 {u}건, 안전 {s}건입니다. 아래 목록을 직접 확인해 주세요."


def summarize(report: Report, facts: FactSheet, client: AnthropicLlmClient | MockLlmClient, provider: str, sleep: Callable[[float], None] = time.sleep) -> SummaryResult:
    """LLM에게 요약을 시키고 검증한다. 틀리면 다시 묻고, 상한에 닿으면 정해진 문구로 대신한다. 4번째 호출은 없다."""
    messages = [{"role": "user", "content": build_first_user_message(facts)}]
    max_attempts = min(1 + MAX_RETRIES, LLM_MAX_CALLS)
    problems: list[str] = []
    for attempt in range(1, max_attempts + 1):
        try:
            text = client.complete(SYSTEM_PROMPT, messages)
        except LlmConfigError as error:  # 다시 해도 같은 오류: 바로 끝낸다
            log_event(logger, logging.ERROR, EVENT_LLM_CONFIG_ERROR, str(error))
            log_event(logger, logging.ERROR, EVENT_SUMMARY_FALLBACK_USED, "LLM 설정 오류로 정해진 문구 사용")
            return SummaryResult(fallback_text(report), "fallback", provider, attempt, [str(error)])
        except LlmCallError as error:  # 일시 오류: 같은 요청으로 다음 시도
            logger.warning("LLM 일시 오류 (%d/%d번째): %s", attempt, max_attempts, error)
            problems = [str(error)]
            if attempt < max_attempts:
                sleep(LLM_RETRY_WAIT_SECONDS)
            continue
        result = check_summary(text, facts)
        if result.ok:
            logger.info("요약 검증 통과 (%d번째 호출)", attempt)
            return SummaryResult(text.strip(), "llm", provider, attempt, [])
        logger.warning("요약 검증 실패 (%d/%d번째): %s", attempt, max_attempts, " / ".join(result.problems))
        problems = result.problems
        messages.append({"role": "assistant", "content": text})
        messages.append({"role": "user", "content": build_retry_message(result.problems)})
    log_event(logger, logging.ERROR, EVENT_SUMMARY_FALLBACK_USED, f"{max_attempts}번 시도로 검증된 요약을 얻지 못함")
    return SummaryResult(fallback_text(report), "fallback", provider, max_attempts, problems)
