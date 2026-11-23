"""確定性計算引擎：BOM 展開、反查上階、可用庫存、在途、替代料判斷、缺料試算。

所有數字都由這裡的程式計算（不交給 LLM 算），結果附帶「依據」：
套用的規則、被排除的資料與筆數，讓問答機器人能說明來源。

本模組不處理權限；權限過濾在 tools.py（工具層）統一執行。
"""

from __future__ import annotations

import math
import sqlite3
from calendar import monthrange
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from .attributes import cert_set, package_key
from .normalize import product_line_of_fg
from .units import bom_qty_to_master

EPS = 1e-9


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s) if isinstance(s, str) and s else None


def ceil_qty(q: float, uom: str) -> float:
    """PCS 無條件進位到整數；M 無條件進位到小數第二位（先消除浮點誤差）。"""
    q = round(q, 9)
    if uom == "M":
        return math.ceil(q * 100 - EPS) / 100
    return float(math.ceil(q - EPS))


def add_months(d: date, months: int) -> date:
    """加月份；該月無同一天時取月底（第 10 節規則）。"""
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, monthrange(y, m)[1]))


@dataclass
class Position:
    """BOM 展開後的一個原材料位置（某個直接上階上的某一行）。"""

    rm: str
    parent: str
    line_no: str
    ref_des: str
    qty_per_parent: float  # 每 1 個上階的用量（已換算單位、已含本行損耗）
    parent_units_per_root: float  # 每 1 個根成品需要幾個此上階（含上層損耗）
    path: list[str]

    @property
    def qty_per_root(self) -> float:
        return self.qty_per_parent * self.parent_units_per_root


@dataclass
class Explosion:
    root: str
    prod_date: date
    positions: list[Position] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    excluded_examples: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    def requirement_per_root(self) -> dict[str, float]:
        req: dict[str, float] = defaultdict(float)
        for p in self.positions:
            req[p.rm] += p.qty_per_root
        return dict(req)


class Engine:
    """載入清理後資料庫（再疊加 R4 人工審核結果）後提供各種查詢與計算。"""

    def __init__(self, clean_db: Path, approved_keys: set[str] | None = None, rejected_keys: set[str] | None = None):
        if not Path(clean_db).exists():
            raise FileNotFoundError(f"清理後資料庫不存在：{clean_db}（請先由 R5 執行資料清理）")
        con = sqlite3.connect(clean_db)
        try:
            self.parts_df = pd.read_sql("select * from parts", con)
            self.attrs_df = pd.read_sql("select * from attributes", con)
            self.bom_df = pd.read_sql("select * from bom", con)
            self.alt_df = pd.read_sql("select * from alternates", con)
            self.inv_df = pd.read_sql("select * from inventory", con)
            self.po_df = pd.read_sql("select * from open_po", con)
            self.review_df = pd.read_sql("select * from review_items", con)
        finally:
            con.close()
        # pandas 的 NaN 一律轉成 None，避免 NaN 被當成「有值」
        for name in ["parts_df", "attrs_df", "bom_df", "alt_df", "inv_df", "po_df", "review_df"]:
            df = getattr(self, name)
            setattr(self, name, df.astype(object).where(df.notna(), None))
        self.approved = approved_keys or set()
        self.rejected = rejected_keys or set()
        self._build()

    # ------------------------------------------------------------------ 建立索引（疊加人工審核）
    def _build(self) -> None:
        # 主檔：ok 直接用；pending（衝突）只有被核准的那筆候選可用，否則料號整體封鎖
        self.parts: dict[str, dict] = {}
        self.blocked_parts: set[str] = set()
        for r in self.parts_df.to_dict("records"):
            if r["status"] == "ok":
                self.parts[r["part_no"]] = r
        for pn, grp in self.parts_df[self.parts_df["status"] == "pending"].groupby("part_no"):
            chosen = [r for r in grp.to_dict("records") if r["review_key"] in self.approved]
            if chosen:
                self.parts[pn] = chosen[0]
            else:
                self.blocked_parts.add(pn)
        self.all_part_nos = set(self.parts_df["part_no"])

        # 屬性：ok + 被核准的候選值
        self.attrs: dict[str, dict[str, str]] = defaultdict(dict)
        for r in self.attrs_df.to_dict("records"):
            if r["status"] == "ok" or (r["status"] == "pending" and r["review_key"] in self.approved):
                if r["part_no"] not in self.blocked_parts:
                    self.attrs[r["part_no"]][r["attr_code"]] = r["value"]

        # BOM：保留全部行（含無效）以便說明排除原因
        self.bom_by_parent: dict[str, list[dict]] = defaultdict(list)
        self.bom_by_child: dict[str, list[dict]] = defaultdict(list)
        for r in self.bom_df.to_dict("records"):
            if r["status"] == "pending" and r["review_key"] in self.approved:
                r = {**r, "status": "ok", "issues": "APPROVED_BY_R4"}
            self.bom_by_parent[r["parent"]].append(r)
            self.bom_by_child[r["child"]].append(r)

        # 替代料
        self.alts: list[dict] = []
        for r in self.alt_df.to_dict("records"):
            r = dict(r)
            if r["status"] == "pending":
                r["status"] = "ok" if r["review_key"] in self.approved else "pending"
            if r["status"] == "ok" and r["approval_status"] == "Pending" and r["review_key"] in self.approved:
                r["approval_status"] = "Approved"
                r["approved_by_review"] = True
            if r["status"] == "ok" and r["approval_status"] == "Pending" and r["review_key"] in self.rejected:
                r["approval_status"] = "Rejected"
            self.alts.append(r)
        self.alts_by_primary: dict[str, list[dict]] = defaultdict(list)
        self.alts_by_alt: dict[str, list[dict]] = defaultdict(list)
        self.alts_by_bundle: dict[str, list[dict]] = defaultdict(list)
        for r in self.alts:
            self.alts_by_primary[r["primary_part"]].append(r)
            self.alts_by_alt[r["alt_part"]].append(r)
            if r.get("bundle_id"):
                self.alts_by_bundle[r["bundle_id"]].append(r)

        self.inv_by_part: dict[str, list[dict]] = defaultdict(list)
        for r in self.inv_df.to_dict("records"):
            self.inv_by_part[r["part_no"]].append(r)
        self.po_by_part: dict[str, list[dict]] = defaultdict(list)
        for r in self.po_df.to_dict("records"):
            self.po_by_part[r["part_no"]].append(r)

    # ------------------------------------------------------------------ 基本查詢
    def part(self, pn: str) -> dict | None:
        return self.parts.get(pn)

    def part_status(self, pn: str) -> str:
        """ok / blocked（主檔待人工確認）/ not_found。"""
        if pn in self.parts:
            return "ok"
        if pn in self.blocked_parts:
            return "blocked"
        return "not_found"

    def fg_list(self) -> list[str]:
        return sorted(p for p, r in self.parts.items() if r["part_type"] == "FG")

    # ------------------------------------------------------------------ BOM 展開
    def _line_usable(self, line: dict, prod_date: date) -> str | None:
        """回傳排除原因代碼；None 表示可用。"""
        if line["status"] == "invalid":
            return "無效行(" + line["issues"] + ")"
        if line["status"] == "pending":
            return "待人工確認(" + line["issues"] + ")"
        ef, et = _d(line["eff_from"]), _d(line["eff_to"])
        if ef is None or prod_date < ef or (et is not None and prod_date > et):
            return "生產日不在生效區間"
        for pn in (line["parent"], line["child"]):
            if pn in self.blocked_parts:
                return "主檔待人工確認"
        child = self.parts.get(line["child"])
        if child is None:
            return "下階料號不在主檔"
        if bom_qty_to_master(line["qty_per"], line["uom"], child["uom"]) is None:
            return "單位無法換算"
        return None

    def explode(self, root: str, prod_date: date) -> Explosion:
        """多階展開到原材料（SA 不留庫存）；記錄每個位置與排除的行數。"""
        ex = Explosion(root=root, prod_date=prod_date)

        def walk(node: str, units: float, path: list[str], depth: int) -> None:
            if depth > 20:
                ex.excluded["展開層數過深（疑似循環）"] += 1
                return
            for line in self.bom_by_parent.get(node, []):
                reason = self._line_usable(line, prod_date)
                if reason:
                    # 版次已失效的整批舊行數量多，只計數不列範例
                    ex.excluded[reason] += 1
                    if reason != "生產日不在生效區間" and len(ex.excluded_examples[reason]) < 5:
                        ex.excluded_examples[reason].append(f"{line['parent']} 行{line['line_no']} → {line['child']}")
                    continue
                child = self.parts[line["child"]]
                qty = bom_qty_to_master(line["qty_per"], line["uom"], child["uom"]) * (1 + line["scrap_rate"])
                if child["part_type"] == "SA":
                    walk(line["child"], units * qty, [*path, line["child"]], depth + 1)
                else:
                    ex.positions.append(Position(rm=line["child"], parent=node, line_no=str(line["line_no"]),
                                                 ref_des=line["ref_des"] or "", qty_per_parent=qty,
                                                 parent_units_per_root=units, path=path))

        walk(root, 1.0, [root], 0)
        return ex

    def single_level(self, parent: str, prod_date: date) -> dict:
        """單階 BOM（含排除說明）。"""
        rows, excluded = [], defaultdict(int)
        for line in self.bom_by_parent.get(parent, []):
            reason = self._line_usable(line, prod_date)
            if reason:
                excluded[reason] += 1
                continue
            child = self.parts[line["child"]]
            rows.append({"line_no": line["line_no"], "child": line["child"], "child_type": child["part_type"],
                         "desc": child["part_desc"], "qty_per": line["qty_per"], "uom": line["uom"],
                         "scrap_rate": line["scrap_rate"], "ref_des": line["ref_des"]})
        return {"parent": parent, "lines": rows, "excluded_lines": dict(excluded)}

    def where_used(self, part: str, prod_date: date) -> dict:
        """反查上階：直接上階與最終成品（只走在生產日有效的行）。"""
        direct: dict[str, dict] = {}
        excluded = defaultdict(int)
        for line in self.bom_by_child.get(part, []):
            reason = self._line_usable(line, prod_date)
            if reason:
                excluded[reason] += 1
                continue
            direct[line["parent"]] = {"parent": line["parent"], "line_no": line["line_no"], "qty_per": line["qty_per"],
                                      "ref_des": line["ref_des"]}
        fgs: dict[str, set[str]] = defaultdict(set)  # FG → 經過的直接上階

        def up(node: str, origin: str, depth: int) -> None:
            if depth > 20:
                return
            if self.parts.get(node, {}).get("part_type") == "FG":
                fgs[node].add(origin)
                return
            for line in self.bom_by_child.get(node, []):
                if self._line_usable(line, prod_date) is None:
                    up(line["parent"], origin, depth + 1)

        for p in direct:
            up(p, p, 0)
        return {"part": part, "direct_parents": list(direct.values()),
                "finished_goods": {fg: sorted(v) for fg, v in sorted(fgs.items())}, "excluded_lines": dict(excluded)}

    def fg_ancestors(self, node: str, prod_date: date | None = None) -> set[str]:
        """某料號往上追到的所有成品（prod_date=None 時不看生效日，只看有效行）。"""
        out: set[str] = set()
        seen: set[str] = set()
        stack = [node]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            if self.parts.get(n, {}).get("part_type") == "FG":
                out.add(n)
                continue
            for line in self.bom_by_child.get(n, []):
                ok = line["status"] == "ok" if prod_date is None else self._line_usable(line, prod_date) is None
                if ok:
                    stack.append(line["parent"])
        return out

    def descendants(self, root: str) -> set[str]:
        """不分日期，所有有效行可達的下階料號（用於 R2 權限範圍）。"""
        out: set[str] = set()
        stack = [root]
        while stack:
            n = stack.pop()
            for line in self.bom_by_parent.get(n, []):
                if line["status"] == "ok" and line["child"] not in out:
                    out.add(line["child"])
                    stack.append(line["child"])
        return out

    # ------------------------------------------------------------------ 庫存
    def shelf_life(self, part: str) -> float | None:
        v = self.attrs.get(part, {}).get("ShelfLifeMonths")
        return float(v) if v is not None else None

    def available_stock(self, part: str, plant: str, prod_date: date) -> dict:
        """可用庫存 = Σ(在庫 − 保留)，只計：有效工廠、合格、單位一致、未過期（有保存期限者）。"""
        total, used = 0.0, 0
        excluded: dict[str, list] = defaultdict(lambda: [0, 0.0])
        shelf = self.shelf_life(part)
        for r in self.inv_by_part.get(part, []):
            if r["plant"] != plant:
                continue
            issues = r["issues"] or ""
            reason = None
            if "QC_NOT_PASSED" in issues:
                reason = f"品檢狀態 {r['qc_status']}"
            elif "QC_INVALID" in issues:
                reason = "品檢狀態不合法(" + r["qc_status"] + ")"
            elif issues:
                reason = "無效批次(" + issues + ")"
            elif part in self.blocked_parts:
                reason = "主檔待人工確認"
            elif shelf is not None:
                mfg = _d(r["mfg_date"])
                if mfg is None:
                    reason = "DateCode 無效，無法確認效期"
                elif add_months(mfg, int(shelf)) < prod_date:
                    reason = "已過期（到期日早於生產日）"
            if reason:
                excluded[reason][0] += 1
                if r["qty_on_hand"] is not None and not math.isnan(r["qty_on_hand"]):
                    excluded[reason][1] += (r["qty_on_hand"] or 0)
                continue
            total += r["qty_available"]
            used += 1
        return {"part": part, "plant": plant, "available": round(total, 4), "lots_used": used,
                "shelf_life_months": shelf,
                "excluded_lots": {k: {"lots": v[0], "qty_on_hand": round(v[1], 2)} for k, v in excluded.items()}}

    def in_transit(self, part: str, plant: str, prod_date: date) -> dict:
        """在途 = Status=Open 的未交量，且 ETA + 3 天 ≤ 生產日；ETA 早於下單日者排除。"""
        total = 0.0
        pos: list[dict] = []
        excluded: dict[str, int] = defaultdict(int)
        late: list[dict] = []
        for r in self.po_by_part.get(part, []):
            if r["plant"] != plant:
                continue
            if part in self.blocked_parts:
                excluded["主檔待人工確認"] += 1
                continue
            issues = r["issues"] or ""
            if "ETA_BEFORE_ORDER" in issues:
                excluded["ETA 早於下單日（日期矛盾）"] += 1
                continue
            if "STATUS_INVALID" in issues:
                excluded[f"狀態不合法({r['status']})"] += 1
                continue
            if r["status"] != "Open":
                excluded[f"狀態 {r['status']}"] += 1
                continue
            if "ETA_INVALID" in issues:
                excluded["ETA 無效"] += 1
                continue
            if issues:
                excluded["無效資料(" + issues + ")"] += 1
                continue
            if not r["open_qty"]:
                excluded["未交量為 0"] += 1
                continue
            eta = _d(r["eta"])
            rec = {"po_no": r["po_no"], "po_line": r["po_line"], "open_qty": r["open_qty"], "eta": r["eta"],
                   "supplier_name": r["supplier_name"], "unit_price_usd": r["unit_price_usd"]}
            if eta + timedelta(days=3) <= prod_date:
                total += r["open_qty"]
                pos.append(rec)
            else:
                excluded["ETA+3 天晚於生產日（來不及）"] += 1
                late.append(rec)
        return {"part": part, "plant": plant, "in_transit_in_time": round(total, 4), "po_lines_in_time": pos,
                "po_lines_too_late": late, "excluded_po_lines": dict(excluded)}

    # ------------------------------------------------------------------ 替代料判斷（A1–A12）
    def _attr_check(self, replaced: str, substitute: str, ratio: float) -> list[str]:
        """A7 屬性相容（替代料相對於被取代料）；回傳不符原因清單。"""
        a, b = self.attrs.get(replaced, {}), self.attrs.get(substitute, {})
        fails = []

        def num(d: dict, k: str) -> float | None:
            return float(d[k]) if k in d else None

        for k in ["RatedVoltage", "RatedCurrent", "PowerRating", "TempMax"]:
            if k in a:
                if num(b, k) is None:
                    fails.append(f"{k} 替代料缺值")
                elif num(b, k) < num(a, k) - EPS:
                    fails.append(f"{k} 較低({b[k]} < {a[k]})")
        if "TempMin" in a:
            if num(b, "TempMin") is None:
                fails.append("TempMin 替代料缺值")
            elif num(b, "TempMin") > num(a, "TempMin") + EPS:
                fails.append(f"TempMin 較高({b['TempMin']} > {a['TempMin']})")
        if "Tolerance" in a:
            if num(b, "Tolerance") is None:
                fails.append("Tolerance 替代料缺值")
            elif num(b, "Tolerance") > num(a, "Tolerance") + EPS:
                fails.append(f"Tolerance 較大({b['Tolerance']} > {a['Tolerance']})")
        if abs(ratio - 1) < EPS:
            for k in ["Capacitance", "Resistance", "Inductance"]:
                if k in a:
                    if num(b, k) is None:
                        fails.append(f"{k} 替代料缺值")
                    elif abs(num(b, k) - num(a, k)) > 1e-9 * max(1, abs(num(a, k))):
                        fails.append(f"{k} 不同({b[k]} vs {a[k]})")
        if "Package" in a:
            if "Package" not in b:
                fails.append("Package 替代料缺值")
            elif package_key(b["Package"]) != package_key(a["Package"]):
                fails.append(f"Package 不同({b['Package']} vs {a['Package']})")
        # RoHS：替代料一律須為 Y（缺值或 N 都不符）
        if b.get("RoHS") != "Y":
            fails.append("替代料 RoHS 不是 Y" if "RoHS" in b else "RoHS 替代料缺值")
        if "SafetyCert" in a:
            if "SafetyCert" not in b:
                fails.append("SafetyCert 替代料缺值")
            else:
                missing = cert_set(a["SafetyCert"]) - cert_set(b["SafetyCert"])
                if missing:
                    fails.append(f"SafetyCert 未涵蓋 {sorted(missing)}")
        return fails

    def evaluate_record(self, rec: dict, replaced: str, parent: str | None, fgs: set[str] | None,
                        prod_date: date, check_bundle: bool = True) -> dict:
        """判斷一筆替代料紀錄能否用在「成品 → 直接上階 → 被取代料」的位置。

        replaced：BOM 上的料號；若 replaced 是紀錄的 alt_part 則為反向使用（A3）。
        fgs：此位置所屬成品集合（用於 PRODUCTLINE 範圍）；None 表示未指定成品。
        """
        reverse = rec["alt_part"] == replaced and rec["primary_part"] != replaced
        substitute = rec["primary_part"] if reverse else rec["alt_part"]
        fails: list[str] = []
        notes: list[str] = []
        if rec["status"] == "invalid":
            fails.append("A11 無效紀錄(" + (rec["issues"] or "") + ")")
        elif rec["status"] == "pending":
            fails.append("A11 同鍵多筆內容不同，待人工確認")
        # A1 核准狀態
        st = rec["approval_status"]
        if st == "Pending":
            fails.append("A1 ApprovalStatus=Pending（須主料類別 R4 核准）")
        elif st == "Rejected":
            fails.append("A1 ApprovalStatus=Rejected")
        elif st != "Approved":
            fails.append(f"A1 核准狀態無效({rec.get('approval_raw')})")
        if rec.get("approved_by_review"):
            notes.append("Pending 已由 R4 於人工審核核准")
        # A2 有效期間
        vf, vt = _d(rec["valid_from"]), _d(rec["valid_to"])
        if vf is None or prod_date < vf or (vt is not None and prod_date > vt):
            fails.append(f"A2 生產日 {prod_date} 不在有效期間 {rec['valid_from']}~{rec['valid_to'] or '無期限'}")
        # A3 方向
        ratio = rec["qty_ratio"] or 0
        if reverse:
            if rec["bidirectional"] != "Y":
                fails.append("A3 紀錄只允許主料由替代料取代，不可反向使用（Bidirectional≠Y）")
            ratio = 1 / ratio if ratio else 0
        # A5 範圍
        scope, sv = rec["scope"], rec["scope_value"]
        if scope == "PRODUCTLINE":
            if fgs is None or not fgs:
                fails.append(f"A5 範圍限 {sv} 產品線成品，但無法確認所屬成品")
            else:
                lines = {product_line_of_fg(f) for f in fgs}
                if lines != {sv}:
                    fails.append(f"A5 範圍限 {sv} 產品線，此位置屬 {sorted(lines)} 成品")
        elif scope == "PARENT":
            if parent is None:
                fails.append(f"A5 範圍限直接上階 {sv}（未指定上階）")
            elif parent != sv:
                fails.append(f"A5 範圍限直接上階 {sv}，此位置上階為 {parent}")
        # A6 生命週期
        sp = self.parts.get(substitute)
        if sp is None:
            fails.append("替代料主檔待人工確認或不存在")
        elif sp["lifecycle"] not in {"Active", "NRND"}:
            fails.append(f"A6 替代料生命週期為 {sp['lifecycle'] or '無效值'}")
        # A7 屬性相容
        if ratio:
            for f_ in self._attr_check(replaced, substitute, ratio):
                fails.append("A7 " + f_)
        # A10 成組替代
        bundle_members: list[dict] = []
        if rec.get("bundle_id") and check_bundle:
            for m in self.alts_by_bundle[rec["bundle_id"]]:
                if m["alt_rec_id"] == rec["alt_rec_id"]:
                    continue
                m_eval = self.evaluate_record(m, m["primary_part"], parent, fgs, prod_date, check_bundle=False)
                in_parent = parent is not None and any(
                    pl["child"] == m["primary_part"] and self._line_usable(pl, prod_date) is None
                    for pl in self.bom_by_parent.get(parent, []))
                member = {"alt_rec_id": m["alt_rec_id"], "primary": m["primary_part"], "alt": m["alt_part"],
                          "qty_ratio": m["qty_ratio"], "usable": m_eval["usable"] and in_parent,
                          "fails": m_eval["fails"] + ([] if in_parent else [f"主料 {m['primary_part']} 不在上階 {parent} 的有效 BOM"])}
                bundle_members.append(member)
                if not member["usable"]:
                    fails.append(f"A10 成組 {rec['bundle_id']} 成員 {m['alt_rec_id']}({m['primary_part']}→{m['alt_part']}) 不可用")
        return {
            "alt_rec_id": rec["alt_rec_id"], "replaced": replaced, "substitute": substitute, "direction": "反向" if reverse else "正向",
            "scope": scope, "scope_value": sv, "priority": rec["priority"], "qty_ratio_effective": ratio,
            "mix_allowed": rec["mix_allowed"], "bundle_id": rec.get("bundle_id"), "bundle_members": bundle_members,
            "approval_status": st, "valid": f"{rec['valid_from']}~{rec['valid_to'] or '無期限'}",
            "substitute_lifecycle": sp["lifecycle"] if sp else None, "usable": not fails, "fails": fails, "notes": notes,
        }

    def alternate_options(self, part: str, parent: str | None, fgs: set[str] | None, prod_date: date) -> list[dict]:
        """列出某料號在某位置的所有替代料紀錄判斷結果（含正向與 Bidirectional 反向），依 Priority 排序。

        A4 不得串接：只看直接紀錄，不推導 A→B→C。
        """
        recs = list(self.alts_by_primary.get(part, [])) + [r for r in self.alts_by_alt.get(part, []) if r["primary_part"] != part]
        out = [self.evaluate_record(r, part, parent, fgs, prod_date) for r in recs]
        out.sort(key=lambda e: (not e["usable"], e["priority"] if e["priority"] is not None else 99))
        return out

    # ------------------------------------------------------------------ 缺料試算
    def shortage(self, root: str, qty: int, plant: str, prod_date: date,
                 include_po: bool = False, include_alt: bool = False, only_parts: set[str] | None = None,
                 only_ref_des: str | None = None) -> dict:
        """缺料試算：毛需求 vs 可用庫存（可選加在途、合法替代料），並求最多可做台數。

        only_parts / only_ref_des：只針對特定料號或零件位置計算（例如「DIMM1-DIMM16 的記憶體最多夠做幾片」）。
        """
        ex = self.explode(root, prod_date)
        if only_parts:
            ex.positions = [p for p in ex.positions if p.rm in only_parts]
        if only_ref_des:
            key = only_ref_des.replace(" ", "").upper()
            ex.positions = [p for p in ex.positions if key in p.ref_des.replace(" ", "").upper()]
        per_root = ex.requirement_per_root()
        fgs = {root} if self.parts.get(root, {}).get("part_type") == "FG" else self.fg_ancestors(root, prod_date)
        supply_cache: dict[str, dict] = {}

        def supply(pn: str) -> float:
            if pn not in supply_cache:
                st = self.available_stock(pn, plant, prod_date)
                tr = self.in_transit(pn, plant, prod_date) if include_po else None
                supply_cache[pn] = {"stock": st, "transit": tr,
                                    "total": st["available"] + (tr["in_transit_in_time"] if tr else 0)}
            return supply_cache[pn]["total"]

        # 預先算出每個位置的可用替代方案（依 Priority）
        alt_plan: dict[int, list[dict]] = {}
        if include_alt:
            for i, p in enumerate(ex.positions):
                alt_plan[i] = [o for o in self.alternate_options(p.rm, p.parent, fgs, prod_date) if o["usable"]]

        def allocate(n: int) -> dict:
            """以 n 台試算：先用主料，不足時依優先順序切換替代料（遵守混用與成組規則）。"""
            uom = {pn: self.parts[pn]["uom"] for pn in per_root}
            need = {pn: ceil_qty(q * n, uom[pn]) for pn, q in per_root.items()}
            pool = {pn: supply(pn) for pn in need}
            switched: list[dict] = []
            if include_alt:
                # 替代料的可用量 = 其供給 − 其自身在本工單的需求（共用同一庫存池）
                def free(pn: str) -> float:
                    if pn not in pool:
                        pool[pn] = supply(pn)
                    return pool[pn] - need.get(pn, 0)

                for i, p in enumerate(ex.positions):
                    deficit = need[p.rm] - pool[p.rm]
                    if deficit <= EPS:
                        continue
                    boards = p.parent_units_per_root * n  # 此位置的上階組件數
                    for opt in alt_plan.get(i, []):
                        deficit = need[p.rm] - pool[p.rm]
                        if deficit <= EPS:
                            break
                        q_alt = p.qty_per_parent * opt["qty_ratio_effective"]
                        members = [(p.rm, opt["substitute"], p.qty_per_parent, q_alt)]
                        for m in opt["bundle_members"]:
                            mp = next((x for x in ex.positions if x.parent == p.parent and x.rm == m["primary"]), None)
                            if mp is None:
                                members = []
                                break
                            members.append((m["primary"], m["alt"], mp.qty_per_parent, mp.qty_per_parent * m["qty_ratio"]))
                        if not members:
                            continue
                        integer_boards = opt["mix_allowed"] != "Y" or bool(opt["bundle_id"])
                        cap = boards
                        for _prim, alt, _qp, qa in members:
                            cap = min(cap, free(alt) / qa if qa else 0)
                        want = deficit / p.qty_per_parent
                        b = min(want, cap)
                        if integer_boards:
                            b = min(math.ceil(want - EPS), math.floor(cap + EPS))
                        if b <= EPS:
                            continue
                        for prim, alt, qp, qa in members:
                            use_alt = ceil_qty(b * qa, self.parts[alt]["uom"])
                            pool[alt] = pool.get(alt, supply(alt)) - use_alt
                            need[prim] = max(0.0, need[prim] - b * qp)
                            need[prim] = ceil_qty(need[prim], uom[prim]) if prim in uom else need[prim]
                        switched.append({"position": f"{p.parent} 行{p.line_no} {p.ref_des}", "primary": p.rm,
                                         "alt_rec_id": opt["alt_rec_id"], "substitute": opt["substitute"],
                                         "boards_switched": round(b, 4), "mix_allowed": opt["mix_allowed"],
                                         "bundle": [(m[0], m[1]) for m in members[1:]]})
            short = {pn: need[pn] - pool[pn] for pn in need if need[pn] - pool[pn] > EPS}
            return {"need": need, "pool": pool, "short": short, "switched": switched}

        base = allocate(qty)
        # 二分搜尋最多可做台數（0..qty）
        lo, hi = 0, qty
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if allocate(mid)["short"]:
                hi = mid - 1
            else:
                lo = mid
        rows = []
        for pn in sorted(per_root, key=lambda x: (x not in base["short"], x)):
            s = supply_cache.get(pn) or {}
            if not s:
                supply(pn)
                s = supply_cache[pn]
            gross = ceil_qty(per_root[pn] * qty, self.parts[pn]["uom"])
            rows.append({
                "part": pn, "desc": self.parts[pn]["part_desc"], "uom": self.parts[pn]["uom"],
                "gross_requirement": gross, "available_stock": s["stock"]["available"],
                "in_transit_in_time": s["transit"]["in_transit_in_time"] if s["transit"] else None,
                "requirement_after_alternates": base["need"][pn] if include_alt else None,
                "shortage": round(base["short"].get(pn, 0.0), 4),
                "positions": [f"{p.parent} 行{p.line_no} {p.ref_des} ×{round(p.qty_per_parent, 4)}" for p in ex.positions if p.rm == pn],
            })
        return {
            "root": root, "qty": qty, "plant": plant, "prod_date": prod_date.isoformat(),
            "include_open_po": include_po, "include_alternates": include_alt,
            "rm_count": len(per_root), "short_count": len(base["short"]),
            "max_buildable": lo, "rows": rows, "alternate_switches": base["switched"],
            "excluded_bom_lines": dict(ex.excluded), "excluded_bom_examples": {k: v for k, v in ex.excluded_examples.items()},
            "supply_detail": supply_cache,
        }
