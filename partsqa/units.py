"""單位換算（data_dictionary.md 第 8 節第 2 點、第 10 節第 1 點）。"""

from __future__ import annotations

# 用量單位 → (換算後的主檔單位, 倍率)
_BOM_UOM = {
    "PCS": ("PCS", 1.0),
    "EA": ("PCS", 1.0),
    "KPCS": ("PCS", 1000.0),
    "M": ("M", 1.0),
    "CM": ("M", 0.01),
    "MM": ("M", 0.001),
}
# 庫存／採購單位
_STOCK_UOM = {"PCS": ("PCS", 1.0), "KPCS": ("PCS", 1000.0), "M": ("M", 1.0)}


def bom_qty_to_master(qty: float, bom_uom: str, master_uom: str | None) -> float | None:
    """BOM 用量換算為下階主檔單位；無法換算（例如主檔 PCS、BOM 為 M）回傳 None。"""
    conv = _BOM_UOM.get(str(bom_uom).strip().upper())
    if conv is None or master_uom is None or conv[0] != master_uom:
        return None
    return qty * conv[1]


def stock_qty_to_master(qty: float, uom: str, master_uom: str | None) -> float | None:
    """庫存／在途數量換算為主檔單位（KPCS × 1,000）；與主檔不一致回傳 None。"""
    conv = _STOCK_UOM.get(str(uom).strip().upper())
    if conv is None or master_uom is None or conv[0] != master_uom:
        return None
    return qty * conv[1]
