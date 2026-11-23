"""建立／重設帳號：密碼取自環境變數 PQA_SEED_PASSWORD，bcrypt 雜湊後寫入 app DB。

用法：python -m scripts.init_users
"""

from __future__ import annotations

import argparse
import os
import sys

from partsqa.app_store import AppStore
from partsqa.auth import seed_users
from partsqa.config import get_settings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--if-empty", action="store_true", help="帳號表已有資料時不動作（容器重啟用）")
    a = ap.parse_args()
    store = AppStore(get_settings().app_db)
    if a.if_empty and store.list_users():
        print("帳號已存在，略過建立")
        return 0
    pw = os.getenv("PQA_SEED_PASSWORD", "")
    try:
        n = seed_users(store, pw)
    except ValueError as e:
        print(f"失敗：{e}")
        return 2
    print(f"已建立／更新 {n} 個帳號（密碼已雜湊，不顯示）。帳號清單見 config/users.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
