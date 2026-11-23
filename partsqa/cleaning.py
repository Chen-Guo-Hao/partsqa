"""資料擷取與清理管線（任務 3）。

輸入：PQA_DATA_DIR 下的六個原始 CSV（唯讀，不修改）。
輸出：PQA_CLEAN_DB（SQLite），內含清理後資料表、待人工確認清單與問題統計；
      另輸出 clean_stats.json 供產生「資料清理說明報告」。

設計原則（依 data_dictionary.md）：
- 每列都保留「狀態」(ok / invalid / pending) 與問題代碼，問答時才能說明「排除了哪些資料、幾筆」。
- 完全重複列只保留一筆（不寫入資料庫）。
- 不推估、不補值（損耗率例外）；無法判定的值視為缺值。
- 可重複執行（idempotent）：每次都從原始 CSV 重建 clean DB；人工審核結果存在另一個 app DB，不會被覆蓋。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

from .attributes import ATTR_CODES, comparable_key, normalize_attr_code, parse_attr
from .domain import get_domain
from .normalize import (
    PRODUCT_LINE_CODES,
    commodity_of,
    match_allowed,
    normalize_part_no,
    normalize_plant,
    parse_date,
    parse_number,
    parse_positive_int,
    part_type_of,
    product_line_of_fg,
)
from .units import bom_qty_to_master, stock_qty_to_master

log = logging.getLogger(__name__)

# 邏輯名稱 → 實際檔名（config/domain.json 的 files）
FN = get_domain().files
LOGICAL = ["parts_master", "part_attributes", "bom", "alternates", "inventory", "open_po"]
SOURCE_FILES = [FN[k] for k in LOGICAL]

# 可疑的「給 AI 的指令」樣式：只做標記與報告，不執行
_INJECTION_RE = re.compile(r"忽略.*規則|系統通知|ignore (all|previous)|視為\s*Approved|直接.*回覆", re.I)


@dataclass
class IssueLog:
    """問題統計收集器：每個問題記錄檔案、代碼、說明、處理方式、筆數、範例。"""

    items: list[dict] = field(default_factory=list)

    def add(self, file: str, code: str, desc: str, handling: str, count: int, examples: list | None = None) -> None:
        if count <= 0:
            return
        ex = [str(e) for e in (examples or [])][:5]
        self.items.append({"file": file, "code": code, "desc": desc, "handling": handling, "count": int(count), "examples": ex})


def _key(*parts: object) -> str:
    """穩定的雜湊鍵（用於人工審核紀錄，重跑清理後仍能對應）。"""
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


def _read_csv(path: Path) -> pd.DataFrame:
    """以字串讀入，避免 pandas 自動轉型造成資訊遺失；缺值以空字串表示。"""
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8")


def _drop_raw_dups(df: pd.DataFrame, file: str, issues: IssueLog) -> pd.DataFrame:
    """所有欄位完全相同的列只保留一筆。"""
    dup = df.duplicated()
    issues.add(file, "DUP_EXACT", "所有欄位完全相同的重複列", "只保留一筆", int(dup.sum()))
    return df[~dup].reset_index(drop=True)


def _iso_week_monday(datecode: str) -> date | None:
    """DateCode(YYWW) → 該 ISO 週星期一；週別在該 ISO 年不存在者無效。"""
    s = str(datecode).strip()
    if not re.fullmatch(r"\d{4}", s):
        return None
    yy, ww = int(s[:2]), int(s[2:])
    if not 1 <= ww <= 53:
        return None
    try:
        return date.fromisocalendar(2000 + yy, ww, 1)
    except ValueError:
        return None


# ====================================================================== parts_master
def clean_parts(df: pd.DataFrame, issues: IssueLog) -> tuple[pd.DataFrame, list[dict]]:
    """料號主檔清理；回傳 (清理後主檔, 待人工確認項目)。"""
    f = FN["parts_master"]
    df = _drop_raw_dups(df, f, issues)
    out = pd.DataFrame()
    out["part_no"] = df["PartNo"].map(normalize_part_no)
    changed = (out["part_no"] != df["PartNo"])
    issues.add(f, "PARTNO_FORMAT", "料號含頭尾空白／全形／小寫／特殊破折號", "依第 3 節標準化", int(changed.sum()),
               df.loc[changed, "PartNo"].head(5).tolist())
    out["fmt_type"] = out["part_no"].map(part_type_of)
    out["part_desc"] = df["PartDesc"].str.strip()
    out["part_type"] = df["PartType"].map(lambda v: match_allowed(v, ["FG", "SA", "RM"], False))
    out["commodity"] = df["Commodity"].str.strip().replace("", None)
    out["product_line_raw"] = df["ProductLine"].map(lambda v: match_allowed(v, PRODUCT_LINE_CODES, False))
    out["uom"] = df["UoM"].map(lambda v: {"EA": "PCS"}.get(str(v).strip(), match_allowed(v, ["PCS", "M"], False)))
    out["lifecycle"] = df["Lifecycle"].map(lambda v: match_allowed(v, ["Active", "NRND", "EOL", "Obsolete"], True))
    out["make_buy"] = df["MakeBuy"].map(lambda v: match_allowed(v, ["M", "B"], False))
    out["supplier_name"] = df["SupplierName"].str.strip().replace("", None)
    out["mfr_part_no"] = df["MfrPartNo"].str.strip().replace("", None)
    out["lead_time_days"] = df["LeadTimeDays"].map(parse_positive_int)
    out["moq"] = df["MOQ"].map(parse_positive_int)
    cost = df["UnitCostUSD"].map(parse_number)
    out["unit_cost_usd"] = cost.where(cost > 0)

    invalid = out["fmt_type"].isna() | (out["part_type"].notna() & (out["fmt_type"] != out["part_type"]))
    issues.add(f, "PARTNO_INVALID", "標準化後仍不符格式的無效料號（如 O/0、I/1 混用、位數錯）",
               "排除該列，不猜測修正", int(invalid.sum()), df.loc[invalid, "PartNo"].head(5).tolist())
    out = out[~invalid].copy()
    out["part_type"] = out["fmt_type"]

    # 欄位值不合法 → 該欄視為缺值（逐欄統計）
    for col, raw, label in [
        ("uom", "UoM", "UoM 不在允許值（PCS/M/EA，大小寫不符亦屬無效）"),
        ("lifecycle", "Lifecycle", "Lifecycle 不在允許值（如 量產、停產、Phase-out）"),
        ("lead_time_days", "LeadTimeDays", "LeadTimeDays 非正整數（如 -14、8週、空白）"),
        ("unit_cost_usd", "UnitCostUSD", "UnitCostUSD 非大於 0 的數字（如 -1.25、0、TBD、N/A）"),
    ]:
        raw_vals = df.loc[out.index, raw]
        # 有填但不合法，或 RM 必填卻空白 → 計為問題
        bad = out[col].isna() & ((raw_vals.str.strip() != "") | (out["part_type"] == "RM"))
        issues.add(f, f"INVALID_{raw.upper()}", label, "該欄視為缺值；計算需要時排除並說明", int(bad.sum()),
                   raw_vals[bad].head(5).tolist())

    # 產品線一律以成品料號判定
    out["product_line"] = out["product_line_raw"]
    fg = out["part_type"] == "FG"
    derived = out.loc[fg, "part_no"].map(product_line_of_fg)
    mismatch = fg & (out["product_line_raw"] != out["part_no"].map(product_line_of_fg))
    issues.add(f, "PRODUCTLINE_MISMATCH", "成品主檔 ProductLine 與成品料號的產品線代碼不一致", "以料號為準",
               int(mismatch.sum()), (out.loc[mismatch, "part_no"] + " 主檔=" + out.loc[mismatch, "product_line_raw"].astype(str)).tolist())
    out.loc[fg, "product_line"] = derived
    out["commodity"] = out.apply(lambda r: commodity_of(r["part_no"]) if r["part_type"] == "RM" else None, axis=1)
    out = out.drop(columns=["fmt_type", "product_line_raw"])

    # 標準化後內容完全相同 → 重複
    content_cols = [c for c in out.columns]
    dup2 = out.astype(str).duplicated()
    issues.add(f, "DUP_AFTER_NORM", "標準化後內容完全相同的重複列", "只保留一筆", int(dup2.sum()))
    out = out[~dup2].copy()

    # 同料號多筆內容不同 → 待人工確認
    out["status"] = "ok"
    out["review_key"] = None
    reviews: list[dict] = []
    multi = out["part_no"].duplicated(keep=False)
    for pn, grp in out[multi].groupby("part_no"):
        diff_cols = [c for c in content_cols if grp[c].astype(str).nunique() > 1]
        for idx, row in grp.iterrows():
            rk = _key("parts", *[row[c] for c in content_cols])
            out.at[idx, "status"] = "pending"
            out.at[idx, "review_key"] = rk
            reviews.append({
                "review_key": rk, "item_type": "PART_MASTER_CONFLICT", "group_key": pn,
                "commodity": row["commodity"] or "", "part_no": pn,
                "summary": f"料號 {pn} 主檔有多筆內容不同（差異欄位：{', '.join(diff_cols)}）；本筆："
                           + ", ".join(f"{c}={row[c]}" for c in diff_cols),
            })
    n_conf = out.loc[out["status"] == "pending", "part_no"].nunique()
    issues.add(f, "MASTER_CONFLICT", "同一料號有多筆內容不同的主檔紀錄", "列為待人工確認（R4）；確認前該料號在其他檔案的相關列也暫不使用",
               n_conf, sorted(out.loc[out["status"] == "pending", "part_no"].unique())[:5])
    return out.reset_index(drop=True), reviews


# ====================================================================== part_attributes
def clean_attributes(df: pd.DataFrame, parts: pd.DataFrame, issues: IssueLog) -> tuple[pd.DataFrame, list[dict]]:
    f = FN["part_attributes"]
    df = _drop_raw_dups(df, f, issues)
    known = set(parts["part_no"])
    pending_parts = set(parts.loc[parts["status"] == "pending", "part_no"])
    comm = parts.drop_duplicates("part_no").set_index("part_no")["commodity"].to_dict()

    rows = []
    n_bad_code = n_orphan = n_invalid_pn = 0
    bad_values: dict[str, list] = defaultdict(list)
    for r in df.itertuples(index=False):
        pn = normalize_part_no(r.PartNo)
        if part_type_of(pn) is None:
            n_invalid_pn += 1
            continue
        if pn not in known:
            n_orphan += 1
            continue
        code = normalize_attr_code(r.AttrCode)
        if code is None:
            n_bad_code += 1
            continue
        val = parse_attr(code, r.AttrValue, r.Unit)
        if val is None:
            bad_values[code].append(f"{r.AttrValue}{r.Unit}")
        rows.append({"part_no": pn, "attr_code": code, "raw_value": f"{r.AttrValue}|{r.Unit}",
                     "value": None if val is None else str(val), "is_num": isinstance(val, float)})
    issues.add(f, "PARTNO_INVALID", "無效料號", "排除", n_invalid_pn)
    issues.add(f, "ORPHAN", "料號主檔中不存在的料號（孤兒資料）", "排除", n_orphan)
    issues.add(f, "ATTRCODE_INVALID", "未定義的屬性代碼", "排除", n_bad_code)
    case_fix = int((df["AttrCode"].str.strip() != df["AttrCode"]).sum() + (~df["AttrCode"].str.strip().isin(ATTR_CODES)).sum())
    issues.add(f, "ATTRCODE_CASE", "AttrCode 大小寫或空白不一致（如 rohs、' RoHS'）", "不分大小寫、去空白後對應標準代碼", case_fix)
    n_bad_val = sum(len(v) for v in bad_values.values())
    issues.add(f, "ATTRVALUE_INVALID", "無法換算的屬性值（TBD、N/A、待確認、-、RoHS 寫成 是/Yes、單位與屬性不符）",
               "視為缺值；A7 比對時主料有、替代料缺值視為不符", n_bad_val,
               [f"{k}:{v[0]}" for k, v in bad_values.items()][:5])
    unit_conv = int((df["Unit"].str.strip() != "").sum())
    issues.add(f, "UNIT_CONVERT", "屬性值帶單位或 SI 字首（pF/nF/kΩ/kohm/mW/℃/Unicode 減號…）", "換算為第 6 節標準單位", unit_conv)

    a = pd.DataFrame(rows)
    a = a[a["value"].notna()].copy()
    a["cmp"] = [comparable_key(c, v) for c, v in zip(a["attr_code"], a["value"], strict=True)]
    # 換算後相同 → 重複；不同 → 待人工確認（確認前視為缺值）
    dup = a.duplicated(subset=["part_no", "attr_code", "cmp"])
    issues.add(f, "DUP_AFTER_CONVERT", "同料號同屬性多筆，換算後相同", "視為重複，保留一筆", int(dup.sum()))
    a = a[~dup].copy()
    a["status"] = "ok"
    a["review_key"] = None
    reviews = []
    multi = a.duplicated(subset=["part_no", "attr_code"], keep=False)
    for (pn, code), grp in a[multi].groupby(["part_no", "attr_code"]):
        vals = grp["value"].tolist()
        for idx, row in grp.iterrows():
            rk = _key("attr", pn, code, row["value"])
            a.at[idx, "status"] = "pending"
            a.at[idx, "review_key"] = rk
            reviews.append({"review_key": rk, "item_type": "ATTRIBUTE_CONFLICT", "group_key": f"{pn}|{code}",
                            "commodity": comm.get(pn) or "", "part_no": pn,
                            "summary": f"{pn} 的 {code} 有多個不同值 {vals}；核准本筆值 = {row['value']}"})
    n_conf = a.loc[a["status"] == "pending"].groupby(["part_no", "attr_code"]).ngroups
    issues.add(f, "ATTR_CONFLICT", "同料號同屬性多筆且換算後不同", "列為待人工確認，確認前該屬性視為缺值", n_conf,
               (a.loc[a["status"] == "pending", "part_no"] + ":" + a.loc[a["status"] == "pending", "attr_code"]).unique()[:5].tolist())
    blocked = a["part_no"].isin(pending_parts)
    issues.add(f, "MASTER_PENDING_REF", "料號主檔待人工確認的料號之屬性", "確認前暫不使用（問答時動態判斷）", int(blocked.sum()))
    return a.drop(columns=["cmp"]).reset_index(drop=True), reviews


# ====================================================================== bom
def _parse_scrap(raw: str) -> tuple[float, str | None]:
    """損耗率：空白=0；帶 % 換算；無 % 視為小數；不在 0~0.2 或無法判讀 → 0 並回報問題代碼。"""
    s = str(raw).strip()
    if s == "":
        return 0.0, None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*%", s)
    if m:
        v = float(m.group(1)) / 100
        return (v, "SCRAP_PERCENT") if 0 <= v <= 0.2 else (0.0, "SCRAP_OUT_OF_RANGE")
    v = parse_number(s)
    if v is None:
        return 0.0, "SCRAP_UNREADABLE"
    if not 0 <= v <= 0.2:
        return 0.0, "SCRAP_OUT_OF_RANGE"
    return v, None


def clean_bom(df: pd.DataFrame, parts: pd.DataFrame, issues: IssueLog) -> tuple[pd.DataFrame, list[dict]]:
    f = FN["bom"]
    df = _drop_raw_dups(df, f, issues)
    master = parts.drop_duplicates("part_no").set_index("part_no")
    master_ok = parts[parts["status"] == "ok"].set_index("part_no")

    recs = []
    for r in df.itertuples(index=False):
        reasons = []
        parent, child = normalize_part_no(r.ParentPartNo), normalize_part_no(r.ChildPartNo)
        pt, ct = part_type_of(parent), part_type_of(child)
        if pt not in {"FG", "SA"}:
            reasons.append("PARENT_INVALID")
        elif parent not in master.index:
            reasons.append("PARENT_ORPHAN")
        if ct not in {"SA", "RM"}:
            reasons.append("CHILD_INVALID")
        elif child not in master.index:
            reasons.append("CHILD_ORPHAN")
        qty = parse_number(r.QtyPer)
        if qty is None or qty <= 0:
            reasons.append("QTY_INVALID")
        uom = str(r.UoM).strip().upper()
        if child in master_ok.index and qty is not None and qty > 0:
            if bom_qty_to_master(qty, uom, master_ok.at[child, "uom"]) is None:
                reasons.append("UOM_INCONVERTIBLE")
        scrap, scrap_issue = _parse_scrap(r.ScrapRate)
        eff_from, eff_to = parse_date(r.EffectiveFrom), parse_date(r.EffectiveTo)
        if eff_from is None:
            reasons.append("DATE_INVALID")
        if str(r.EffectiveTo).strip() and eff_to is None:
            reasons.append("DATE_INVALID")
        if eff_from and eff_to and eff_to < eff_from:
            reasons.append("DATE_REVERSED")
        line_no = parse_positive_int(r.LineNo)
        recs.append({
            "parent": parent, "bom_rev": str(r.BOMRev).strip(), "line_no": line_no if line_no else str(r.LineNo).strip(),
            "child": child, "qty_per": qty, "uom": uom, "scrap_rate": scrap, "scrap_issue": scrap_issue,
            "scrap_raw": str(r.ScrapRate), "ref_des": str(r.RefDes).strip(),
            "eff_from": eff_from.isoformat() if eff_from else None, "eff_to": eff_to.isoformat() if eff_to else None,
            "raw_child": r.ChildPartNo, "raw_qty": r.QtyPer, "raw_from": r.EffectiveFrom,
            "issues": ";".join(dict.fromkeys(reasons)),
        })
    b = pd.DataFrame(recs)
    b["status"] = b["issues"].map(lambda s: "invalid" if s else "ok")

    labels = {
        "PARENT_INVALID": "上階料號無效", "PARENT_ORPHAN": "上階料號不在主檔（孤兒）",
        "CHILD_INVALID": "下階料號無效（字母 O 與數字 0 混用、位數不符、非法分隔符號）", "CHILD_ORPHAN": "下階料號不在主檔（孤兒）",
        "QTY_INVALID": "QtyPer 不是大於 0 的數字（空白、0、-1、1..2、TBD）",
        "UOM_INCONVERTIBLE": "用量單位無法換算為下階主檔單位（如主檔 PCS、BOM 為 M）",
        "DATE_INVALID": "日期格式不合法（如 01/01/2024、01-Jan-2024）", "DATE_REVERSED": "失效日早於生效日",
    }
    for code, label in labels.items():
        m = b["issues"].str.contains(code)
        ex = (b.loc[m, "parent"] + "/" + b.loc[m, "raw_child"].astype(str) + " qty=" + b.loc[m, "raw_qty"].astype(str)
              + " from=" + b.loc[m, "raw_from"].astype(str)).head(5).tolist()
        issues.add(f, code, label, "該行為無效行，不展開", int(m.sum()), ex)
    for code, label in [("SCRAP_PERCENT", "損耗率以百分比表示（如 2%）"),
                        ("SCRAP_OUT_OF_RANGE", "損耗率不在 0～0.2（如 5、15；無 % 一律視為小數）"),
                        ("SCRAP_UNREADABLE", "損耗率無法判讀（如 abc）")]:
        m = b["scrap_issue"] == code
        handling = "換算為小數" if code == "SCRAP_PERCENT" else "以 0 計算（第 8 節唯一允許的預設值）"
        issues.add(f, code, label, handling, int(m.sum()), b.loc[m, "scrap_raw"].head(5).tolist())
    norm_fmt = int((df["ChildPartNo"].map(normalize_part_no) != df["ChildPartNo"]).sum())
    issues.add(f, "PARTNO_FORMAT", "下階料號含空白／全形／小寫／特殊破折號", "依第 3 節標準化", norm_fmt)
    alt_fmt = int(df["EffectiveFrom"].str.fullmatch(r"\d{8}|\d{4}/\d{2}/\d{2}").sum())
    issues.add(f, "DATE_ALT_FORMAT", "日期為 YYYYMMDD 或 YYYY/MM/DD", "屬允許格式，轉為 ISO 日期", alt_fmt)
    pending_parts = set(parts.loc[parts["status"] == "pending", "part_no"])
    pm = (b["status"] == "ok") & (b["parent"].isin(pending_parts) | b["child"].isin(pending_parts))
    issues.add(f, "MASTER_PENDING_REF", "上階或下階料號主檔待人工確認", "確認前暫不展開（問答時動態判斷）", int(pm.sum()),
               (b.loc[pm, "parent"] + "/" + b.loc[pm, "child"]).head(5).tolist())

    # 標準化後內容完全相同 → 重複
    content = ["parent", "bom_rev", "line_no", "child", "qty_per", "uom", "scrap_rate", "ref_des", "eff_from", "eff_to"]
    dup = b.duplicated(subset=content + ["status"])
    issues.add(f, "DUP_AFTER_NORM", "標準化後內容完全相同的重複行", "只保留一筆", int(dup.sum()))
    b = b[~dup].copy()

    reviews: list[dict] = []
    master_comm = master["commodity"].to_dict()
    b["review_key"] = None
    # 同一上階、版次、行號卻內容不同 → 待人工確認（只看有效行）
    ok = b[b["status"] == "ok"]
    conflict_idx = ok[ok.duplicated(subset=["parent", "bom_rev", "line_no"], keep=False)].index
    for idx in conflict_idx:
        row = b.loc[idx]
        rk = _key("bom", *[row[c] for c in content])
        b.at[idx, "status"] = "pending"
        b.at[idx, "issues"] = "LINE_CONFLICT"
        b.at[idx, "review_key"] = rk
        reviews.append({"review_key": rk, "item_type": "BOM_LINE_CONFLICT", "group_key": f"{row['parent']}|{row['bom_rev']}|{row['line_no']}",
                        "commodity": master_comm.get(row["child"]) or "", "part_no": row["child"],
                        "summary": f"BOM {row['parent']} 版次 {row['bom_rev']} 行 {row['line_no']} 有多筆不同內容；本筆：下階 {row['child']} 用量 {row['qty_per']} {row['uom']} 位置 {row['ref_des']} 生效 {row['eff_from']}~{row['eff_to'] or ''}"})
    issues.add(f, "LINE_CONFLICT", "同一上階、版次、行號但內容不同", "列為待人工確認，確認前不展開",
               len(conflict_idx), (b.loc[conflict_idx, "parent"] + " 行" + b.loc[conflict_idx, "line_no"].astype(str)).head(5).tolist())

    # 循環參照偵測（以有效行建圖，找出強連通元件內的邊）
    ok = b[b["status"] == "ok"]
    cyc = _cycle_edges(list(zip(ok["parent"], ok["child"], strict=True)))
    cyc_idx = ok[[(p, c) in cyc for p, c in zip(ok["parent"], ok["child"], strict=True)]].index
    for idx in cyc_idx:
        row = b.loc[idx]
        rk = _key("bomcycle", *[row[c] for c in content])
        b.at[idx, "status"] = "pending"
        b.at[idx, "issues"] = "CYCLE"
        b.at[idx, "review_key"] = rk
        reviews.append({"review_key": rk, "item_type": "BOM_CYCLE", "group_key": f"{row['parent']}|{row['child']}",
                        "commodity": master_comm.get(row["child"]) or "", "part_no": row["child"],
                        "summary": f"BOM 循環參照：{row['parent']} → {row['child']}（行 {row['line_no']}）"})
    issues.add(f, "CYCLE", "形成循環參照的 BOM 行", "列為待人工確認，不得展開", len(cyc_idx),
               (b.loc[cyc_idx, "parent"] + "→" + b.loc[cyc_idx, "child"]).head(5).tolist())
    b["line_no"] = b["line_no"].astype(str)
    return b.drop(columns=["raw_child", "raw_qty", "raw_from", "scrap_raw"]).reset_index(drop=True), reviews


def _cycle_edges(edges: list[tuple[str, str]]) -> set[tuple[str, str]]:
    """Tarjan 強連通元件：回傳位於循環上的邊。"""
    graph: dict[str, list[str]] = defaultdict(list)
    for p, c in edges:
        graph[p].append(c)
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    comp: dict[str, int] = {}
    counter = [0, 0]

    def strongconnect(v: str) -> None:
        # 迭代版，避免遞迴深度問題
        work = [(v, iter(graph.get(v, [])))]
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        while work:
            node, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter[0]
                    counter[0] += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(graph.get(w, []))))
                    advanced = True
                    break
                if w in on_stack:
                    low[node] = min(low[node], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp[w] = counter[1]
                    if w == node:
                        break
                counter[1] += 1

    for v in list(graph):
        if v not in index:
            strongconnect(v)
    sizes = defaultdict(int)
    for c in comp.values():
        sizes[c] += 1
    return {(p, c) for p, c in edges if p == c or (comp.get(p) == comp.get(c) and sizes[comp[p]] > 1)}


# ====================================================================== alternates
def clean_alternates(df: pd.DataFrame, parts: pd.DataFrame, issues: IssueLog) -> tuple[pd.DataFrame, list[dict]]:
    f = FN["alternates"]
    df = _drop_raw_dups(df, f, issues)
    master = parts.drop_duplicates("part_no").set_index("part_no")
    recs = []
    injections = []
    for r in df.itertuples(index=False):
        reasons = []
        prim, alt = normalize_part_no(r.PrimaryPartNo), normalize_part_no(r.AltPartNo)
        for pn, tag in [(prim, "PRIMARY"), (alt, "ALT")]:
            if part_type_of(pn) != "RM":
                reasons.append(f"{tag}_INVALID")
            elif pn not in master.index:
                reasons.append(f"{tag}_ORPHAN")
        if prim == alt:
            reasons.append("SAME_PART")
        if not re.fullmatch(r"ALT\d{7}", str(r.AltRecID).strip()):
            reasons.append("ALTRECID_INVALID")
        scope = match_allowed(r.Scope, ["GLOBAL", "PRODUCTLINE", "PARENT"], False)
        sv_raw = str(r.ScopeValue).strip()
        sv = None
        if scope is None:
            reasons.append("SCOPE_INVALID")
        elif scope == "GLOBAL":
            if sv_raw:
                reasons.append("SCOPEVALUE_INVALID")
        elif scope == "PRODUCTLINE":
            sv = match_allowed(sv_raw, PRODUCT_LINE_CODES, False)
            if sv is None:
                reasons.append("SCOPEVALUE_INVALID")
        else:
            sv = normalize_part_no(sv_raw)
            if part_type_of(sv) not in {"FG", "SA"} or sv not in master.index:
                reasons.append("SCOPEVALUE_INVALID")
        prio = parse_number(r.Priority)
        if prio is None:
            reasons.append("PRIORITY_INVALID")
        ratio = parse_number(r.QtyRatio)
        if ratio is None or ratio <= 0:
            reasons.append("QTYRATIO_INVALID")
        bidir = "N" if str(r.Bidirectional).strip() == "" else match_allowed(r.Bidirectional, ["Y", "N"], False)
        mix = "N" if str(r.MixAllowed).strip() == "" else match_allowed(r.MixAllowed, ["Y", "N"], False)
        if bidir is None or mix is None:
            reasons.append("YN_INVALID")
        bundle = str(r.BundleID).strip()
        if bundle and not re.fullmatch(r"BND-\d{4}", bundle):
            reasons.append("BUNDLE_INVALID")
        appr = match_allowed(r.ApprovalStatus, ["Approved", "Pending", "Rejected"], True)
        if appr is None:
            reasons.append("APPROVAL_INVALID")
        vf, vt = parse_date(r.ValidFrom), parse_date(r.ValidTo)
        if vf is None or (str(r.ValidTo).strip() and vt is None):
            reasons.append("DATE_INVALID")
        if vf and vt and vt < vf:
            reasons.append("DATE_REVERSED")
        remark = str(r.Remark).strip()
        if remark and _INJECTION_RE.search(remark):
            injections.append(f"{r.AltRecID}: {remark}")
        recs.append({
            "alt_rec_id": str(r.AltRecID).strip(), "primary_part": prim, "alt_part": alt, "scope": scope or str(r.Scope).strip(),
            "scope_value": sv if sv is not None else (sv_raw or None), "priority": prio, "qty_ratio": ratio,
            "bidirectional": bidir, "mix_allowed": mix, "bundle_id": bundle or None, "approval_status": appr,
            "approval_raw": str(r.ApprovalStatus), "valid_from": vf.isoformat() if vf else None,
            "valid_to": vt.isoformat() if vt else None, "remark": remark or None,
            "remark_flag": "SUSPECT_INSTRUCTION" if remark and _INJECTION_RE.search(remark) else None,
            "issues": ";".join(dict.fromkeys(reasons)), "raw_ratio": r.QtyRatio, "raw_sv": r.ScopeValue,
        })
    a = pd.DataFrame(recs)
    a["status"] = a["issues"].map(lambda s: "invalid" if s else "ok")
    labels = {
        "PRIMARY_INVALID": "主料料號無效", "ALT_INVALID": "替代料料號無效", "PRIMARY_ORPHAN": "主料不在主檔（孤兒）",
        "ALT_ORPHAN": "替代料不在主檔（孤兒）", "SAME_PART": "主料與替代料相同", "ALTRECID_INVALID": "AltRecID 格式不符",
        "SCOPE_INVALID": "Scope 不在允許值", "SCOPEVALUE_INVALID": "範圍值無效（不在允許的產品線代碼、不存在或非上階的料號）",
        "PRIORITY_INVALID": "Priority 非數字", "QTYRATIO_INVALID": "QtyRatio 無效（空白、0、-1、x2）",
        "YN_INVALID": "Bidirectional/MixAllowed 非 Y/N", "BUNDLE_INVALID": "BundleID 格式不符",
        "APPROVAL_INVALID": "ApprovalStatus 不在允許值（如 核准中、OK、Approve?）",
        "DATE_INVALID": "有效期間日期格式不合法", "DATE_REVERSED": "ValidTo 早於 ValidFrom",
    }
    for code, label in labels.items():
        m = a["issues"].str.contains(code)
        ex = (a.loc[m, "alt_rec_id"] + " ratio=" + a.loc[m, "raw_ratio"].astype(str) + " sv=" + a.loc[m, "raw_sv"].astype(str)
              + " appr=" + a.loc[m, "approval_raw"]).head(5).tolist()
        issues.add(f, code, label, "無效紀錄，不可使用", int(m.sum()), ex)
    case_fix = int(((a["approval_status"].notna()) & (a["approval_raw"] != a["approval_status"])).sum())
    issues.add(f, "APPROVAL_CASE", "ApprovalStatus 大小寫／空白不一（如 approved、' Approved'、'APPROVED '）", "不分大小寫對應標準值", case_fix)
    slash = int(df["ValidFrom"].str.contains("/").sum())
    issues.add(f, "DATE_ALT_FORMAT", "ValidFrom 為 YYYY/MM/DD", "屬允許格式，轉為 ISO 日期", slash)
    pfmt = int(((df["PrimaryPartNo"].map(normalize_part_no) != df["PrimaryPartNo"]) | (df["AltPartNo"].map(normalize_part_no) != df["AltPartNo"])).sum())
    issues.add(f, "PARTNO_FORMAT", "主料/替代料料號含空白、全形、小寫或特殊破折號", "依第 3 節標準化", pfmt)
    issues.add(f, "REMARK_INJECTION", "備註欄含看似「給 AI 的指令」的文字（例如要求忽略規則、直接視為 Approved）",
               "視為一般資料，不執行；Remark 不具核准效力，核准狀態仍以 ApprovalStatus 為準", len(injections), injections)
    issues.add(f, "REMARK_NO_EFFECT", "備註欄有內容", "依規則僅供參考，不影響可否使用", int(a["remark"].notna().sum()))

    # 標準化後完全相同（不含 AltRecID 與 Remark）→ 重複
    key_cols = ["primary_part", "alt_part", "scope", "scope_value"]
    content = key_cols + ["priority", "qty_ratio", "bidirectional", "mix_allowed", "bundle_id", "approval_status", "valid_from", "valid_to"]
    dup = a.duplicated(subset=content + ["status"])
    issues.add(f, "DUP_AFTER_NORM", "標準化後內容相同的替代料紀錄（不同 AltRecID 或僅備註不同）", "只保留一筆", int(dup.sum()))
    a = a[~dup].copy()

    reviews: list[dict] = []
    comm = master["commodity"].to_dict()
    a["review_key"] = None
    ok = a[a["status"] == "ok"]
    conf_idx = ok[ok.duplicated(subset=key_cols, keep=False)].index
    for idx in conf_idx:
        row = a.loc[idx]
        rk = _key("altconf", row["alt_rec_id"], *[row[c] for c in content])
        a.at[idx, "status"] = "pending"
        a.at[idx, "issues"] = "RECORD_CONFLICT"
        a.at[idx, "review_key"] = rk
        reviews.append({"review_key": rk, "item_type": "ALT_RECORD_CONFLICT", "group_key": "|".join(str(row[c]) for c in key_cols),
                        "commodity": comm.get(row["primary_part"]) or "", "part_no": row["primary_part"],
                        "summary": f"替代料 {row['primary_part']}→{row['alt_part']}（{row['scope']} {row['scope_value'] or ''}）有多筆不同內容；本筆 {row['alt_rec_id']}：狀態 {row['approval_status']}、比 {row['qty_ratio']}、優先 {row['priority']}、期間 {row['valid_from']}~{row['valid_to'] or ''}"})
    issues.add(f, "RECORD_CONFLICT", "同主料、替代料、範圍與範圍值有多筆內容不同的紀錄", "列為待人工確認",
               len(conf_idx), a.loc[conf_idx, "alt_rec_id"].head(5).tolist())
    # Pending 替代料 → 須由主料類別的 R4 核准
    pend = a[(a["status"] == "ok") & (a["approval_status"] == "Pending")]
    for idx, row in pend.iterrows():
        rk = _key("altpending", row["alt_rec_id"], row["primary_part"], row["alt_part"], row["scope"], row["scope_value"])
        a.at[idx, "review_key"] = rk
        reviews.append({"review_key": rk, "item_type": "ALT_PENDING", "group_key": row["alt_rec_id"],
                        "commodity": comm.get(row["primary_part"]) or "", "part_no": row["primary_part"],
                        "summary": f"Pending 替代料 {row['alt_rec_id']}：{row['primary_part']} → {row['alt_part']}（{row['scope']} {row['scope_value'] or ''}，比 {row['qty_ratio']}，期間 {row['valid_from']}~{row['valid_to'] or ''}）"})
    issues.add(f, "ALT_PENDING", "ApprovalStatus = Pending 的替代料", "列入待人工確認；須由主料所屬類別的 R4 核准後才可使用", len(pend))
    rej = int(((a["status"] == "ok") & (a["approval_status"] == "Rejected")).sum())
    issues.add(f, "ALT_REJECTED", "ApprovalStatus = Rejected", "保留紀錄但不可使用", rej)
    return a.drop(columns=["raw_ratio", "raw_sv"]).reset_index(drop=True), reviews


# ====================================================================== inventory
def clean_inventory(df: pd.DataFrame, parts: pd.DataFrame, issues: IssueLog) -> pd.DataFrame:
    f = FN["inventory"]
    df = _drop_raw_dups(df, f, issues)
    master = parts[parts["status"] == "ok"].set_index("part_no")
    known = set(parts["part_no"])
    recs = []
    for r in df.itertuples(index=False):
        reasons = []
        plant = normalize_plant(r.Plant)
        if plant is None:
            reasons.append("PLANT_INVALID")
        pn = normalize_part_no(r.PartNo)
        if part_type_of(pn) != "RM":
            reasons.append("PARTNO_INVALID")
        elif pn not in known:
            reasons.append("ORPHAN")
        qc = match_allowed(r.QCStatus, ["合格", "待驗", "凍結", "MRB"], False)
        if qc is None:
            reasons.append("QC_INVALID")
        elif qc != "合格":
            reasons.append("QC_NOT_PASSED")
        onhand = parse_number(r.QtyOnHand)
        alloc = 0.0 if str(r.QtyAllocated).strip() == "" else parse_number(r.QtyAllocated)
        if onhand is None or onhand < 0:
            reasons.append("QTY_INVALID")
        elif alloc is None or alloc < 0 or alloc > onhand:
            reasons.append("ALLOC_INVALID")
        uom = str(r.UoM).strip().upper()
        avail = None
        if onhand is not None and onhand >= 0 and alloc is not None and 0 <= alloc <= onhand and pn in master.index:
            avail = stock_qty_to_master(onhand - alloc, uom, master.at[pn, "uom"])
            if avail is None:
                reasons.append("UOM_MISMATCH")
        mfg = _iso_week_monday(r.DateCode)
        recs.append({
            "plant": plant, "plant_raw": str(r.Plant), "part_no": pn, "lot_no": str(r.LotNo).strip(),
            "date_code": str(r.DateCode).strip(), "mfg_date": mfg.isoformat() if mfg else None,
            "qc_status": qc or str(r.QCStatus).strip(), "qty_on_hand": onhand, "qty_allocated": alloc,
            "uom": uom, "qty_available": avail, "storage_loc": str(r.StorageLoc).strip(),
            "issues": ";".join(dict.fromkeys(reasons)), "raw_qty": r.QtyOnHand,
        })
    inv = pd.DataFrame(recs)
    labels = {
        "PLANT_INVALID": ("工廠代碼無效（不在工廠清單的代碼）", "排除"),
        "PARTNO_INVALID": ("料號無效", "排除"), "ORPHAN": ("料號不在主檔（孤兒）", "排除"),
        "QC_INVALID": ("QCStatus 不在允許值（OK、PASS、已驗）", "視為無效，不計入可用庫存"),
        "QC_NOT_PASSED": ("QCStatus 為待驗／凍結／MRB", "不計入可用庫存"),
        "QTY_INVALID": ("在庫量為負數或非數字（如 -109、約500、N/A、空白）", "無效批次，排除"),
        "ALLOC_INVALID": ("保留量大於在庫量", "無效批次，排除"),
        "UOM_MISMATCH": ("單位換算後與主檔不一致", "排除"),
    }
    for code, (label, handling) in labels.items():
        m = inv["issues"].str.contains(code)
        ex = (inv.loc[m, "lot_no"] + " plant=" + inv.loc[m, "plant_raw"] + " qc=" + inv.loc[m, "qc_status"]
              + " qty=" + inv.loc[m, "raw_qty"].astype(str)).head(5).tolist()
        issues.add(f, code, label, handling, int(m.sum()), ex)
    alias = inv["plant"].notna() & (inv["plant"] != inv["plant_raw"])
    issues.add(f, "PLANT_ALIAS", "工廠以別名、中文名或小寫表示（代碼省略連字號、中文名稱、小寫）", "對應為標準代碼", int(alias.sum()),
               inv.loc[alias, "plant_raw"].unique()[:5].tolist())
    kp = int((df["UoM"].str.strip() == "KPCS").sum())
    issues.add(f, "UOM_KPCS", "庫存單位為 KPCS", "× 1,000 換算為 PCS", kp)
    bad_dc = inv["mfg_date"].isna()
    issues.add(f, "DATECODE_INVALID", "DateCode 無效（非 YYWW 四位數、含字母、空白、該年不存在的週）",
               "保留；若料號有 ShelfLifeMonths 則無法確認效期 → 不可用", int(bad_dc.sum()), inv.loc[bad_dc, "date_code"].head(5).tolist())
    pfmt = int((df["PartNo"].map(normalize_part_no) != df["PartNo"]).sum())
    issues.add(f, "PARTNO_FORMAT", "料號含空白／全形／小寫／特殊破折號", "依第 3 節標準化", pfmt)
    inv["status"] = inv["issues"].map(lambda s: "invalid" if s else "ok")
    dup = inv.duplicated(subset=[c for c in inv.columns if c not in {"plant_raw", "raw_qty", "issues", "status"}] + ["status"])
    issues.add(f, "DUP_AFTER_NORM", "標準化後內容相同的重複批次", "只保留一筆", int(dup.sum()))
    inv = inv[~dup]
    return inv.drop(columns=["raw_qty"]).reset_index(drop=True)


# ====================================================================== open_po
def clean_open_po(df: pd.DataFrame, parts: pd.DataFrame, issues: IssueLog) -> pd.DataFrame:
    f = FN["open_po"]
    df = _drop_raw_dups(df, f, issues)
    master = parts[parts["status"] == "ok"].set_index("part_no")
    known = set(parts["part_no"])
    recs = []
    for r in df.itertuples(index=False):
        reasons = []
        if not re.fullmatch(r"PO\d{8}", str(r.PONo).strip()):
            reasons.append("PONO_INVALID")
        plant = normalize_plant(r.Plant)
        if plant is None:
            reasons.append("PLANT_INVALID")
        pn = normalize_part_no(r.PartNo)
        if part_type_of(pn) != "RM":
            reasons.append("PARTNO_INVALID")
        elif pn not in known:
            reasons.append("ORPHAN")
        status = match_allowed(r.Status, ["Open", "Closed", "Cancelled"], True)
        if status is None:
            reasons.append("STATUS_INVALID")
        oq, rq = parse_number(r.OrderQty), parse_number(r.ReceivedQty)
        open_qty = None
        if oq is None or rq is None or oq < 0 or rq < 0 or rq > oq:
            reasons.append("QTY_INVALID")
        else:
            open_qty = oq - rq
        uom = str(r.UoM).strip().upper()
        if open_qty is not None and pn in master.index:
            conv = stock_qty_to_master(open_qty, uom, master.at[pn, "uom"])
            if conv is None:
                reasons.append("UOM_MISMATCH")
            open_qty = conv
        od, eta = parse_date(r.OrderDate), parse_date(r.ETA)
        if od is None:
            reasons.append("ORDERDATE_INVALID")
        if eta is None:
            reasons.append("ETA_INVALID")
        elif od is not None and eta < od:
            reasons.append("ETA_BEFORE_ORDER")
        price = parse_number(r.UnitPriceUSD)
        recs.append({
            "po_no": str(r.PONo).strip(), "po_line": str(r.POLine).strip(), "plant": plant, "plant_raw": str(r.Plant),
            "part_no": pn, "supplier_name": str(r.SupplierName).strip() or None, "order_qty": oq, "received_qty": rq,
            "uom": uom, "open_qty": open_qty, "unit_price_usd": price if price and price > 0 else None,
            "order_date": od.isoformat() if od else None, "eta": eta.isoformat() if eta else None,
            "status": status or str(r.Status).strip(), "issues": ";".join(dict.fromkeys(reasons)),
            "raw_eta": r.ETA,
        })
    po = pd.DataFrame(recs)
    labels = {
        "PONO_INVALID": ("採購單號格式不符", "排除"), "PLANT_INVALID": ("收貨工廠無效", "排除"),
        "PARTNO_INVALID": ("料號無效", "排除"), "ORPHAN": ("料號不在主檔（孤兒）", "排除"),
        "STATUS_INVALID": ("Status 不在允許值（?、Hold、部分到貨）", "視為無效，不計入在途"),
        "QTY_INVALID": ("已收量大於訂購量或數量非數字", "排除"), "UOM_MISMATCH": ("單位與主檔不一致", "排除"),
        "ORDERDATE_INVALID": ("下單日無效", "排除"), "ETA_INVALID": ("ETA 無效（TBD、空白、非允許格式）", "不計入在途"),
        "ETA_BEFORE_ORDER": ("ETA 早於下單日（日期矛盾）", "無效資料，一律排除"),
    }
    for code, (label, handling) in labels.items():
        m = po["issues"].str.contains(code)
        ex = (po.loc[m, "po_no"] + "-" + po.loc[m, "po_line"] + " status=" + po.loc[m, "status"] + " eta=" + po.loc[m, "raw_eta"].astype(str)
              + " od=" + po.loc[m, "order_date"].astype(str)).head(5).tolist()
        issues.add(f, code, label, handling, int(m.sum()), ex)
    alias = po["plant"].notna() & (po["plant"] != po["plant_raw"])
    issues.add(f, "PLANT_ALIAS", "工廠以別名或中文名表示", "對應為標準代碼", int(alias.sum()))
    case_fix = int((po["status"].isin(["Open", "Closed", "Cancelled"]) & (df["Status"].values != po["status"].values)).sum())
    issues.add(f, "STATUS_CASE", "Status 大小寫／空白不一（OPEN、' Open'、cancelled）", "不分大小寫對應標準值", case_fix)
    alt_fmt = int(df["ETA"].str.fullmatch(r"\d{8}|\d{4}/\d{2}/\d{2}").sum())
    issues.add(f, "DATE_ALT_FORMAT", "ETA 為 YYYYMMDD 或 YYYY/MM/DD", "屬允許格式，轉為 ISO 日期", alt_fmt)
    pfmt = int((df["PartNo"].map(normalize_part_no) != df["PartNo"]).sum())
    issues.add(f, "PARTNO_FORMAT", "料號含空白／全形／小寫／特殊破折號", "依第 3 節標準化", pfmt)
    nprice = int(po["unit_price_usd"].isna().sum())
    issues.add(f, "PRICE_INVALID", "UnitPriceUSD 非大於 0", "單價視為缺值（不影響在途數量）", nprice)
    po["row_status"] = po["issues"].map(lambda s: "invalid" if s else "ok")
    dup = po.duplicated(subset=[c for c in po.columns if c not in {"plant_raw", "raw_eta"}])
    issues.add(f, "DUP_AFTER_NORM", "標準化後內容相同的重複採購單項次", "只保留一筆", int(dup.sum()))
    po = po[~dup]
    return po.drop(columns=["raw_eta"]).reset_index(drop=True)


# ====================================================================== 主流程
def run_cleaning(data_dir: Path, out_db: Path, report_dir: Path) -> dict:
    """執行完整清理流程，回傳統計資訊（同時寫入 clean DB 與 clean_stats.json）。"""
    t0 = time.time()
    missing = [f for f in SOURCE_FILES if not (data_dir / f).exists()]
    if missing:
        raise FileNotFoundError(f"找不到原始資料檔：{missing}（請確認 PQA_DATA_DIR={data_dir}）")
    issues = IssueLog()
    raw = {f: _read_csv(data_dir / f) for f in SOURCE_FILES}
    log.info("讀入原始資料：%s", {k: len(v) for k, v in raw.items()})

    parts, rv_parts = clean_parts(raw[FN["parts_master"]], issues)
    attrs, rv_attrs = clean_attributes(raw[FN["part_attributes"]], parts, issues)
    bom, rv_bom = clean_bom(raw[FN["bom"]], parts, issues)
    alts, rv_alts = clean_alternates(raw[FN["alternates"]], parts, issues)
    inv = clean_inventory(raw[FN["inventory"]], parts, issues)
    po = clean_open_po(raw[FN["open_po"]], parts, issues)
    reviews = pd.DataFrame(rv_parts + rv_attrs + rv_bom + rv_alts)

    counts = {
        FN["parts_master"]: _count(raw[FN["parts_master"]], parts, "status"),
        FN["part_attributes"]: _count(raw[FN["part_attributes"]], attrs, "status"),
        FN["bom"]: _count(raw[FN["bom"]], bom, "status"),
        FN["alternates"]: _count(raw[FN["alternates"]], alts, "status"),
        FN["inventory"]: _count(raw[FN["inventory"]], inv, "status"),
        FN["open_po"]: _count(raw[FN["open_po"]], po, "row_status"),
    }
    out_db.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_db.with_suffix(".tmp.db")
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(tmp)
    try:
        parts.to_sql("parts", con, index=False)
        attrs.to_sql("attributes", con, index=False)
        bom.to_sql("bom", con, index=False)
        alts.to_sql("alternates", con, index=False)
        inv.to_sql("inventory", con, index=False)
        po.to_sql("open_po", con, index=False)
        reviews.to_sql("review_items", con, index=False)
        pd.DataFrame(issues.items).assign(examples=lambda d: d["examples"].map(json.dumps, na_action="ignore")).to_sql("clean_issues", con, index=False)
        for sql in [
            "CREATE INDEX ix_parts ON parts(part_no)", "CREATE INDEX ix_attr ON attributes(part_no)",
            "CREATE INDEX ix_bom_p ON bom(parent)", "CREATE INDEX ix_bom_c ON bom(child)",
            "CREATE INDEX ix_alt_p ON alternates(primary_part)", "CREATE INDEX ix_alt_a ON alternates(alt_part)",
            "CREATE INDEX ix_inv ON inventory(part_no, plant)", "CREATE INDEX ix_po ON open_po(part_no, plant)",
        ]:
            con.execute(sql)
        con.commit()
    finally:
        con.close()  # Windows 上必須先關閉連線才能替換檔案
    tmp.replace(out_db)  # 原子替換，失敗時不破壞既有資料庫

    stats = {"run_at": time.strftime("%Y-%m-%d %H:%M:%S"), "elapsed_s": round(time.time() - t0, 1),
             "counts": counts, "issues": issues.items, "review_items": int(len(reviews)),
             "review_by_type": reviews["item_type"].value_counts().to_dict() if len(reviews) else {}}
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "clean_stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    return stats


def _count(raw: pd.DataFrame, clean: pd.DataFrame, status_col: str) -> dict:
    vc = clean[status_col].value_counts().to_dict()
    return {"raw_rows": int(len(raw)), "raw_columns": list(raw.columns), "stored_rows": int(len(clean)),
            "ok": int(vc.get("ok", 0)), "invalid": int(vc.get("invalid", 0)), "pending": int(vc.get("pending", 0)),
            "clean_columns": list(clean.columns)}
