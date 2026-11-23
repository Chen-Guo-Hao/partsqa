"""命令列執行資料清理：與 UI 相同，僅 R5 系統管理員可啟動，並寫入稽核紀錄。

用法：python -m scripts.run_clean --user r5_admin
密碼從環境變數 PQA_LOGIN_PASSWORD 讀取；未設定時以隱藏輸入詢問。
輸出：PQA_CLEAN_DB、PQA_REPORT_DIR/clean_stats.json、專案根目錄的 資料清理說明報告.md。
螢幕上只輸出筆數統計，不輸出任何料件內容。
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys

from partsqa.app_store import AppStore
from partsqa.auth import login
from partsqa.cleaning import run_cleaning
from partsqa.config import get_settings
from partsqa.report import write_cleaning_report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True, help="R5 系統管理員帳號")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s = get_settings()
    store = AppStore(s.app_db)
    ctx = login(store, a.user, os.getenv("PQA_LOGIN_PASSWORD") or getpass.getpass("密碼："))
    if ctx is None:
        print("登入失敗")
        return 1
    if ctx.role != "R5":
        store.audit(username=ctx.username, role=ctx.role, event="cleaning_denied")
        print("權限不足：清理作業僅 R5 系統管理員可啟動")
        return 1
    try:
        stats = run_cleaning(s.data_dir, s.clean_db, s.report_dir)
    except FileNotFoundError as e:
        store.audit(username=ctx.username, role=ctx.role, event="cleaning_failed", error=str(e))
        logging.error("%s", e)
        return 2
    path = write_cleaning_report(stats, s.report_dir)
    store.audit(username=ctx.username, role=ctx.role, event="cleaning_run",
                answer=json.dumps({k: {x: v[x] for x in ["raw_rows", "ok", "invalid", "pending"]} for k, v in stats["counts"].items()},
                                  ensure_ascii=False))
    print(f"清理完成：耗時 {stats['elapsed_s']} 秒；待人工確認 {stats['review_items']} 筆")
    for f, c in stats["counts"].items():
        print(f"  {f}: 原始 {c['raw_rows']} → 儲存 {c['stored_rows']}（可用 {c['ok']}／無效 {c['invalid']}／待確認 {c['pending']}）")
    print(f"清理說明報告：{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
