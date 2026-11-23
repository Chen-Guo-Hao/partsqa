"""材料屬性解析與換算（data_dictionary.md 第 6 節）。

每個屬性換算成標準單位：
  Capacitance→µF、Resistance→Ω、Inductance→µH、RatedVoltage→V、RatedCurrent→A、
  PowerRating→W、Tolerance→%、TempMin/TempMax→°C、ShelfLifeMonths→月。
無法換算（TBD、N/A、單位與屬性不符…）回傳 None，視為缺值。
"""

from __future__ import annotations

import re
import unicodedata

ATTR_CODES = [
    "Capacitance", "Resistance", "Inductance", "RatedVoltage", "RatedCurrent", "PowerRating",
    "Tolerance", "TempMin", "TempMax", "MSL", "ShelfLifeMonths", "Package", "RoHS", "SafetyCert",
]
_ATTR_LOOKUP = {c.lower(): c for c in ATTR_CODES}

# 數值型屬性：(SI 單位符號, 標準單位相對於 SI 的倍率)
_SI_ATTRS = {
    "Capacitance": ("F", 1e-6),   # 標準 µF
    "Resistance": ("Ω", 1.0),     # 標準 Ω
    "Inductance": ("H", 1e-6),    # 標準 µH
    "RatedVoltage": ("V", 1.0),
    "RatedCurrent": ("A", 1.0),
    "PowerRating": ("W", 1.0),
}
_PREFIX = {"": 1.0, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6}
_MISSING_TOKENS = {"", "TBD", "N/A", "NA", "-", "待確認", "NULL", "NONE"}


def normalize_attr_code(raw: str) -> str | None:
    """屬性代碼不分大小寫、去頭尾空白。"""
    return _ATTR_LOOKUP.get(str(raw).strip().lower())


def _clean_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", str(s)).strip()
    # NFKC 會把 µ(U+00B5) 轉成 μ(U+03BC)、℃ 轉成 °C；Unicode 減號轉 '-'
    return s.replace("−", "-").replace("﹣", "-")


def _round(v: float) -> float:
    """消除浮點誤差，方便比對相等（保留 12 位有效數字）。"""
    return float(f"{v:.12g}")


def parse_attr(code: str, value: str, unit: str) -> float | str | None:
    """依屬性代碼解析值；回傳標準單位數值（或文字）；無法判讀回傳 None。"""
    v = _clean_text(value)
    u = _clean_text(unit)
    if v.upper() in _MISSING_TOKENS:
        return None
    text = (v + u).replace(" ", "")

    if code in _SI_ATTRS:
        symbol, std_factor = _SI_ATTRS[code]
        t = re.sub(r"(?i)ohm", "Ω", text.replace("Ω", "Ω"))
        m = re.match(r"^(-?\d+(?:\.\d+)?)([pnuµμmkKM]?)(F|Ω|H|V|A|W)?$", t)
        if not m:
            return None
        num, prefix, sym = float(m.group(1)), m.group(2), m.group(3)
        if sym is None:
            if prefix == "":
                return _round(num)  # 只有數字 → 視為標準單位
            # 電阻只寫 k/K/M 視為 kΩ/MΩ；其他屬性只有字首無單位 → 無法判讀
            if code != "Resistance" or prefix not in {"k", "K", "M"}:
                return None
        elif sym != symbol:
            return None  # 單位與屬性不符
        if prefix == "K" and sym is not None and code != "Resistance":
            return None
        return _round(num * _PREFIX[prefix] / std_factor)

    if code in {"TempMin", "TempMax"}:
        m = re.match(r"^(-?\d+(?:\.\d+)?)(°C|C)?$", text)
        return _round(float(m.group(1))) if m else None

    if code == "Tolerance":
        m = re.match(r"^±?(\d+(?:\.\d+)?)%?$", text)
        return _round(float(m.group(1))) if m else None

    if code == "ShelfLifeMonths":
        m = re.match(r"^(\d+(?:\.\d+)?)(月|months?|M)?$", text, flags=re.I)
        if not m or float(m.group(1)) <= 0:
            return None
        return _round(float(m.group(1)))

    if code == "MSL":
        m = re.match(r"^(\d[A-Za-z]?)$", text)
        return m.group(1).upper() if m else None

    if code == "RoHS":
        s = v.strip().upper()
        return s if s in {"Y", "N"} else None  # 其他寫法（是、Yes）視為缺值

    if code == "Package":
        return v.strip() or None

    if code == "SafetyCert":
        items = [i for i in (x.strip() for x in v.split(";")) if i]
        return ";".join(items) if items else None
    return None


def package_key(pkg: str) -> str:
    """封裝比對鍵：忽略大小寫、空白與 '-'。"""
    return re.sub(r"[\s\-]", "", str(pkg)).upper()


def cert_set(certs: str) -> set[str]:
    """認證清單比對：以 ';' 分隔，忽略大小寫與空白。"""
    return {re.sub(r"\s+", "", c).upper() for c in str(certs).split(";") if c.strip()}


def comparable_key(code: str, val: float | str) -> str:
    """同料號同屬性多筆時判斷是否「換算後相同」所用的鍵。"""
    if code == "Package":
        return package_key(str(val))
    if code == "SafetyCert":
        return ";".join(sorted(cert_set(str(val))))
    return str(val)
