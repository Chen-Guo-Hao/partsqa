"""Tool Registry：提供給問答機器人（LLM tool use）與 MCP 伺服器共用的查詢工具。

每個工具都有：名稱、說明、輸入 Schema（Pydantic 驗證）、處理函式。
權限在這一層強制過濾（資料範圍＋欄位敏感等級），LLM 拿不到越權資料，
即使提示被注入也無法繞過。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import mean

from pydantic import BaseModel, Field, ValidationError, field_validator

from .config import BASELINE_DATE
from .engine import Engine
from .normalize import PLANTS, normalize_part_no, normalize_plant, product_line_of_fg
from .permissions import PermissionDenied, Scope, UserCtx, aggregate_count

MAX_ROWS = 40  # 單一工具回傳的最大列數（控制 token）


# ====================================================================== 輸入 Schema
class _Base(BaseModel):
    model_config = {"extra": "forbid"}


def _date(v: str | None) -> date:
    if v in (None, ""):
        return BASELINE_DATE
    try:
        return date.fromisoformat(str(v))
    except ValueError as e:
        raise ValueError("日期格式須為 YYYY-MM-DD") from e


class PartArgs(_Base):
    part_no: str = Field(description="料號，例如 FGS208-01、ASY-10001、GPU-100801")
    prod_date: str | None = Field(default=None, description="生產日 YYYY-MM-DD；未指定用基準日 2026-10-31")

    @field_validator("part_no")
    @classmethod
    def _pn(cls, v: str) -> str:
        return normalize_part_no(v)


class BomArgs(PartArgs):
    full: bool = Field(default=False, description="true=多階展開到原材料（每 1 個上階的需求）；false=單階")
    commodity: str | None = Field(default=None, description="只列此物料類別碼的原材料，例如 CON、MEM（多階展開時建議使用以免結果被截斷）")
    ref_des: str | None = Field(default=None, description="只列此零件位置，例如 DIMM1-DIMM16")


class PartsArgs(_Base):
    part_nos: list[str] = Field(description="料號清單（最多 30 個）", max_length=30)

    @field_validator("part_nos")
    @classmethod
    def _pns(cls, v: list[str]) -> list[str]:
        return [normalize_part_no(x) for x in v]


class StockArgs(PartArgs):
    plant: str = Field(description="工廠代碼 " + "/".join(PLANTS) + "（可用別名）")

    @field_validator("plant")
    @classmethod
    def _plant(cls, v: str) -> str:
        p = normalize_plant(v)
        if p is None:
            raise ValueError(f"無效工廠：{v}；有效值 {list(PLANTS)}")
        return p


class AltArgs(PartArgs):
    parent: str | None = Field(default=None, description="直接上階料號（選填；不填則對每個直接上階分別判斷）")
    finished_good: str | None = Field(default=None, description="成品料號（選填，用於 PRODUCTLINE 範圍判斷）")
    plant: str | None = Field(default=None, description="工廠（選填）：提供時計算每個可用替代方案（含成組成員）以該廠可用庫存最多可切換幾個上階組件")

    @field_validator("plant")
    @classmethod
    def _plant(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        p = normalize_plant(v)
        if p is None:
            raise ValueError(f"無效工廠：{v}")
        return p


class ShortageArgs(_Base):
    root: str = Field(description="要生產的成品或半成品料號")
    qty: int = Field(gt=0, le=1_000_000, description="生產數量")
    plant: str = Field(description="生產工廠")
    prod_date: str | None = Field(default=None, description="生產日 YYYY-MM-DD")
    include_open_po: bool = Field(default=False, description="是否計入來得及到貨的在途採購（ETA+3天≤生產日）")
    include_alternates: bool = Field(default=False, description="是否計入可合法使用的替代料（A1–A12）")
    ref_des: str | None = Field(default=None, description="只計算特定零件位置，例如 'DIMM1-DIMM16'")
    only_parts: list[str] | None = Field(default=None, description="只計算特定原材料料號")

    @field_validator("root")
    @classmethod
    def _root(cls, v: str) -> str:
        return normalize_part_no(v)

    @field_validator("plant")
    @classmethod
    def _plant(cls, v: str) -> str:
        p = normalize_plant(v)
        if p is None:
            raise ValueError(f"無效工廠：{v}")
        return p


class LeadTimeArgs(_Base):
    part_no: str = Field(description="受影響的原材料料號")
    change_from: str = Field(description="交期變更起始日 YYYY-MM-DD（也作為評估的生產起始日）")
    new_lead_time_days: int = Field(gt=0, description="新的採購前置天數")
    old_lead_time_days: int | None = Field(default=None, description="原本的前置天數（選填）")

    @field_validator("part_no")
    @classmethod
    def _pn(cls, v: str) -> str:
        return normalize_part_no(v)


# ====================================================================== 共用：欄位遮蔽
def _mask_supplier(ctx: UserCtx, name: str | None) -> str | None:
    return name if ctx.sensitive_mode == "detail" else None


def _mask_price(ctx: UserCtx, price: float | None) -> float | None:
    return price if ctx.price_mode == "detail" else None


def _sens_note(ctx: UserCtx) -> list[str]:
    notes = []
    if ctx.sensitive_mode != "detail":
        notes.append("供應商名稱／原廠料號屬敏感欄位，您的角色只能看彙總（家數），不提供個別名稱")
    if ctx.price_mode == "none":
        notes.append("單價屬高度敏感欄位，您的角色不可查看")
    elif ctx.price_mode == "aggregate":
        notes.append("單價屬高度敏感欄位，您的角色只能看不指名料號的統計（樣本少於 3 筆不提供）")
    return notes


def _po_view(ctx: UserCtx, rec: dict) -> dict:
    out = {"po": f"{rec['po_no']}-{rec['po_line']}", "open_qty": rec["open_qty"], "eta": rec["eta"]}
    if ctx.sensitive_mode == "detail":
        out["supplier"] = rec["supplier_name"]
    if ctx.price_mode == "detail":
        out["unit_price_usd"] = rec["unit_price_usd"]
    return out


# ====================================================================== 工具實作
@dataclass
class ToolEnv:
    engine: Engine
    scope: Scope
    ctx: UserCtx


def t_part_info(env: ToolEnv, a: PartsArgs) -> dict:
    """料號主檔與屬性（依角色遮蔽敏感欄位）。"""
    out, hidden = [], 0
    for pn in a.part_nos:
        if not env.scope.part_allowed(env.ctx, pn):
            hidden += 1
            continue
        st = env.engine.part_status(pn)
        if st != "ok":
            out.append({"part_no": pn, "status": "主檔待人工確認（確認前不使用）" if st == "blocked" else "主檔中不存在或為無效料號"})
            continue
        p = env.engine.part(pn)
        row = {k: p[k] for k in ["part_no", "part_desc", "part_type", "commodity", "product_line", "uom", "lifecycle",
                                 "make_buy", "lead_time_days", "moq"] if p.get(k) is not None}
        if env.ctx.sensitive_mode == "detail":
            row["supplier_name"], row["mfr_part_no"] = p["supplier_name"], p["mfr_part_no"]
        if env.ctx.price_mode == "detail":
            row["unit_cost_usd"] = p["unit_cost_usd"]
        row["attributes"] = env.engine.attrs.get(pn, {})
        out.append(row)
    res = {"parts": out, "notes": _sens_note(env.ctx)}
    if hidden:
        res["hidden_out_of_scope"] = hidden
    return res


def t_bom(env: ToolEnv, a: BomArgs) -> dict:
    env.scope.check_part(env.ctx, a.part_no)
    d = _date(a.prod_date)
    e = env.engine
    if e.part_status(a.part_no) != "ok":
        return {"error": f"{a.part_no} 主檔不存在或待人工確認"}
    if not a.full:
        res = e.single_level(a.part_no, d)
        lines = [ln for ln in res["lines"] if env.scope.part_allowed(env.ctx, ln["child"])]
        hidden = len(res["lines"]) - len(lines)
        out = {"parent": a.part_no, "prod_date": d.isoformat(), "line_count": len(lines), "lines": lines[:MAX_ROWS * 2],
               "excluded_lines": res["excluded_lines"]}
    else:
        ex = e.explode(a.part_no, d)
        req = ex.requirement_per_root()
        rows = []
        for pn, q in sorted(req.items()):
            if not env.scope.part_allowed(env.ctx, pn):
                continue
            if a.commodity and not pn.startswith(a.commodity.strip().upper() + "-"):
                continue
            if a.ref_des and not any(a.ref_des.replace(" ", "").upper() in p.ref_des.replace(" ", "").upper()
                                     for p in ex.positions if p.rm == pn):
                continue
            pos = [f"{p.parent} 行{p.line_no} {p.ref_des}".strip() for p in ex.positions if p.rm == pn]
            rows.append({"rm": pn, "desc": e.parts[pn]["part_desc"], "qty_per_unit": round(q, 6), "uom": e.parts[pn]["uom"], "positions": pos})
        hidden = len(req) - len(rows)
        out = {"root": a.part_no, "prod_date": d.isoformat(), "filter": {"commodity": a.commodity, "ref_des": a.ref_des},
               "rm_count": len(rows), "rows": rows[:MAX_ROWS * 2], "excluded_bom_lines": dict(ex.excluded),
               "rule": "半成品不留庫存，展開到原材料；需求=各階(用量×(1+損耗率))連乘"}
        if len(rows) > MAX_ROWS * 2:
            out["truncated"] = f"共 {len(rows)} 項只列前 {MAX_ROWS * 2} 項；請改用 commodity 或 ref_des 篩選取得完整清單"
    if hidden:
        out["hidden_out_of_scope"] = hidden
    return out


def t_where_used(env: ToolEnv, a: PartArgs) -> dict:
    env.scope.check_part(env.ctx, a.part_no)
    d = _date(a.prod_date)
    w = env.engine.where_used(a.part_no, d)
    fgs = {fg: v for fg, v in w["finished_goods"].items() if env.scope.part_allowed(env.ctx, fg)}
    parents = [p for p in w["direct_parents"] if env.scope.part_allowed(env.ctx, p["parent"])]
    out = {"part": a.part_no, "prod_date": d.isoformat(), "direct_parents": parents,
           "finished_goods_affected": fgs, "excluded_lines": w["excluded_lines"]}
    # R2 不可透露兩條產品線是否共用料，因此對 R2 不提示「有被隱藏的上階」
    if env.ctx.role != "R2" and (len(fgs) < len(w["finished_goods"]) or len(parents) < len(w["direct_parents"])):
        out["note"] = "部分上階不在您的權限範圍，已排除（不揭露其內容）"
    return out


def t_stock(env: ToolEnv, a: StockArgs) -> dict:
    env.scope.check_part(env.ctx, a.part_no)
    env.scope.check_plant(env.ctx, a.plant)
    d = _date(a.prod_date)
    st = env.engine.available_stock(a.part_no, a.plant, d)
    tr = env.engine.in_transit(a.part_no, a.plant, d)
    return {
        "part": a.part_no, "plant": a.plant, "prod_date": d.isoformat(), "available_stock": st["available"],
        "lots_used": st["lots_used"], "excluded_lots": st["excluded_lots"], "shelf_life_months": st["shelf_life_months"],
        "in_transit_in_time": tr["in_transit_in_time"], "po_in_time": [_po_view(env.ctx, r) for r in tr["po_lines_in_time"]],
        "po_too_late": [_po_view(env.ctx, r) for r in tr["po_lines_too_late"]][:15],
        "excluded_po_lines": tr["excluded_po_lines"],
        "rules": "可用庫存=Σ(在庫−保留)，只計合格批、單位一致、未過期；在途只計 Open 未交量且 ETA+3天≤生產日",
        "notes": _sens_note(env.ctx),
    }


def _alt_view(env: ToolEnv, o: dict) -> dict:
    allowed = env.scope.part_allowed(env.ctx, o["substitute"])
    v = {k: o[k] for k in ["alt_rec_id", "substitute", "direction", "scope", "scope_value", "priority", "qty_ratio_effective",
                           "mix_allowed", "bundle_id", "approval_status", "valid", "substitute_lifecycle", "usable", "fails", "notes"]}
    if o["bundle_members"]:
        v["bundle_members"] = [{"alt_rec_id": m["alt_rec_id"], "primary": m["primary"], "alt": m["alt"], "qty_ratio": m["qty_ratio"],
                                "usable": m["usable"], "fails": m["fails"]} for m in o["bundle_members"]]
    if not allowed:
        v["substitute"] = "(不在權限範圍)"
    return v


def _switch_capacity(env: ToolEnv, o: dict, parent: str, plant: str, d: date) -> dict:
    """某可用替代方案（含成組成員）在某廠以可用庫存最多可切換幾個上階組件（整片切換，不混用）。"""
    e = env.engine
    single = {ln["child"]: ln for ln in e.single_level(parent, d)["lines"]}
    members = [(o["replaced"], o["substitute"], o["qty_ratio_effective"])]
    members += [(m["primary"], m["alt"], m["qty_ratio"]) for m in o["bundle_members"]]
    rows, cap = [], None
    for prim, alt, ratio in members:
        ln = single.get(prim)
        per_board = ln["qty_per"] * (1 + ln["scrap_rate"]) * ratio if ln else None
        st = e.available_stock(alt, plant, d)
        boards = int(st["available"] // per_board) if per_board else 0
        cap = boards if cap is None else min(cap, boards)
        rows.append({"replaced": prim, "substitute": alt, "ref_des": ln["ref_des"] if ln else None,
                     "substitute_qty_per_board": per_board, "substitute_available": st["available"],
                     "boards_supported": boards, "excluded_lots": st["excluded_lots"]})
    return {"alt_rec_id": o["alt_rec_id"], "bundle_id": o["bundle_id"], "plant": plant, "members": rows,
            "max_boards_switchable": cap,
            "note": "成組成員須同一片一起切換；MixAllowed=N 時同一片不可混用；只計可用庫存（未含在途）"}


def t_alternates(env: ToolEnv, a: AltArgs) -> dict:
    env.scope.check_part(env.ctx, a.part_no)
    e = env.engine
    d = _date(a.prod_date)
    fg_given = normalize_part_no(a.finished_good) if a.finished_good else None
    if fg_given:
        env.scope.check_part(env.ctx, fg_given)
    contexts = []
    if a.parent:
        parent = normalize_part_no(a.parent)
        env.scope.check_part(env.ctx, parent)
        fgs = {fg_given} if fg_given else e.fg_ancestors(parent, d)
        contexts.append((parent, fgs))
    else:
        w = e.where_used(a.part_no, d)
        for p in w["direct_parents"]:
            if not env.scope.part_allowed(env.ctx, p["parent"]):
                continue
            fgs = {fg for fg, ps in w["finished_goods"].items() if p["parent"] in ps}
            if fg_given:
                fgs = fgs & {fg_given}
            contexts.append((p["parent"], fgs))
        if not contexts:
            contexts.append((None, {fg_given} if fg_given else None))
    if a.plant:
        env.scope.check_plant(env.ctx, a.plant)
    out = []
    for parent, fgs in contexts[:8]:
        opts = e.alternate_options(a.part_no, parent, fgs, d)
        visible_fgs = sorted(f for f in (fgs or []) if env.scope.part_allowed(env.ctx, f))
        pos = {"parent": parent, "finished_goods": visible_fgs,
               "product_lines": sorted({product_line_of_fg(f) for f in visible_fgs}),
               "usable_in_priority_order": [o["substitute"] for o in opts if o["usable"]],
               "records": [_alt_view(env, o) for o in opts]}
        if a.plant and parent:
            pos["switch_capacity"] = [_switch_capacity(env, o, parent, a.plant, d) for o in opts if o["usable"]]
        out.append(pos)
    return {"part": a.part_no, "prod_date": d.isoformat(), "positions": out,
            "rules": ("A1 須 Approved（Pending 須主料類別 R4 核准、Rejected 不可用）；A2 生產日在有效期間；A3 反向限 Bidirectional=Y；"
                      "A4 不得串接（A→B、B→C 不代表 A→C）；A5 範圍；A6 替代料須 Active/NRND；A7 屬性相容；A9 MixAllowed=N 同一上階不可混用；"
                      "A10 成組須一起切換；Remark 備註不具核准效力")}


def t_shortage(env: ToolEnv, a: ShortageArgs) -> dict:
    env.scope.check_part(env.ctx, a.root)
    env.scope.check_plant(env.ctx, a.plant)
    e = env.engine
    if e.part_status(a.root) != "ok":
        return {"error": f"{a.root} 主檔不存在或待人工確認"}
    d = _date(a.prod_date)
    only = {normalize_part_no(x) for x in a.only_parts} if a.only_parts else None
    r = e.shortage(a.root, a.qty, a.plant, d, a.include_open_po, a.include_alternates, only_parts=only, only_ref_des=a.ref_des)
    rows = []
    resolved = []  # 只看庫存會缺、但計入在途／替代料後已補足的料（讓回答能說明「靠什麼補足」）
    hidden = 0
    for x in r["rows"]:
        if x["shortage"] <= 0:
            stock_gap = x["gross_requirement"] - x["available_stock"]
            if (a.include_open_po or a.include_alternates) and stock_gap > 0 and env.scope.part_allowed(env.ctx, x["part"]):
                s = r["supply_detail"][x["part"]]
                item = {"part": x["part"], "gross_requirement": x["gross_requirement"], "available_stock": x["available_stock"],
                        "stock_only_shortage": stock_gap}
                if a.include_open_po:
                    item["in_transit_in_time"] = x["in_transit_in_time"]
                    item["po_in_time"] = [_po_view(env.ctx, p) for p in s["transit"]["po_lines_in_time"]]
                if a.include_alternates:
                    item["requirement_after_alternates"] = x["requirement_after_alternates"]
                resolved.append(item)
            continue
        if not env.scope.part_allowed(env.ctx, x["part"]):
            hidden += 1
            continue
        s = r["supply_detail"][x["part"]]
        row = {k: x[k] for k in ["part", "desc", "uom", "gross_requirement", "available_stock", "shortage", "positions"]}
        if a.include_open_po:
            row["in_transit_in_time"] = x["in_transit_in_time"]
            row["po_in_time"] = [_po_view(env.ctx, p) for p in s["transit"]["po_lines_in_time"]]
            row["po_too_late"] = [_po_view(env.ctx, p) for p in s["transit"]["po_lines_too_late"]][:5]
        if a.include_alternates:
            row["requirement_after_alternates"] = x["requirement_after_alternates"]
        row["excluded_lots"] = s["stock"]["excluded_lots"]
        rows.append(row)
    out = {
        "root": a.root, "qty": a.qty, "plant": a.plant, "prod_date": d.isoformat(),
        "include_open_po": a.include_open_po, "include_alternates": a.include_alternates,
        "filter": {"ref_des": a.ref_des, "only_parts": sorted(only) if only else None},
        "raw_materials_checked": r["rm_count"], "short_items": rows, "short_count": len(rows),
        "resolved_by_po_or_alternates": resolved,
        "max_buildable": r["max_buildable"],
        "alternate_switches": [s for s in r["alternate_switches"] if env.scope.part_allowed(env.ctx, s["primary"])],
        "excluded_bom_lines": r["excluded_bom_lines"],
        "rules": "毛需求=數量×各階(用量×(1+損耗率))連乘，PCS 無條件進位；可用庫存只計合格、未過期、扣保留量；"
                 "在途只計 Open 且 ETA+3天≤生產日；替代料依 A1–A12 並依 Priority 使用；max_buildable 為在相同條件下最多可做數量",
    }
    if hidden:
        out["hidden_out_of_scope_short_items"] = hidden
    return out


def t_supplier_price(env: ToolEnv, a: PartsArgs) -> dict:
    """供應商與單價：R4 明細；R3 供應商明細無單價；R1 彙總統計；R2 只有供應商家數。"""
    ctx, e = env.ctx, env.engine
    parts, hidden = env.scope.filter_parts(ctx, a.part_nos)
    recs = [e.part(p) for p in parts if e.part(p)]
    out: dict = {"parts_considered": len(recs)}
    if hidden:
        out["hidden_out_of_scope"] = hidden
    if ctx.sensitive_mode == "detail":
        out["suppliers"] = {r["part_no"]: r["supplier_name"] for r in recs}
    else:
        out["supplier_count"] = aggregate_count(len({r["supplier_name"] for r in recs if r["supplier_name"]}))
    if ctx.price_mode == "detail":
        out["unit_cost_usd"] = {r["part_no"]: r["unit_cost_usd"] for r in recs}
        prices = [r["unit_cost_usd"] for r in recs if r["unit_cost_usd"]]
        if len(prices) == 2:
            out["price_difference_usd"] = round(prices[0] - prices[1], 6)
    elif ctx.price_mode == "aggregate":
        # R1 只能看「不指名料號」的統計：以物料類別維度（該類別全部料號）彙總平均單價，
        # 不針對使用者指定的料號集合計算，避免以差集推回個別料號單價；少於 3 筆回「少於 3」
        stats = {}
        for comm in sorted({r["commodity"] for r in recs if r.get("commodity")}):
            prices = [p["unit_cost_usd"] for p in e.parts.values() if p.get("commodity") == comm and p.get("unit_cost_usd")]
            stats[comm] = {"n": len(prices), "avg_unit_cost_usd": round(mean(prices), 4)} if len(prices) >= 3 else "少於 3"
        out["unit_cost_stats_by_commodity"] = stats or "無可彙總的物料類別"
    out["notes"] = _sens_note(ctx)
    return out


def t_lead_time(env: ToolEnv, a: LeadTimeArgs) -> dict:
    """交期變更影響：受影響成品、各廠可用庫存與在途、合法替代料、新下單最早到貨日。"""
    ctx, e = env.ctx, env.engine
    env.scope.check_part(ctx, a.part_no)
    d = _date(a.change_from)
    w = e.where_used(a.part_no, d)
    fgs = {fg: v for fg, v in w["finished_goods"].items() if env.scope.part_allowed(ctx, fg)}
    plants = [ctx.scope] if ctx.role == "R3" else list(PLANTS)
    supply = []
    for pl in plants:
        st = e.available_stock(a.part_no, pl, d)
        tr = e.in_transit(a.part_no, pl, d)  # ETA+3 天 ≤ 起始日者才算來得及
        supply.append({"plant": pl, "available_stock_on_start_date": st["available"],
                       "po_usable_by_start_date": [_po_view(ctx, r) for r in tr["po_lines_in_time"]],
                       "qty_usable_by_start_date": st["available"] + tr["in_transit_in_time"],
                       "po_arriving_after_start_date": [_po_view(ctx, r) for r in tr["po_lines_too_late"]],
                       "excluded_po_lines": tr["excluded_po_lines"]})
    alts = []
    for p in w["direct_parents"]:
        if not env.scope.part_allowed(ctx, p["parent"]):
            continue
        pf = {fg for fg, ps in w["finished_goods"].items() if p["parent"] in ps}
        alts.append({"parent": p["parent"], "qty_per": p["qty_per"],
                     "records": [_alt_view(env, o) for o in e.alternate_options(a.part_no, p["parent"], pf, d)]})
    part = e.part(a.part_no) or {}
    return {
        "part": a.part_no, "desc": part.get("part_desc"), "master_lead_time_days": part.get("lead_time_days"),
        "change_from": d.isoformat(), "new_lead_time_days": a.new_lead_time_days,
        "earliest_arrival_if_ordered_on_change_date": (d + timedelta(days=a.new_lead_time_days)).isoformat(),
        "earliest_usable_date_incl_3day_inspection": (d + timedelta(days=a.new_lead_time_days + 3)).isoformat(),
        "finished_goods_affected": fgs, "supply_by_plant": supply, "alternates_by_parent": alts,
        "notes": _sens_note(ctx),
    }


# ====================================================================== Registry
@dataclass
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[ToolEnv, BaseModel], dict]

    def schema(self) -> dict:
        s = self.args_model.model_json_schema()
        s.pop("title", None)
        for prop in s.get("properties", {}).values():
            prop.pop("title", None)
        return {"name": self.name, "description": self.description, "input_schema": s}


TOOLS: list[ToolSpec] = [
    ToolSpec("get_part_info", "查料號主檔一般欄位與材料屬性（依角色決定能否看到供應商／單價）。", PartsArgs, t_part_info),
    ToolSpec("get_bom", "查 BOM：full=false 單階；full=true 多階展開到原材料（每 1 個上階的用量，含零件位置）。", BomArgs, t_bom),
    ToolSpec("where_used", "反查上階：某料號的直接上階與最終受影響成品（依生產日有效 BOM）。", PartArgs, t_where_used),
    ToolSpec("get_stock_and_transit", "查某料號在某工廠於生產日的可用庫存（含排除批次原因）與在途採購（來得及/來不及）。", StockArgs, t_stock),
    ToolSpec("check_alternates", "依規則 A1–A12 判斷某料號在各直接上階位置可用的替代料，列出不可用原因（含成組、方向、範圍、核准狀態）；給 plant 時另算以該廠庫存最多可切換幾片（含成組瓶頸）。", AltArgs, t_alternates),
    ToolSpec("shortage_check", "缺料試算：生產某成品/半成品 N 台，可選計入在途與替代料，回傳缺料清單與最多可做數量；可用 ref_des 只算特定位置。", ShortageArgs, t_shortage),
    ToolSpec("get_supplier_price", "查料號供應商與單價（依角色：R4 明細、R3 僅供應商、R1 統計、R2 僅家數）。", PartsArgs, t_supplier_price),
    ToolSpec("lead_time_impact", "評估料號交期拉長的影響：受影響成品、各廠庫存與在途、替代料、新下單最早到貨日。", LeadTimeArgs, t_lead_time),
]
TOOL_MAP = {t.name: t for t in TOOLS}


def tool_schemas() -> list[dict]:
    return [t.schema() for t in TOOLS]


def run_tool(env: ToolEnv, name: str, raw_args: dict) -> tuple[dict, dict]:
    """執行工具；回傳 (結果, 執行紀錄)。所有例外轉為結構化錯誤，不讓程式崩潰。"""
    t0 = time.time()
    log = {"tool": name, "args": raw_args, "status": "ok"}
    spec = TOOL_MAP.get(name)
    try:
        if spec is None:
            raise KeyError(f"未知工具：{name}")
        if not env.ctx.can_see_parts:
            raise PermissionDenied("R5 系統管理員依職責分離不可查看任何料件資料。")
        args = spec.args_model.model_validate(raw_args)
        result = spec.handler(env, args)
    except PermissionDenied as e:
        result, log["status"] = {"permission_denied": True, "message": str(e)}, "denied"
    except ValidationError as e:
        result, log["status"] = {"error": "參數錯誤", "detail": json.loads(e.json(include_url=False))}, "invalid_args"
    except Exception as e:  # noqa: BLE001 - 工具錯誤需回報給 LLM 而非中斷對話
        result, log["status"] = {"error": f"{type(e).__name__}: {e}"}, "error"
    log["ms"] = int((time.time() - t0) * 1000)
    return result, log
