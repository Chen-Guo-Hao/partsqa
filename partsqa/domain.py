"""領域設定載入：料號格式、產品線、工廠、物料類別、資料檔名、基準日。

所有「跟這份資料有關」的規格都集中在 config/domain.json，
程式邏輯（清理規則、BOM 展開、替代料 A1–A12）不需要因為換資料而改寫。
可用環境變數 PQA_DOMAIN_FILE 指向其他設定檔。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Domain:
    system_name: str
    company_context: str
    baseline_date: date
    files: dict[str, str]  # 邏輯名稱 → 實際 CSV 檔名
    part_patterns: dict[str, re.Pattern]  # FG / SA / RM
    product_lines: dict[str, dict]  # 料號中的產品線字母 → {"code","name"}
    plants: dict[str, dict]  # 標準代碼 → {"name","aliases"}
    commodity_groups: dict[str, list[str]]  # R4 負責群組 → 類別碼

    @property
    def line_codes(self) -> list[str]:
        return [v["code"] for v in self.product_lines.values()]

    @property
    def all_commodities(self) -> set[str]:
        return {c for codes in self.commodity_groups.values() for c in codes}

    def file(self, logical: str) -> str:
        return self.files[logical]


@lru_cache(maxsize=4)
def _load(path: str) -> Domain:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return Domain(
        system_name=raw["system_name"],
        company_context=raw["company_context"],
        baseline_date=date.fromisoformat(raw["baseline_date"]),
        files=raw["files"],
        part_patterns={k: re.compile(v) for k, v in raw["part_patterns"].items()},
        product_lines=raw["product_lines"],
        plants=raw["plants"],
        commodity_groups=raw["commodity_groups"],
    )


def get_domain() -> Domain:
    p = os.getenv("PQA_DOMAIN_FILE", "").strip()
    path = Path(p) if p else PROJECT_ROOT / "config" / "domain.json"
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return _load(str(path))
