"""cleaner.py의 데이터 정리 함수를 확인하는 테스트."""

from datetime import date

from stockout_alert import cleaner
from stockout_alert.models import InventoryRow, SalesRow


def _sale(sku="SKU-01", d=date(2026, 10, 1), channel="amazon_us", qty=10, line_no=1):
    return SalesRow(line_no=line_no, sale_date=d, sku=sku, channel=channel, qty=qty)


def test_merge_key_conflicts_runs_after_negative_removal():
    """막는 사고: 8과 -6을 먼저 더해 2로 합쳐버려서, 음수를 버리는 결정과 반대로 동작한다."""
    sales = [_sale(qty=8, line_no=1), _sale(qty=-6, line_no=2)]
    no_negative, _ = cleaner.remove_negative_rows(sales)
    merged, issues = cleaner.merge_key_conflicts(no_negative)
    assert [row.qty for row in merged] == [8]  # 8-6=2로 더한 것이 아니라 음수가 이미 빠진 상태
    assert len(issues) == 0  # 충돌 자체가 없음


def test_split_inventory_by_status_separates_sellable():
    """막는 사고: 입고예정·이전중 재고를 판매가능 재고와 합쳐서 세어 거짓 안전이 생긴다."""
    rows = [
        InventoryRow(line_no=1, as_of=date(2026, 10, 5), sku="SKU-01", warehouse="FBA_US", status="판매가능", qty=100),
        InventoryRow(line_no=2, as_of=date(2026, 10, 5), sku="SKU-01", warehouse="FBA_US", status="이전중", qty=50),
    ]
    sellable, excluded, issues = cleaner.split_inventory_by_status(rows, ("판매가능",))
    assert [row.qty for row in sellable] == [100]
    assert [row.qty for row in excluded] == [50]
    assert issues[0].code == "stock_excluded"
