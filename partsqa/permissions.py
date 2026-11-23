"""角色權限（第二節）：資料範圍 + 欄位敏感等級，於工具層強制執行。

- 一般欄位：R1–R4 明細；R5 不可。
- 敏感（SupplierName、MfrPartNo）：R1/R2 只能彙總；R3/R4 明細；R5 不可。
- 高度敏感（UnitCostUSD、UnitPriceUSD）：R4 明細；R1 只能看不指名料號的統計；R2/R3 不可。
- 彙總結果少於 3 筆只能回答「少於 3」。
- R2 範圍 = 自己產品線成品經 BOM 展開的料號 + 以這些料號為主料的替代料（動態計算）。
- R3 範圍 = 自己工廠的庫存與在途；BOM／替代料／屬性／主檔一般欄位全集團共用。
- R4 範圍 = 負責物料類別 + 其反查上階（動態計算）。
- R5 只能看稽核紀錄與帳號，不可看任何料件資料。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from .engine import Engine
from .normalize import COMMODITY_GROUPS, product_line_of_fg

ROLE_NAMES = {"R1": "供應鏈主管", "R2": "產品線經理", "R3": "工廠物控", "R4": "零件工程師", "R5": "系統管理員"}
MIN_AGG = 3  # 彙總門檻


@dataclass(frozen=True)
class UserCtx:
    """登入後的使用者身分（由帳號資料查出，不信任使用者在問題中自稱的角色）。"""

    username: str
    role: str  # R1..R5
    scope: str = ""  # R2: 產品線代碼（見 config/domain.json）；R3: 工廠代碼；R4: 物料群組名稱

    @property
    def label(self) -> str:
        return f"{ROLE_NAMES.get(self.role, self.role)}({self.scope})" if self.scope else ROLE_NAMES.get(self.role, self.role)

    @property
    def commodities(self) -> set[str]:
        return set(COMMODITY_GROUPS.get(self.scope, [])) if self.role == "R4" else set()

    # 欄位敏感等級
    @property
    def sensitive_mode(self) -> str:
        return {"R1": "aggregate", "R2": "aggregate", "R3": "detail", "R4": "detail"}.get(self.role, "none")

    @property
    def price_mode(self) -> str:
        return {"R1": "aggregate", "R4": "detail"}.get(self.role, "none")

    @property
    def can_see_parts(self) -> bool:
        return self.role in {"R1", "R2", "R3", "R4"}

    @property
    def can_approve(self) -> bool:
        return self.role == "R4"


class PermissionDenied(Exception):
    """越權存取；訊息會回給 LLM，且寫入稽核紀錄。"""


class Scope:
    """依引擎資料動態計算 R2/R4 的料號範圍（含快取）。"""

    def __init__(self, engine: Engine):
        self.engine = engine
        self._r2 = lru_cache(maxsize=4)(self._r2_scope)
        self._r4 = lru_cache(maxsize=8)(self._r4_scope)

    def _r2_scope(self, line: str) -> frozenset[str]:
        e = self.engine
        fgs = [f for f in e.fg_list() if product_line_of_fg(f) == line]
        parts: set[str] = set(fgs)
        for fg in fgs:
            parts |= e.descendants(fg)
        # 以這些料號為主料的替代料（可看一般欄位以判斷能否替代）
        alts = {r["alt_part"] for p in list(parts) for r in e.alts_by_primary.get(p, [])}
        return frozenset(parts | alts)

    def _r4_scope(self, group: str) -> frozenset[str]:
        e = self.engine
        codes = set(COMMODITY_GROUPS.get(group, []))
        own = {p for p, r in e.parts.items() if r.get("commodity") in codes}
        own |= {p for p in e.blocked_parts if p[:3] in codes}
        ancestors: set[str] = set()
        stack = list(own)
        while stack:
            n = stack.pop()
            for line in e.bom_by_child.get(n, []):
                if line["status"] == "ok" and line["parent"] not in ancestors:
                    ancestors.add(line["parent"])
                    stack.append(line["parent"])
        alts = {r["alt_part"] for p in own for r in e.alts_by_primary.get(p, [])}
        return frozenset(own | ancestors | alts)

    # ------------------------------------------------------------------ 判斷
    def part_allowed(self, ctx: UserCtx, pn: str) -> bool:
        if not ctx.can_see_parts:
            return False
        if ctx.role in {"R1", "R3"}:
            return True
        if ctx.role == "R2":
            return pn in self._r2(ctx.scope)
        if ctx.role == "R4":
            return pn in self._r4(ctx.scope)
        return False

    def check_part(self, ctx: UserCtx, pn: str) -> None:
        if not ctx.can_see_parts:
            raise PermissionDenied("R5 系統管理員依職責分離不可查看任何料件資料。")
        if not self.part_allowed(ctx, pn):
            if ctx.role == "R2":
                raise PermissionDenied(f"料號 {pn} 不在您（{ctx.scope} 產品線）的權限範圍內，無法查詢，也不提供與其他產品線的比對資訊。")
            raise PermissionDenied(f"料號 {pn} 不在您（{ctx.label}）負責的物料類別及其上階範圍內。")

    def check_plant(self, ctx: UserCtx, plant: str) -> None:
        if not ctx.can_see_parts:
            raise PermissionDenied("R5 系統管理員不可查看任何料件資料。")
        if ctx.role == "R3" and plant != ctx.scope:
            raise PermissionDenied(f"R3 工廠物控只能查看自己工廠（{ctx.scope}）的庫存與在途，不可查看 {plant}。")

    def filter_parts(self, ctx: UserCtx, parts: list[str]) -> tuple[list[str], int]:
        """回傳 (可看的料號, 被隱藏的筆數)。"""
        ok = [p for p in parts if self.part_allowed(ctx, p)]
        return ok, len(parts) - len(ok)


def aggregate_count(n: int) -> int | str:
    """彙總門檻：少於 3 只回答「少於 3」。"""
    return n if n >= MIN_AGG else "少於 3"
