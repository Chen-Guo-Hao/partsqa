"""通用標準化函式：料號、工廠、日期、允許值、數字。

料號格式、產品線、工廠與物料類別由 config/domain.json 定義（見 domain.py）；
原則是「不符合就視為缺值（回傳 None）」，絕不猜測修正。
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime

from .domain import get_domain

_DOMAIN = get_domain()

# ---------------------------------------------------------------- 料號
_DASHES = "–—－‐‑−﹣"  # 各種破折號／減號一律轉為 "-"
PART_PATTERNS = _DOMAIN.part_patterns
COMMODITY_GROUPS: dict[str, list[str]] = _DOMAIN.commodity_groups
ALL_COMMODITIES = _DOMAIN.all_commodities
PRODUCT_LINE_CODES = _DOMAIN.line_codes


def normalize_part_no(raw: str | None) -> str:
    """料號標準化：去頭尾空白、全形轉半形、轉大寫、破折號轉 '-'。

    只做「格式上的」標準化，不做任何字元猜測（例如 O→0）。
    """
    if raw is None:
        return ""
    s = unicodedata.normalize("NFKC", str(raw)).strip().upper()
    for ch in _DASHES:
        s = s.replace(ch, "-")
    return s


def part_type_of(part_no: str) -> str | None:
    """依料號格式判斷類型（FG/SA/RM）；不符任何格式回傳 None（無效料號）。"""
    for t, pat in PART_PATTERNS.items():
        m = pat.match(part_no)
        if m:
            if t == "RM" and m.group("commodity") not in ALL_COMMODITIES:
                return None
            if t == "FG" and m.group("line") not in _DOMAIN.product_lines:
                return None
            return t
    return None


def product_line_of_fg(part_no: str) -> str | None:
    """產品線一律以成品料號判定（料號中的產品線字母 → 產品線代碼）。"""
    m = PART_PATTERNS["FG"].match(part_no or "")
    if not m:
        return None
    pl = _DOMAIN.product_lines.get(m.group("line"))
    return pl["code"] if pl else None


def commodity_of(part_no: str) -> str | None:
    """RM 的物料類別碼取自料號（domain.json 的 commodity 群組）。"""
    m = PART_PATTERNS["RM"].match(part_no or "")
    return m.group("commodity") if m and m.group("commodity") in ALL_COMMODITIES else None


# ---------------------------------------------------------------- 工廠
PLANTS = {code: (v["name"], v.get("aliases", [])) for code, v in _DOMAIN.plants.items()}
_PLANT_LOOKUP: dict[str, str] = {}
for _code, (_name, _aliases) in PLANTS.items():
    for _a in [_code, _name, *_aliases]:
        _PLANT_LOOKUP[re.sub(r"\s+", "", unicodedata.normalize("NFKC", _a)).upper()] = _code


def normalize_plant(raw: str | None) -> str | None:
    """工廠代碼標準化：不分大小寫、去除空白後比對標準代碼與別名；不在表內回傳 None。"""
    if raw is None:
        return None
    key = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(raw))).upper()
    return _PLANT_LOOKUP.get(key)


# ---------------------------------------------------------------- 日期
_DATE_FORMATS = [
    (re.compile(r"^\d{4}-\d{2}-\d{2}$"), "%Y-%m-%d"),
    (re.compile(r"^\d{4}/\d{2}/\d{2}$"), "%Y/%m/%d"),
    (re.compile(r"^\d{8}$"), "%Y%m%d"),
]


def parse_date(raw: str | None) -> date | None:
    """只接受 YYYY-MM-DD、YYYY/MM/DD、YYYYMMDD；其他寫法（即使可推測）一律無效。"""
    if raw is None:
        return None
    s = str(raw).strip()
    for pat, fmt in _DATE_FORMATS:
        if pat.match(s):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------- 允許值
def match_allowed(raw: str | None, allowed: list[str], case_insensitive: bool) -> str | None:
    """允許值比對：先去頭尾空白；欄位說明寫明不分大小寫者才忽略大小寫。

    清單以外的寫法（例如 OK、PASS、核准中）一律回傳 None，不自行對應。
    """
    if raw is None:
        return None
    s = str(raw).strip()
    for a in allowed:
        if s == a or (case_insensitive and s.lower() == a.lower()):
            return a
    return None


# ---------------------------------------------------------------- 數字
_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")


def parse_number(raw: str | None) -> float | None:
    """嚴格解析數字（不接受 '約500'、'8週'、'TBD'、'x2' 等寫法）。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if not _NUM_RE.match(s):
        return None
    return float(s)


def parse_positive_int(raw: str | None) -> int | None:
    """正整數（LeadTimeDays、MOQ）。"""
    v = parse_number(raw)
    if v is None or v <= 0 or v != int(v):
        return None
    return int(v)
