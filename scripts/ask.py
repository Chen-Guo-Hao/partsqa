"""命令列問答（與 UI 相同的 Agent、權限與稽核）。

用法：
  python -m scripts.ask --user r2_srv "新竹廠 10 月 20 日要生產 FGS208-01 共 50 台，哪些原材料會缺料？"
  python -m scripts.ask --user r2_srv --chat          # 互動模式（同一對話可追問）
密碼從環境變數 PQA_LOGIN_PASSWORD 讀取；未設定時以隱藏輸入詢問。
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import uuid

from partsqa.auth import login
from partsqa.service import build_agent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--model", default=None)
    ap.add_argument("--chat", action="store_true")
    ap.add_argument("question", nargs="*")
    a = ap.parse_args()
    agent = build_agent()
    pw = os.getenv("PQA_LOGIN_PASSWORD") or getpass.getpass("密碼：")
    ctx = login(agent.store, a.user, pw)
    if ctx is None:
        print("登入失敗")
        return 1
    conv = uuid.uuid4().hex[:12]
    history: list[dict] = []
    questions = [" ".join(a.question)] if a.question else []
    while True:
        if not questions:
            if not a.chat:
                break
            try:
                q = input("\n問題> ").strip()
            except EOFError:
                break
            if not q:
                break
        else:
            q = questions.pop(0)
        res = agent.ask(ctx, q, history, conv, model=a.model)
        print(res.answer)
        print(f"\n[trace {res.trace_id} | {res.model} | in {res.usage.total_input} out {res.usage.output_tokens} | "
              f"tools {len(res.tool_logs)} | {res.latency_ms / 1000:.1f}s | US$ {res.usage.cost_usd:.4f}]")
        history += [{"role": "user", "content": q}, {"role": "assistant", "content": res.answer}]
        if res.error:
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
