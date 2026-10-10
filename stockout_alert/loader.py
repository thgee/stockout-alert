"""CSV 3개를 읽어서 dataclass 목록으로 만드는 파일.

행 하나가 깨져도 전체를 멈추지 않는다. 다만 SKU 값 자체를 읽을 수 없는 행이나 마스터
파일이 깨진 경우는 어느 제품이 영향을 받는지 알 수 없어서 예외(DataLoadError)를 낸다.
"""

import csv
import logging
from datetime import date
from pathlib import Path

from stockout_alert.models import DataIssue, DataLoadError, InventoryRow, RawData, SalesRow, SkuInfo

logger = logging.getLogger(__name__)


def read_csv_rows(path: Path, required_columns: tuple[str, ...]) -> list[dict[str, str]]:
    """CSV 파일을 읽어 행 목록(dict)으로 돌려준다. 파일이 없거나 열이 모자라면 DataLoadError."""
    if not path.exists():
        raise DataLoadError(f"{path} 파일이 없습니다")
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [col for col in required_columns if col not in (reader.fieldnames or [])]
        if missing:
            raise DataLoadError(f"{path}에 필요한 열이 없습니다: {missing}")
        rows = list(reader)
    if not rows:
        raise DataLoadError(f"{path}에 데이터가 한 행도 없습니다")
    return rows


def parse_date_value(raw: str) -> date:
    """YYYY-MM-DD 문자열을 날짜로 바꾼다. 형식이 틀리면 ValueError(부른 쪽이 처리한다)."""
    return date.fromisoformat(raw.strip())


def parse_int_value(raw: str) -> int:
    """정수 문자열을 숫자로 바꾼다. 형식이 틀리면 ValueError(부른 쪽이 처리한다)."""
    return int(raw.strip())


def load_sku_master(path: Path) -> list[SkuInfo]:
    """sku_master.csv를 읽는다. 마스터는 판정 기준 자체라서 행 하나만 깨져도 전체를 멈춘다."""
    rows = read_csv_rows(path, ("sku", "name", "lead_time_days", "safety_days"))
    result: list[SkuInfo] = []
    seen: set[str] = set()
    for line_no, row in enumerate(rows, start=2):  # 1번 줄은 머리글
        sku = row["sku"].strip()
        if not sku:
            raise DataLoadError(f"{path} {line_no}번째 줄: sku가 비어 있습니다")
        if sku in seen:
            raise DataLoadError(f"{path} {line_no}번째 줄: sku {sku}가 중복됩니다")
        seen.add(sku)
        try:
            lead_time_days = parse_int_value(row["lead_time_days"])
            safety_days = parse_int_value(row["safety_days"])
        except ValueError as error:
            raise DataLoadError(f"{path} {line_no}번째 줄({sku}): 숫자를 읽을 수 없습니다") from error
        result.append(
            SkuInfo(sku=sku, name=row["name"].strip(), lead_time_days=lead_time_days, safety_days=safety_days)
        )
    return result


def load_inventory(path: Path) -> tuple[list[InventoryRow], list[DataIssue]]:
    """inventory_snapshot.csv를 읽는다. SKU를 읽을 수 있으면 다른 값이 깨져도 그 행만 뺀다."""
    rows = read_csv_rows(path, ("as_of", "sku", "warehouse", "status", "qty"))
    result: list[InventoryRow] = []
    issues: list[DataIssue] = []
    for line_no, row in enumerate(rows, start=2):
        sku = row["sku"].strip()
        if not sku:
            raise DataLoadError(f"{path} {line_no}번째 줄: sku를 읽을 수 없습니다")
        try:
            as_of = parse_date_value(row["as_of"])
            qty = parse_int_value(row["qty"])
        except ValueError:
            issues.append(
                DataIssue(code="row_parse_error", sku=sku, issue_date=None, detail=f"{path} {line_no}", action="행 제외")
            )
            continue
        result.append(
            InventoryRow(
                line_no=line_no, as_of=as_of, sku=sku, warehouse=row["warehouse"].strip(),
                status=row["status"].strip(), qty=qty,
            )
        )
    return result, issues


def load_sales(path: Path) -> tuple[list[SalesRow], list[DataIssue]]:
    """sales_daily.csv를 읽는다. SKU를 읽을 수 있으면 다른 값이 깨져도 그 행만 뺀다."""
    rows = read_csv_rows(path, ("date", "sku", "channel", "qty"))
    result: list[SalesRow] = []
    issues: list[DataIssue] = []
    for line_no, row in enumerate(rows, start=2):
        sku = row["sku"].strip()
        if not sku:
            raise DataLoadError(f"{path} {line_no}번째 줄: sku를 읽을 수 없습니다")
        try:
            sale_date = parse_date_value(row["date"])
            qty = parse_int_value(row["qty"])
        except ValueError:
            issues.append(
                DataIssue(code="row_parse_error", sku=sku, issue_date=None, detail=f"{path} {line_no}", action="행 제외")
            )
            continue
        result.append(
            SalesRow(line_no=line_no, sale_date=sale_date, sku=sku, channel=row["channel"].strip(), qty=qty)
        )
    return result, issues


def load_all(data_dir: Path) -> RawData:
    """data_dir 아래 세 CSV(sku_master, inventory_snapshot, sales_daily)를 전부 읽는다."""
    skus = load_sku_master(data_dir / "sku_master.csv")
    inventory, inventory_issues = load_inventory(data_dir / "inventory_snapshot.csv")
    sales, sales_issues = load_sales(data_dir / "sales_daily.csv")
    logger.info("읽은 행 수: sku_master=%d, inventory=%d, sales=%d", len(skus), len(inventory), len(sales))
    return RawData(skus=skus, inventory=inventory, sales=sales, load_issues=inventory_issues + sales_issues)
