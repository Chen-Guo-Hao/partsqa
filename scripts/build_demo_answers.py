"""從評測結果產生示範模式的重播回答檔（demo/demo_answers.json）。

回答全部是 Claude Haiku 在評測中實際產生、未經修改的內容。
用法：python -m scripts.build_demo_answers [eval/results/<檔名>.json]
未指定時使用最新一份 15 題完整評測結果。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    if len(sys.argv) > 1:
        src = Path(sys.argv[1])
    else:
        cands = [p for p in (ROOT / "eval" / "results").glob("*.json")
                 if len(json.loads(p.read_text(encoding="utf-8")).get("results", [])) >= 15]
        if not cands:
            print("找不到 15 題的完整評測結果，請先執行 python -m eval.run_eval")
            return 2
        src = max(cands, key=lambda p: p.stat().st_mtime)
    data = json.loads(src.read_text(encoding="utf-8"))
    rows = [{"id": r["id"], "user": r["user"], "question": r["question"], "answer": r["answer"],
             "model": r.get("model_used", data.get("model")), "input_tokens": r["input_tokens"],
             "output_tokens": r["output_tokens"], "tool_calls": r["tool_calls"], "latency_s": r["latency_s"],
             "source": src.name} for r in data["results"]]
    out = ROOT / "demo" / "demo_answers.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已輸出 {len(rows)} 筆重播回答：{out}（來源 {src.name}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
