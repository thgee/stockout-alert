"""읽어 들인 데이터를 계산에 쓸 수 있게 정리하는 파일.

함수 10개를 clean_data()가 고정된 순서로 부른다. 순서가 중요하다(예: 음수를 먼저 뺀
다음에 같은 키의 중복 합산을 해야, 8과 -6 같은 쌍이 충돌로 잘못 합쳐지지 않는다).
함수 설명의 D1~D12는 README "발견한 데이터 문제와 처리 방법"의 번호다.
"""

import logging
from dataclasses import replace
from datetime import date

from stockout_alert.config import INBOUND_STATUS, TRANSFER_STATUS
from stockout_alert.models import CleanData, DataIssue, InventoryRow, RawData, Rules, SalesRow, SkuInfo

logger = logging.getLogger(__name__)


def normalize_channels(sales: list[SalesRow], known_channels: tuple[str, ...]) -> tuple[list[SalesRow], list[DataIssue]]:
    """채널 표기를 "소문자 + 공백을 밑줄로"로 통일한다. 그래도 모르는 채널이면 행은 남기고 경고만 남긴다(D1)."""
    result: list[SalesRow] = []
    issues: list[DataIssue] = []
    for row in sales:
        fixed = row.channel.strip().lower().replace(" ", "_")
        if fixed != row.channel:
            issues.append(DataIssue(code="channel_fixed", sku=row.sku, issue_date=row.sale_date,
                                     detail=f"{row.channel!r} -> {fixed!r}", action="채널 표기 통일"))
            row = replace(row, channel=fixed)
        if fixed not in known_channels:
            issues.append(DataIssue(code="unknown_channel", sku=row.sku, issue_date=row.sale_date,
                                     detail=fixed, action="행은 남기고 경고"))
        result.append(row)
    return result, issues


def drop_unknown_skus(
    sales: list[SalesRow], inventory: list[InventoryRow], skus: list[SkuInfo]
) -> tuple[list[SalesRow], list[InventoryRow], list[DataIssue]]:
    """마스터에 없는 SKU의 판매·재고 행을 뺀다."""
    known = {info.sku for info in skus}
    issues: list[DataIssue] = []
    kept_sales = []
    for row in sales:
        if row.sku in known:
            kept_sales.append(row)
        else:
            issues.append(DataIssue(code="unknown_sku", sku=row.sku, issue_date=row.sale_date, detail="sales", action="행 제외"))
    kept_inventory = []
    for row in inventory:
        if row.sku in known:
            kept_inventory.append(row)
        else:
            issues.append(DataIssue(code="unknown_sku", sku=row.sku, issue_date=None, detail="inventory", action="행 제외"))
    return kept_sales, kept_inventory, issues


def remove_exact_duplicates(sales: list[SalesRow]) -> tuple[list[SalesRow], list[DataIssue]]:
    """날짜·sku·채널·수량이 전부 같은 행이 둘 이상이면 하나만 남긴다(D2)."""
    seen: set[tuple] = set()
    result: list[SalesRow] = []
    issues: list[DataIssue] = []
    for row in sales:
        key = (row.sale_date, row.sku, row.channel, row.qty)
        if key in seen:
            issues.append(DataIssue(code="duplicate_removed", sku=row.sku, issue_date=row.sale_date,
                                     detail=row.channel, action="중복 행 제거"))
            continue
        seen.add(key)
        result.append(row)
    return result, issues


def remove_negative_rows(sales: list[SalesRow]) -> tuple[list[SalesRow], list[DataIssue]]:
    """음수 판매 행을 뺀다(D3). 원인(반품 등)과 무관하게, 남은 양수만 쓰는 쪽이 항상 더 보수적이다."""
    result: list[SalesRow] = []
    issues: list[DataIssue] = []
    for row in sales:
        if row.qty < 0:
            issues.append(DataIssue(code="negative_removed", sku=row.sku, issue_date=row.sale_date,
                                     detail=f"{row.channel} {row.qty}", action="음수 행 제외"))
            continue
        result.append(row)
    return result, issues


def merge_key_conflicts(sales: list[SalesRow]) -> tuple[list[SalesRow], list[DataIssue]]:
    """같은 (날짜, sku, 채널)에 서로 다른 양수 값이 둘 이상이면 더해서 한 행으로 합친다."""
    groups: dict[tuple, list[SalesRow]] = {}
    for row in sales:
        groups.setdefault((row.sale_date, row.sku, row.channel), []).append(row)
    result: list[SalesRow] = []
    issues: list[DataIssue] = []
    for rows in groups.values():
        if len(rows) == 1:
            result.append(rows[0])
            continue
        total = sum(row.qty for row in rows)
        result.append(replace(rows[0], qty=total))
        issues.append(DataIssue(code="key_conflict_summed", sku=rows[0].sku, issue_date=rows[0].sale_date,
                                 detail=f"{rows[0].channel} {len(rows)}행 합산 -> {total}", action="합산"))
    return result, issues


def remove_duplicate_inventory_rows(inventory: list[InventoryRow]) -> tuple[list[InventoryRow], list[DataIssue]]:
    """재고 파일에서 완전히 같은 행이 둘 이상이면 하나만 남긴다."""
    seen: set[tuple] = set()
    result: list[InventoryRow] = []
    issues: list[DataIssue] = []
    for row in inventory:
        key = (row.as_of, row.sku, row.warehouse, row.status, row.qty)
        if key in seen:
            issues.append(DataIssue(code="inventory_duplicate_removed", sku=row.sku, issue_date=row.as_of,
                                     detail=row.warehouse, action="중복 행 제거"))
            continue
        seen.add(key)
        result.append(row)
    return result, issues


def check_inventory_values(
    inventory: list[InventoryRow], warehouse_to_unit: dict[str, str]
) -> tuple[list[InventoryRow], list[DataIssue]]:
    """음수 재고(invalid_stock, 그 제품은 ROW_PARSE_ERROR가 된다)와 표에 없는 창고(unknown_warehouse)를 뺀다."""
    result: list[InventoryRow] = []
    issues: list[DataIssue] = []
    for row in inventory:
        if row.qty < 0:
            issues.append(DataIssue(code="invalid_stock", sku=row.sku, issue_date=row.as_of,
                                     detail=f"{row.warehouse} {row.qty}", action="행 제외, 제품 판정 불가"))
            continue
        if row.warehouse not in warehouse_to_unit:
            issues.append(DataIssue(code="unknown_warehouse", sku=row.sku, issue_date=row.as_of,
                                     detail=f"{row.warehouse} {row.status} {row.qty}", action="계산에서 제외"))
            continue
        result.append(row)
    return result, issues


def split_inventory_by_status(
    inventory: list[InventoryRow], sellable_statuses: tuple[str, ...]
) -> tuple[list[InventoryRow], list[InventoryRow], list[DataIssue]]:
    """판매가능 재고와 그 밖의 재고(입고예정·이전중 등)를 나눈다(D8)."""
    sellable: list[InventoryRow] = []
    excluded: list[InventoryRow] = []
    issues: list[DataIssue] = []
    known_excluded_statuses = (TRANSFER_STATUS, INBOUND_STATUS)
    for row in inventory:
        if row.status in sellable_statuses:
            sellable.append(row)
            continue
        excluded.append(row)
        if row.status in known_excluded_statuses:
            issues.append(DataIssue(code="stock_excluded", sku=row.sku, issue_date=row.as_of,
                                     detail=f"{row.warehouse} {row.status} {row.qty}", action="계산 제외, 메모로"))
        else:
            issues.append(DataIssue(code="unknown_status", sku=row.sku, issue_date=row.as_of,
                                     detail=f"{row.warehouse} {row.status} {row.qty}", action="계산 제외, 메모로"))
    return sellable, excluded, issues


def check_inventory_dates(inventory: list[InventoryRow]) -> tuple[date, list[DataIssue]]:
    """재고 스냅샷 날짜가 여럿이면 가장 이른 날짜를 쓰고 경고를 남긴다(D9)."""
    dates = {row.as_of for row in inventory}
    issues: list[DataIssue] = []
    earliest = min(dates)
    if len(dates) > 1:
        issues.append(DataIssue(code="snapshot_date_mixed", sku=None, issue_date=earliest,
                                 detail=f"{sorted(dates)}", action=f"가장 이른 날짜({earliest}) 사용"))
    return earliest, issues


def check_master_lead_times(skus: list[SkuInfo], lead_time_range: tuple[int, int]) -> list[DataIssue]:
    """리드타임이 보통 범위를 벗어나면 경고만 남긴다(D10). 값은 그대로 쓴다."""
    min_days, max_days = lead_time_range
    issues: list[DataIssue] = []
    for info in skus:
        if info.lead_time_days < min_days or info.lead_time_days > max_days:
            issues.append(DataIssue(code="lead_time_out_of_range", sku=info.sku, issue_date=None,
                                     detail=f"{info.lead_time_days}일 (보통 {min_days}~{max_days}일)",
                                     action="값은 그대로 쓰고 메모만 남김"))
    return issues


def clean_data(raw: RawData, rules: Rules) -> CleanData:
    """위 함수 10개를 고정된 순서로 불러 CleanData를 만든다."""
    issues: list[DataIssue] = list(raw.load_issues)

    sales, step_issues = normalize_channels(raw.sales, rules.known_channels)
    issues += step_issues
    sales, inventory, step_issues = drop_unknown_skus(sales, raw.inventory, raw.skus)
    issues += step_issues
    sales, step_issues = remove_exact_duplicates(sales)
    issues += step_issues
    sales, step_issues = remove_negative_rows(sales)
    issues += step_issues
    sales, step_issues = merge_key_conflicts(sales)
    issues += step_issues

    inventory, step_issues = remove_duplicate_inventory_rows(inventory)
    issues += step_issues
    inventory, step_issues = check_inventory_values(inventory, rules.warehouse_to_unit)
    issues += step_issues
    sellable, excluded, step_issues = split_inventory_by_status(inventory, rules.sellable_statuses)
    issues += step_issues
    inventory_as_of, step_issues = check_inventory_dates(sellable + excluded)
    issues += step_issues
    issues += check_master_lead_times(raw.skus, rules.lead_time_range)

    logger.info("정리 뒤 행 수: sales=%d, sellable_inventory=%d, excluded_inventory=%d", len(sales), len(sellable), len(excluded))
    for code in sorted({issue.code for issue in issues}):
        count = sum(1 for issue in issues if issue.code == code)
        logger.warning("정리 기록 %s: %d건", code, count)

    return CleanData(
        skus={info.sku: info for info in raw.skus},
        sellable_inventory=sellable,
        excluded_inventory=excluded,
        sales=sales,
        issues=issues,
        inventory_as_of=inventory_as_of,
    )
