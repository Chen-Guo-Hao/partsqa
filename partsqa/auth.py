"""帳號密碼登入：密碼以 bcrypt 雜湊存放，程式碼中不含任何明碼密碼。

帳號清單（不含密碼）在 config/users.json；初始密碼由環境變數 PQA_SEED_PASSWORD 提供，
執行 scripts/init_users.py 後雜湊寫入 app DB。
"""

from __future__ import annotations

import json
import logging
import time

import bcrypt

from .app_store import AppStore
from .config import PROJECT_ROOT
from .permissions import UserCtx

log = logging.getLogger(__name__)
USERS_FILE = PROJECT_ROOT / "config" / "users.json"
VALID_ROLES = {"R1", "R2", "R3", "R4", "R5"}

_failed: dict[str, list[float]] = {}
MAX_FAILS, LOCK_SECONDS = 5, 300


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def seed_users(store: AppStore, default_password: str, per_user: dict[str, str] | None = None) -> int:
    """依 config/users.json 建立帳號（密碼雜湊後寫入）。"""
    if not default_password or len(default_password) < 8:
        raise ValueError("PQA_SEED_PASSWORD 未設定或少於 8 碼")
    users = json.loads(USERS_FILE.read_text(encoding="utf-8"))
    for u in users:
        if u["role"] not in VALID_ROLES:
            raise ValueError(f"無效角色：{u}")
        pw = (per_user or {}).get(u["username"]) or default_password
        store.upsert_user(u["username"], hash_password(pw), u["role"], u.get("scope", ""), u.get("display_name", ""))
    return len(users)


def login(store: AppStore, username: str, password: str) -> UserCtx | None:
    """驗證帳密；成功回傳 UserCtx（角色與範圍以帳號資料為準）。登入成敗都寫入稽核。"""
    username = (username or "").strip()
    fails = [t for t in _failed.get(username, []) if time.time() - t < LOCK_SECONDS]
    if len(fails) >= MAX_FAILS:
        store.audit(username=username, event="login_locked")
        return None
    u = store.get_user(username)
    ok = bool(u) and bcrypt.checkpw((password or "").encode("utf-8"), u["password_hash"].encode("ascii"))
    if not ok:
        _failed[username] = [*fails, time.time()]
        store.audit(username=username, event="login_failed")
        return None
    _failed.pop(username, None)
    store.audit(username=username, role=u["role"], event="login_ok")
    return UserCtx(username=u["username"], role=u["role"], scope=u["scope"] or "")
