"""Unit：計算引擎（BOM 展開、可用庫存、在途、替代料 A1–A12、缺料試算）。"""

from datetime import date

D = date(2026, 11, 20)


def test_explode_and_requirements(engine):
    ex = engine.explode("FGS100-01", D)
    req = ex.requirement_per_root()
    assert req["CAP-000001"] == 2 * 1.02
    assert req["GPU-000001"] == 1.01
    assert req["RES-000001"] == 1000          # KPCS→PCS，損耗率 abc 以 0 計
    assert "CAP-000005" not in req and "CAP-000004" not in req  # 無效日期行、失效版次不展開
    assert ex.excluded["生產日不在生效區間"] == 1


def test_where_used(engine):
    w = engine.where_used("RES-000001", D)
    assert set(w["finished_goods"]) == {"FGS100-01", "FGE100-01"}


def test_available_stock_rules(engine):
    st = engine.available_stock("CAP-000002", "TW-HC", D)
    assert st["available"] == 100  # 150-50，OK 批次不計
    mos = engine.available_stock("GPU-000001", "TW-HC", D)
    assert mos["available"] == 80   # 2501 過期、2615A 無法確認效期
    assert "已過期（到期日早於生產日）" in mos["excluded_lots"]
    assert "DateCode 無效，無法確認效期" in mos["excluded_lots"]


def test_in_transit_rules(engine):
    tr = engine.in_transit("CAP-000001", "TW-HC", D)
    assert tr["in_transit_in_time"] == 50     # 60-10，ETA 11/10+3 ≤ 11/20
    assert len(tr["po_lines_too_late"]) == 1  # 11/19+3 > 11/20
    assert tr["excluded_po_lines"]["ETA 早於下單日（日期矛盾）"] == 1


def test_alternate_rules(engine):
    opts = {o["alt_rec_id"]: o for o in engine.alternate_options("CAP-000001", "ASY-00001", {"FGS100-01"}, D)}
    assert opts["ALT0000001"]["usable"]
    assert not opts["ALT0000002"]["usable"]                                # Pending（備註不具效力）
    assert any(f.startswith("A1") for f in opts["ALT0000002"]["fails"])
    assert any(f.startswith("A7 RatedVoltage") for f in opts["ALT0000002"]["fails"])
    assert any(f.startswith("A3") for f in opts["ALT0000004"]["fails"])    # 反向不允許
    assert any(f.startswith("A5") for f in opts["ALT0000006"]["fails"])    # 限 EDGE 產品線
    # A4 不得串接：CAP-000001→CAP-000002→CAP-000004 不會出現「CAP-000001 可用 CAP-000004」的串接紀錄
    assert all(o["alt_rec_id"] != "ALT0000003" for o in opts.values())


def test_shortage_with_po_and_alternates(engine):
    base = engine.shortage("FGS100-01", 100, "TW-HC", D)
    rows = {r["part"]: r for r in base["rows"]}
    assert rows["CAP-000001"]["gross_requirement"] == 204
    assert rows["CAP-000001"]["shortage"] == 104
    po = engine.shortage("FGS100-01", 100, "TW-HC", D, include_po=True)
    assert {r["part"]: r for r in po["rows"]}["CAP-000001"]["shortage"] == 54
    alt = engine.shortage("FGS100-01", 100, "TW-HC", D, include_po=True, include_alt=True)
    assert alt["alternate_switches"][0]["substitute"] == "CAP-000002"
    assert alt["alternate_switches"][0]["boards_switched"] == int(alt["alternate_switches"][0]["boards_switched"])  # MixAllowed=N 整片切換
    assert alt["max_buildable"] >= po["max_buildable"]


def test_review_approval_enables_pending(mini):
    import sqlite3

    from partsqa.engine import Engine
    con = sqlite3.connect(mini[0].clean_db)
    key = con.execute("select review_key from alternates where alt_rec_id='ALT0000002'").fetchone()[0]
    con.close()
    e = Engine(mini[0].clean_db, approved_keys={key})
    o = [x for x in e.alternate_options("CAP-000001", "ASY-00001", {"FGS100-01"}, D) if x["alt_rec_id"] == "ALT0000002"][0]
    assert not any(f.startswith("A1") for f in o["fails"])   # 已核准
    assert any(f.startswith("A7") for f in o["fails"])       # 但屬性仍不相容


def test_rohs_must_be_y_and_blocked_part_po_excluded(engine):
    # CAP-000004 沒有 RoHS 屬性 → A7 須判定不符（替代料 RoHS 一律須為 Y）
    o = [x for x in engine.alternate_options("CAP-000001", "ASY-00001", {"FGS100-01"}, D) if x["alt_rec_id"] == "ALT0000006"][0]
    assert any("RoHS" in f for f in o["fails"])
    # 主檔待人工確認的料號：在途也暫不使用
    engine.blocked_parts.add("CAP-000001")
    assert engine.in_transit("CAP-000001", "TW-HC", D)["in_transit_in_time"] == 0
