"""集中管理設定：所有路徑、模型名稱、金鑰都從環境變數（或 .env）讀取。

不在程式碼中寫死任何金鑰、密碼或個人電腦的絕對路徑。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from .domain import get_domain

# 專案根目錄（partsqa/ 的上一層）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 讀取專案根目錄下的 .env（若存在）；已存在的環境變數優先
load_dotenv(PROJECT_ROOT / ".env", override=False)

# 資料基準日（定義於 config/domain.json）：庫存與採購單為當日快照；未指定生產日時以此日判斷
BASELINE_DATE: date = get_domain().baseline_date


def _path_from_env(name: str, default: Path) -> Path:
    """讀取路徑型環境變數；相對路徑以專案根目錄為基準。"""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    p = Path(raw)
    return p if p.is_absolute() else (PROJECT_ROOT / p)


@dataclass(frozen=True)
class Settings:
    """系統設定（唯讀）。"""

    # 原始 CSV 所在資料夾（唯讀）
    data_dir: Path = field(default_factory=lambda: _path_from_env("PQA_DATA_DIR", PROJECT_ROOT / "data"))
    # 清理後資料庫（由 scripts/run_clean.py 產生，可隨時重建）
    clean_db: Path = field(
        default_factory=lambda: _path_from_env("PQA_CLEAN_DB", PROJECT_ROOT / "data" / "processed" / "partsqa_clean.db")
    )
    # 應用程式資料庫：帳號、稽核紀錄、token 用量、人工審核紀錄（不會被清理作業覆蓋）
    app_db: Path = field(
        default_factory=lambda: _path_from_env("PQA_APP_DB", PROJECT_ROOT / "data" / "app" / "partsqa_app.db")
    )
    # 清理報告輸出位置
    report_dir: Path = field(default_factory=lambda: _path_from_env("PQA_REPORT_DIR", PROJECT_ROOT / "data" / "processed"))

    # LLM 設定
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", "").strip())
    anthropic_base_url: str = field(default_factory=lambda: os.getenv("PQA_LLM_BASE_URL", "").strip())
    # 模型路由（依 eval 模型比較結果決定）：預設 Haiku；開放式應變建議題與失敗時升級 Sonnet
    model_router: str = field(default_factory=lambda: os.getenv("PQA_MODEL_AGENT", "claude-haiku-5-5").strip())
    model_escalate: str = field(default_factory=lambda: os.getenv("PQA_MODEL_ESCALATE", "claude-haiku-5-5").strip())
    routing: str = field(default_factory=lambda: os.getenv("PQA_ROUTING", "fixed").strip())  # fixed（預設，最省）/ auto
    model_light: str = field(default_factory=lambda: os.getenv("PQA_MODEL_LIGHT", "claude-haiku-5-5").strip())
    llm_timeout_s: float = field(default_factory=lambda: float(os.getenv("PQA_LLM_TIMEOUT", "120")))
    llm_max_retries: int = field(default_factory=lambda: int(os.getenv("PQA_LLM_MAX_RETRIES", "2")))
    llm_max_tokens: int = field(default_factory=lambda: int(os.getenv("PQA_LLM_MAX_TOKENS", "8000")))
    # 推理強度（low/medium/high）；空字串表示使用模型預設
    effort: str = field(default_factory=lambda: os.getenv("PQA_EFFORT", "medium").strip())
    agent_max_turns: int = field(default_factory=lambda: int(os.getenv("PQA_AGENT_MAX_TURNS", "6")))
    # 使用 Mock LLM（測試用），不呼叫真實 API
    use_mock_llm: bool = field(default_factory=lambda: os.getenv("PQA_MOCK_LLM", "0").strip() == "1")

    @property
    def has_api_key(self) -> bool:
        """是否已設定 API Key（不回傳金鑰本身）。"""
        return bool(self.anthropic_api_key) and self.anthropic_api_key not in {"input_api_key", "your_api_key_here"}


def get_settings() -> Settings:
    """每次呼叫重新讀取環境變數（方便測試時以 monkeypatch 覆寫）。"""
    return Settings()
