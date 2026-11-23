"""環境檢查：PASS / FAIL / SKIP（不印出金鑰）。必要項目 FAIL 時 exit code = 1。

用法：python -m scripts.env_check
"""

from __future__ import annotations

import importlib
import sys

from partsqa.app_store import AppStore
from partsqa.cleaning import SOURCE_FILES
from partsqa.config import get_settings

results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool | None, detail: str = "", required: bool = True) -> None:
    status = "SKIP" if ok is None else ("PASS" if ok else ("FAIL" if required else "WARN"))
    results.append((status, name, detail))


def main() -> int:
    s = get_settings()
    check("Python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0])
    check("虛擬環境", sys.prefix != sys.base_prefix, sys.prefix, required=False)
    for pkg in ["pandas", "anthropic", "streamlit", "bcrypt", "pydantic", "dotenv", "mcp"]:
        try:
            m = importlib.import_module(pkg)
            check(f"套件 {pkg}", True, getattr(m, "__version__", ""))
        except ImportError as e:
            check(f"套件 {pkg}", False, str(e), required=pkg != "mcp")
    missing = [f for f in SOURCE_FILES if not (s.data_dir / f).exists()]
    check("原始資料 PQA_DATA_DIR", not missing, f"{s.data_dir}" + (f" 缺 {missing}" if missing else ""))
    check("清理後資料庫", s.clean_db.exists(), "不存在時請執行 python -m scripts.run_clean" if not s.clean_db.exists() else str(s.clean_db),
          required=False)
    try:
        n = len(AppStore(s.app_db).list_users())
        check("帳號已建立", n > 0, f"{n} 個帳號" if n else "請執行 python -m scripts.init_users", required=False)
    except Exception as e:  # noqa: BLE001
        check("帳號資料庫", False, type(e).__name__)
    check("ANTHROPIC_API_KEY", s.has_api_key if not s.use_mock_llm else None,
          "已設定（不顯示）" if s.has_api_key else "未設定：問答需真實模型時無法使用", required=False)
    check("PQA_LLM_BASE_URL", bool(s.anthropic_base_url) or None, s.anthropic_base_url or "未設定（使用 Anthropic 官方端點）", required=False)
    check("模型設定", bool(s.model_router), f"agent={s.model_router} light={s.model_light} effort={s.effort or 'default'}")
    for st, name, detail in results:
        print(f"[{st}] {name}: {detail}")
    return 1 if any(st == "FAIL" for st, _, _ in results) else 0


if __name__ == "__main__":
    sys.exit(main())
