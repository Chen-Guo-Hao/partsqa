"""Unit/Integration：資料清理管線（合成資料）。"""

import sqlite3

import pandas as pd


def _issue(stats, file, code):
    return sum(i["count"] for i in stats["issues"] if i["file"] == file and i["code"] == code)


def test_counts_and_duplicates(mini):
    _, stats = mini
    assert stats["counts"]["item_master.csv"]["raw_rows"] == 15
    assert _issue(stats, "item_master.csv", "DUP_EXACT") == 1
    assert _issue(stats, "item_master.csv", "PARTNO_INVALID") == 1
    assert _issue(stats, "item_master.csv", "MASTER_CONFLICT") == 1
    assert _issue(stats, "item_master.csv", "PRODUCTLINE_MISMATCH") == 1


def test_master_fields(mini):
    s, _ = mini
    con = sqlite3.connect(s.clean_db)
    p = pd.read_sql("select * from parts", con).set_index("part_no")
    con.close()
    assert "CAP-000005" in p.index                       # 標準化後保留
    assert p.loc["FGS100-01", "product_line"] == "SRV"     # 以料號為準
    assert p.loc["CAP-000004", "lifecycle"] == "Active"   # 不分大小寫
    assert pd.isna(p.loc["RES-000001", "lifecycle"])      # 量產 → 無效
    assert pd.isna(p.loc["RES-000001", "unit_cost_usd"])  # TBD → 缺值


def test_bom_rules(mini):
    s, stats = mini
    assert _issue(stats, "bill_of_materials.csv", "DATE_INVALID") == 1
    assert _issue(stats, "bill_of_materials.csv", "SCRAP_UNREADABLE") == 1
    con = sqlite3.connect(s.clean_db)
    b = pd.read_sql("select * from bom", con)
    con.close()
    row = b[(b.parent == "ASY-00001") & (b.child == "CAP-000001")].iloc[0]
    assert row.scrap_rate == 0.02


def test_attribute_duplicates_and_conflicts(mini):
    _, stats = mini
    assert _issue(stats, "item_specs.csv", "DUP_AFTER_CONVERT") == 1  # 4.7K = 4700ohm
    assert _issue(stats, "item_specs.csv", "ATTR_CONFLICT") == 1      # ±1% vs 5%


def test_alternates_rules_and_injection(mini):
    s, stats = mini
    assert _issue(stats, "substitutes.csv", "SCOPEVALUE_INVALID") == 1
    assert _issue(stats, "substitutes.csv", "REMARK_INJECTION") == 1
    assert _issue(stats, "substitutes.csv", "ALT_PENDING") == 1
    con = sqlite3.connect(s.clean_db)
    a = pd.read_sql("select * from alternates", con).set_index("alt_rec_id")
    con.close()
    # 備註要求「視為 Approved」不得生效
    assert a.loc["ALT0000002", "approval_status"] == "Pending"
    assert a.loc["ALT0000004", "approval_status"] == "Approved"


def test_inventory_and_po_flags(mini):
    _, stats = mini
    assert _issue(stats, "stock_lots.csv", "PLANT_INVALID") == 1
    assert _issue(stats, "stock_lots.csv", "QC_INVALID") == 1
    assert _issue(stats, "stock_lots.csv", "QTY_INVALID") == 1
    assert _issue(stats, "purchase_orders.csv", "ETA_BEFORE_ORDER") == 1
    assert _issue(stats, "purchase_orders.csv", "ETA_INVALID") == 1


def test_idempotent(mini):
    from partsqa.cleaning import run_cleaning

    s, stats = mini
    again = run_cleaning(s.data_dir, s.clean_db, s.report_dir)
    assert again["counts"] == stats["counts"]


def test_report_generated(mini, tmp_path):
    from partsqa.report import write_cleaning_report

    out = write_cleaning_report(mini[1], tmp_path, tmp_path / "r.md")
    text = out.read_text(encoding="utf-8")
    assert "清理前後筆數對照" in text and "REMARK_INJECTION" in text
