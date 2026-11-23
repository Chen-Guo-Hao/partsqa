"""示範模式（PQA_DEMO_MODE=1）：讓沒有帳號密碼、沒有 API Key 的訪客也能體驗系統。

- 一鍵以範例角色登入（免密碼；角色與範圍仍由帳號資料查出，權限照常執行）。
- 每個角色提供範例問題按鈕。
- 未設定 API Key 時：重播 Claude Haiku 在評測中實際產生的回答（demo/demo_answers.json），不呼叫 API、零成本。
- 有設定 API Key 時：即時呼叫模型，但每個瀏覽器工作階段有提問次數上限（PQA_DEMO_MAX_QUESTIONS）。
- 新部署環境自動準備：帳號表為空時以隨機密碼建立範例帳號（密碼不顯示、不儲存明碼）；
  清理後資料庫不存在時執行一次初始資料清理（稽核事件 demo_bootstrap_cleaning）。

正式環境請勿開啟示範模式。
"""

from __future__ import annotations

import json
import os
import secrets

from .app_store import AppStore
from .auth import seed_users
from .cleaning import run_cleaning
from .config import PROJECT_ROOT, Settings
from .permissions import UserCtx
from .report import write_cleaning_report

DEMO_ANSWERS = PROJECT_ROOT / "demo" / "demo_answers.json"
EVAL_SET = PROJECT_ROOT / "eval" / "eval_set.json"


def is_demo() -> bool:
    return os.getenv("PQA_DEMO_MODE", "0").strip() == "1"


def max_live_questions() -> int:
    return int(os.getenv("PQA_DEMO_MAX_QUESTIONS", "10"))


def ensure_ready(s: Settings, store: AppStore) -> list[str]:
    """新部署環境的初始化；回傳做了哪些事（供畫面提示）。"""
    done = []
    if not store.list_users():
        seed_users(store, secrets.token_urlsafe(16))  # 隨機密碼：示範模式以按鈕登入，不需要知道密碼
        store.audit(username="system", event="demo_seed_users")
        done.append("已建立範例帳號")
    if not s.clean_db.exists():
        stats = run_cleaning(s.data_dir, s.clean_db, s.report_dir)
        write_cleaning_report(stats, s.report_dir)
        store.audit(username="system", event="demo_bootstrap_cleaning")
        done.append("已執行初始資料清理")
    return done


def demo_login(store: AppStore, username: str) -> UserCtx | None:
    """示範模式免密碼登入；角色與範圍一律以帳號資料為準。"""
    if not is_demo():
        return None
    u = store.get_user(username)
    if u is None:
        return None
    store.audit(username=u["username"], role=u["role"], event="demo_login")
    return UserCtx(username=u["username"], role=u["role"], scope=u["scope"] or "")


def sample_questions(username: str) -> list[str]:
    """該帳號在評測集中的題目，作為範例問題按鈕。"""
    try:
        items = json.loads(EVAL_SET.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    return [i["question"] for i in items if i["user"] == username]


def _load_answers() -> dict[tuple[str, str], dict]:
    try:
        rows = json.loads(DEMO_ANSWERS.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return {(r["user"], r["question"]): r for r in rows}


def replay_answer(username: str, question: str) -> dict | None:
    """回傳預先錄製的回答（含模型與 token 資訊）；沒有則回傳 None。"""
    return _load_answers().get((username, question.strip()))
