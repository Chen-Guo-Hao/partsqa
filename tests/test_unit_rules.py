"""Unit：標準化、屬性換算、單位換算、日期與效期規則。"""

from datetime import date

import pytest

from partsqa.attributes import parse_attr
from partsqa.engine import add_months, ceil_qty
from partsqa.normalize import match_allowed, normalize_part_no, normalize_plant, parse_date, part_type_of
from partsqa.units import bom_qty_to_master, stock_qty_to_master


@pytest.mark.parametrize("raw,expected", [
    (" cap－000005 ", "CAP-000005"), ("ＧＰＵ－１００８０１", "GPU-100801"), ("CPU–110105", "CPU-110105"), ("fgs208-01", "FGS208-01"),
    ("asy-10001", "ASY-10001"),
])
def test_part_no_normalization(raw, expected):
    """料號標準化只做格式處理（全形、破折號、大小寫、空白）。"""
    assert normalize_part_no(raw) == expected


@pytest.mark.parametrize("pn", ["CAP-16O101", "GPU-1OO801", "MEM-12010", "MEC 123456", "GPU_100801", "XYZ-123456",
                                "FGD180-02", "FGS20-01", "ASY-1000", "SA100001"])
def test_invalid_part_numbers_not_guessed(pn):
    assert part_type_of(normalize_part_no(pn)) is None


def test_plant_aliases():
    assert normalize_plant("twhc ") == "TW-HC"
    assert normalize_plant("台灣-高雄") == "TW-KH" and normalize_plant("新竹廠") == "TW-HC"
    assert normalize_plant("HQ") is None and normalize_plant("TW-TP") is None and normalize_plant("VN-HN") is None


def test_dates_only_three_formats():
    assert parse_date("2024/01/01") == date(2024, 1, 1)
    assert parse_date("20240101") == date(2024, 1, 1)
    assert parse_date("01/01/2024") is None and parse_date("01-Jan-2024") is None and parse_date("TBD") is None


def test_allowed_values_case_rules():
    assert match_allowed(" approved ", ["Approved", "Pending", "Rejected"], True) == "Approved"
    assert match_allowed("核准中", ["Approved", "Pending", "Rejected"], True) is None
    assert match_allowed("OK", ["合格", "待驗", "凍結", "MRB"], False) is None
    assert match_allowed("pcs", ["PCS", "M"], False) is None  # UoM 未註明不分大小寫


@pytest.mark.parametrize("code,v,u,exp", [
    ("Capacitance", "100pF", "", 0.0001), ("Capacitance", "10000", "nF", 10.0), ("Capacitance", "100", "", 100.0),
    ("Resistance", "4.7", "K", 4700.0), ("Resistance", "10kohm", "", 10000.0), ("Resistance", "1MΩ", "", 1e6),
    ("RatedVoltage", "1.2kV", "", 1200.0), ("PowerRating", "500mW", "", 0.5), ("TempMin", "−40℃", "", -40.0),
    ("Tolerance", "±0.5%", "", 0.5), ("RoHS", "y", "", "Y"), ("RoHS", "是", "", None), ("MSL", "TBD", "", None),
    ("Capacitance", "10", "ohm", None),
])
def test_attribute_conversion(code, v, u, exp):
    assert parse_attr(code, v, u) == exp


def test_unit_conversion():
    assert bom_qty_to_master(1200, "MM", "M") == pytest.approx(1.2)
    assert bom_qty_to_master(1, "KPCS", "PCS") == 1000
    assert bom_qty_to_master(1, "M", "PCS") is None
    assert stock_qty_to_master(2, "KPCS", "PCS") == 2000
    assert stock_qty_to_master(2, "PCS", None) is None


def test_rounding_and_month_end():
    assert ceil_qty(816.0000000001, "PCS") == 816  # 浮點誤差不應多進一位
    assert ceil_qty(816.01, "PCS") == 817
    assert ceil_qty(1.231, "M") == 1.24
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)


def test_product_line_and_commodity_from_domain():
    from partsqa.normalize import commodity_of, product_line_of_fg

    assert product_line_of_fg("FGS208-01") == "SRV" and product_line_of_fg("FGE102-01") == "EDGE"
    assert product_line_of_fg("FGD180-02") is None
    assert part_type_of("ASY-10001") == "SA" and part_type_of("GPU-100801") == "RM"
    assert commodity_of("MEM-120101") == "MEM" and commodity_of("ZZZ-123456") is None   # ZZZ 不在本領域的物料類別
