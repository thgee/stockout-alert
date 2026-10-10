"""프로젝트 전체가 쓰는 데이터 모양을 한 곳에 모은 파일.

dataclass는 필드 이름과 타입만 적으면 파이썬이 생성자를 대신 만들어 주는 클래스다.
frozen=True를 붙인 것은 만든 뒤에는 값을 바꿀 수 없는 클래스다. 읽기만 하는 값에 쓴다.
이 파일은 표준 라이브러리만 쓰고, 다른 stockout_alert 모듈을 import하지 않는다.
"""

from dataclasses import dataclass
from datetime import date

# ===== 판정 상태 =====
STATUS_SAFE = "안전"
STATUS_ALERT = "경보"
STATUS_UNKNOWN = "판정불가"
# 묶음 여러 개를 하나의 제품 상태로 합칠 때, 숫자가 큰 쪽(더 나쁜 쪽)이 이긴다.
STATUS_RANK = {STATUS_SAFE: 0, STATUS_UNKNOWN: 1, STATUS_ALERT: 2}

# ===== 보고 종류 =====
KIND_NORMAL = "normal"
KIND_DATA_LATE = "data_late"

# 이 정리 기록 코드가 하나라도 있는 제품은 그 자체로 ROW_PARSE_ERROR 판정 불가다.
BLOCKING_ISSUE_CODES = ("row_parse_error", "invalid_stock")

# ===== 판정 사유 문구 =====
# {unit}은 config.UNIT_LABELS의 사람이 읽는 이름("아마존 창고(FBA_US)" 등)으로 채운다.
# 날짜는 M/D, 일수는 소수 1자리, 수량은 천 단위 쉼표다. 채우는 곳은 judge.make_reason()이다.
REASON_MESSAGES = {
    # 제품 단위: 계산 자체를 할 수 없는 경우
    "ROW_PARSE_ERROR": "읽을 수 없는 행이 있어 이 제품은 판정하지 않았습니다({detail}).",
    "NO_INVENTORY": "재고 파일에 이 제품이 없습니다. 재고 파일이 빠졌는지 확인해 주세요.",
    "INVENTORY_STALE": "재고 스냅샷이 {age}일 전({as_of}) 것이라 지금 재고를 알 수 없습니다.",
    "NO_SALES_HISTORY": (
        "판매 기록이 한 건도 없어 판매 속도를 알 수 없습니다(판매가능 재고 {stock}개). "
        "출시 전인지, 판매 데이터가 빠졌는지 확인해 주세요."
    ),
    "ZERO_UNITS": "판정할 묶음이 없습니다. 이 제품의 재고 창고와 최근 판매 채널이 묶음 표에 없습니다. 설정의 묶음 표를 확인해 주세요.",
    # 제품 단위(창 안 판매 합 0) 또는 묶음 단위(급증일에만 팔림)
    "NO_RECENT_SALES": (
        "{unit}: 최근 {window_days}일 동안 급증일을 뺀 판매가 없어 하루 판매 속도를 정할 수 없습니다"
        "(판매가능 재고 {stock}개, 마지막 판매 {last_sale})."
    ),
    # 묶음 단위
    "BELOW_THRESHOLD": "{unit} 남은 {days}일(넉넉하게 잡아도 {good}일), 필요 일수 {threshold}일(리드타임 {lead}일 + 안전 {safety}일).",
    "SALES_GAP": (
        "{unit}: 최근 {window_days}일 중 {gap_count}일({gap_dates}) 판매 기록이 없어 안전이라고 말할 수 없습니다. "
        "빈 날을 0개로 치면 {good}일, 기록 있는 날로만 보면 {bad}일입니다."
    ),
    "SPIKE_CHANGES_VERDICT": (
        "{unit}: {spike_date} 하루 {spike_qty}개(평소의 {ratio}배)를 빼면 {good}일, 넣으면 {bad}일이라 결론이 갈립니다. "
        "그날 행사였는지 확인해 주세요."
    ),
    "INVENTORY_LATE": (
        "{unit}: 재고 스냅샷이 {age}일 전({as_of}) 것이라 그사이 재고 이동을 알 수 없어서 안전이라고 말할 수 없습니다. "
        "계산상 {days}일입니다."
    ),
    "UNKNOWN_CHANNEL": (
        "{unit}: 묶음 표에 없는 판매 채널({channels})의 판매가 최근 {window_days}일에 {qty}개 있어, "
        "어느 창고 판매인지 몰라 안전이라고 말할 수 없습니다."
    ),
}


@dataclass(frozen=True)
class SkuInfo:
    """sku_master.csv 한 줄. 제품 하나의 이름과 판정 기준."""

    sku: str
    name: str
    lead_time_days: int
    safety_days: int


@dataclass(frozen=True)
class InventoryRow:
    """inventory_snapshot.csv 한 줄. 창고 하나, 상태 하나의 재고 수량."""

    line_no: int
    as_of: date  # 재고를 찍은 날짜. 실행 기준일(run_date)과는 다른 값이다
    sku: str
    warehouse: str
    status: str
    qty: int


@dataclass(frozen=True)
class SalesRow:
    """sales_daily.csv 한 줄. 어느 날, 어느 채널에서 몇 개 팔렸는지."""

    line_no: int
    sale_date: date
    sku: str
    channel: str
    qty: int


@dataclass(frozen=True)
class DataIssue:
    """정리 과정에서 생긴 기록 한 건. 영향을 받은 행마다 하나씩 쌓인다."""

    code: str  # channel_fixed, duplicate_removed, negative_removed, row_parse_error 등
    sku: str | None
    issue_date: date | None
    detail: str
    action: str


@dataclass
class RawData:
    """CSV 3개를 그대로 읽은 결과. 아직 정리하지 않은 원본이다."""

    skus: list[SkuInfo]
    inventory: list[InventoryRow]
    sales: list[SalesRow]
    load_issues: list[DataIssue]


@dataclass
class CleanData:
    """정리를 마친 데이터. 이후 계산은 이 값만 쓴다."""

    skus: dict[str, SkuInfo]
    sellable_inventory: list[InventoryRow]
    excluded_inventory: list[InventoryRow]
    sales: list[SalesRow]  # 모르는 채널 행도 남긴다 (경고와 안전 보류에 쓴다)
    issues: list[DataIssue]
    inventory_as_of: date  # 스냅샷 날짜가 여럿이면 그중 가장 이른 날짜


@dataclass(frozen=True)
class Rules:
    """판정에 쓰는 규칙 값 전부. config.build_rules()가 config.py 상수로 만든다."""

    velocity_window_days: int
    expected_sales_lag_days: int
    outlier_multiplier: float
    expected_snapshot_age_days: int  # 경과일이 이보다 길면 INVENTORY_LATE
    max_snapshot_age_days: int  # 경과일이 이보다 길면 INVENTORY_STALE
    sellable_statuses: tuple[str, ...]
    known_channels: tuple[str, ...]
    lead_time_range: tuple[int, int]
    channel_to_unit: dict[str, str]  # 채널 -> 창고 묶음
    warehouse_to_unit: dict[str, str]  # 창고 -> 창고 묶음


@dataclass
class RunInfo:
    """실행 한 번의 날짜 계산 결과. 데이터 계산에 들어가기 전에 먼저 정한다."""

    run_date: date
    window_start: date
    window_end: date  # 계산 기간의 끝 (run_date - expected_sales_lag_days)
    channel_last_dates: dict[str, date]  # 채널별 판매 기록의 마지막 날
    complete_last_date: date  # 그중 가장 이른 날. 이 뒤 날짜는 모든 제품의 빈 날로 친다
    sales_late_days: int  # max(0, window_end - complete_last_date)
    inventory_as_of: date
    snapshot_age_days: int  # run_date - inventory_as_of
    report_kind: str  # 판매가 늦거나 재고 경과일 > 1이면 KIND_DATA_LATE
    header_warnings: list[str]


@dataclass
class SalesDates:
    """제품 하나를 기준으로 정한 날짜 묶음. metrics가 묶음별 계산에 그대로 넘긴다."""

    start: date
    end: date  # 계산 기간의 끝 (run_date - 2)
    last_day: date  # min(end, 완전한 마지막 날). 이 뒤 날짜의 판매는 계산에 쓰지 않는다
    recorded: list[date]  # 제품 기록이 있는 날
    gaps: list[date]  # 계산 기간 중 기록이 없는 날 전부
    spikes: list[date]  # 급증일로 판단한 날
    used: list[date]  # recorded에서 spikes를 뺀 날 (기본 일수 계산에 쓰는 날)


@dataclass
class UnitMetrics:
    """묶음(창고 하나, 또는 전체) 하나의 계산 결과."""

    unit: str  # "FBA_US" / "국내창고"
    channels: list[str]
    stock: int  # 이 묶음에 속한 창고들의 판매가능 재고 합
    has_stock_row: bool  # False면 재고 파일에 행이 없어 0으로 계산했다는 뜻
    excluded: list[tuple[str, int, float | None]]  # (상태, 수량, 들어오면 며칠분)
    used_total: int  # 쓰는 날(used)의 이 묶음 판매 합
    recorded_total: int  # 기록 있는 모든 날(급증일 포함)의 이 묶음 판매 합
    velocity: float | None  # used_total / 쓰는 날 수. 화면 표시용
    days_left: float | None  # 기본 일수: 빈 날 빼고, 급증일 빼고. 표시와 정렬에만 쓴다
    days_best: float | None  # 넉넉하게 잡은 일수: 빈 날은 0개로 치고, 급증일은 뺀다
    days_worst: float | None  # 빠듯하게 잡은 일수: 빈 날은 빼고, 급증일은 넣는다
    channel_missing_dates: list[date]  # 제품 기록은 있는데 이 묶음 채널 행만 없던 날
    last_sale_date: date | None  # 이 묶음 채널의 파일 전체 마지막 판매일. 사유 문구에만 쓴다


@dataclass
class SkuMetrics:
    """제품 하나의 계산 결과. 판정(안전/경보/판정불가)은 judge.py가 이 값을 받아서 한다."""

    sku: str
    name: str
    lead_time_days: int
    safety_days: int
    threshold: int  # lead_time_days + safety_days
    has_inventory_rows: bool
    has_sales_rows: bool  # 판매 파일 전체 기간을 기준으로
    last_sale_date: date | None  # 파일 전체 마지막 판매일. 기록이 없으면 None
    has_blocking_issue: bool
    window_sales_total: int  # 계산 기간 안 판매 합 (급증일 포함, 모든 채널)
    daily_totals: dict[date, int]  # 계산 기간 안, 제품 단위, 기록 있는 날만
    dates: SalesDates
    unknown_channels: list[str]  # 계산 기간 안 판매가 있는, 묶음 표에 없는 채널
    warehouse_units: list[UnitMetrics]  # 판정 대상 창고 묶음 (계산 기간 안 판매가 0인 묶음은 뺀다)
    skipped_units: list[tuple[str, int]]  # 판매가 없어 뺀 묶음 (이름, 판매가능 재고)
    stock_total: int  # 모든 창고의 판매가능 재고 합. 제품 단위 사유 문구와 정렬 동점 처리에 쓴다
    unknown_qty: int  # 계산 기간 안 묶음 표에 없는 채널의 판매 합


@dataclass(frozen=True)
class Reason:
    """판정 사유 한 건. 문구는 REASON_MESSAGES[code]를 값으로 채운 결과다."""

    code: str
    unit: str | None  # 제품 단위 사유면 None
    text: str


@dataclass
class UnitJudgment:
    """묶음 하나의 판정 결과."""

    unit: str
    status: str
    reasons: list[Reason]
    stock: int
    has_stock_row: bool
    velocity: float | None
    days_left: float | None
    days_best: float | None
    days_worst: float | None
    excluded: list[tuple[str, int, float | None]]


@dataclass
class Judgment:
    """제품 하나의 최종 판정 결과. 묶음 판정 중 가장 나쁜 것을 따른다."""

    sku: str
    name: str
    status: str
    reasons: list[Reason]  # 제품 상태와 같은 상태인 묶음들의 사유
    notes: list[str]  # 주의 메모 (마침표 없이 쓴다)
    days_left: float | None  # 판정한 묶음의 기본 일수 중 가장 짧은 값
    shortest_unit: str | None  # 그 값을 낸 묶음 이름 (경보 첫 줄에 쓴다)
    stock_total: int  # 모든 창고의 판매가능 재고 합
    threshold: int
    lead_time_days: int
    safety_days: int
    urgent: bool  # 경보 묶음 중 하나라도 기본 일수가 lead_time_days보다 짧으면 참
    units: list[UnitJudgment]


@dataclass
class SummaryResult:
    """AI 요약 생성 결과."""

    text: str
    source: str  # "llm" | "fallback"
    provider: str  # "anthropic" | "mock" | "none"
    calls_used: int
    last_problems: list[str]
    label: str = ""  # 요약 제목에 붙일 짧은 설명(모델 이름, "키 없음" 등)


@dataclass
class Report:
    """실행 한 번의 보고 전체. 슬랙 메시지와 화면 출력이 모두 이 값에서 나온다."""

    run_info: RunInfo
    alerts: list[Judgment]
    unknowns: list[Judgment]
    safes: list[Judgment]
    data_issues: list[DataIssue]
    summary: SummaryResult | None = None


@dataclass
class FactSheet:
    """AI 요약을 검증할 때 쓰는 사실 표. 요약에 쓸 수 있는 숫자와 이름만 담는다."""

    text: str
    year: int
    product_names: list[str]
    sku_codes: list[str]
    allowed_numbers: list[float]  # 사실 표 숫자와 그 숫자를 정수로 반올림한 값
    allowed_dates: list[date]
    required_items: list[tuple[str, float]]  # 급한 순 상위 3개 경보 (이름, 남은 일수)
    unknown_count: int


@dataclass
class CheckResult:
    """요약 검증 결과."""

    ok: bool
    problems: list[str]


# ===== 오류 클래스 =====
# 상속은 여기서만 쓴다. 각 오류가 어떤 상황에서 나는지와, main()이 그 오류를 어떻게
# 처리하는지(종료 코드, RUN_FAILED 메시지)는 클래스 docstring에 적는다.


class ConfigError(Exception):
    """설정값이 잘못됐을 때 낸다. 종료 코드 1, RUN_FAILED 메시지는 "설정 오류: ..."다."""


class DataLoadError(Exception):
    """입력 CSV를 읽거나 정리할 수 없을 때 낸다. 종료 코드 1, RUN_FAILED 메시지는 "입력 데이터 오류: ..."다."""


class SlackSendError(Exception):
    """재시도를 다 써도 슬랙 발송이 실패했을 때 낸다. 종료 코드 1이다."""


class SentButNotRecorded(Exception):
    """발송은 끝났는데 발송 기록 저장에 실패했을 때 낸다. 종료 코드 1이다."""


class LlmCallError(Exception):
    """다시 시도하면 될 수 있는 일시적인 LLM 오류다 (시간 초과, 연결 끊김, 5xx, 429)."""


class LlmConfigError(Exception):
    """다시 불러도 똑같이 실패하는 LLM 오류다 (400, 401, 403, 404). 곧바로 정해진 문구로 넘어간다."""
