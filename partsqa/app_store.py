"""應用程式資料庫（SQLite）：帳號、稽核紀錄、人工審核決策、清理作業紀錄。

與清理後資料庫分開存放：重跑清理不會覆蓋帳號、稽核與核准紀錄。
敏感內容（密碼、API Key）不寫入任何表；密碼只存 bcrypt 雜湊。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, role TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '', display_name TEXT, active INTEGER NOT NULL DEFAULT 1, created_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, trace_id TEXT NOT NULL, username TEXT, role TEXT,
    event TEXT NOT NULL, conversation_id TEXT, question TEXT, answer TEXT, tools TEXT, denied_count INTEGER DEFAULT 0,
    model TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0, llm_calls INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0, latency_ms INTEGER, error TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_log(ts);
CREATE TABLE IF NOT EXISTS review_decisions (
    review_key TEXT PRIMARY KEY, item_type TEXT, part_no TEXT, commodity TEXT, decision TEXT NOT NULL,
    reviewer TEXT NOT NULL, note TEXT, summary TEXT, ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, review_key TEXT, decision TEXT, reviewer TEXT, note TEXT, ts TEXT
);
"""


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class AppStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._con() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _con(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    # ------------------------------------------------------------------ 帳號
    def upsert_user(self, username: str, password_hash: str, role: str, scope: str, display_name: str) -> None:
        with self._con() as c:
            c.execute(
                "INSERT INTO users(username,password_hash,role,scope,display_name,active,created_at) VALUES(?,?,?,?,?,1,?) "
                "ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash, role=excluded.role, "
                "scope=excluded.scope, display_name=excluded.display_name",
                (username, password_hash, role, scope, display_name, now()),
            )

    def get_user(self, username: str) -> dict | None:
        with self._con() as c:
            r = c.execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
        return dict(r) if r else None

    def list_users(self) -> list[dict]:
        with self._con() as c:
            return [dict(r) for r in c.execute("SELECT username,role,scope,display_name,active,created_at FROM users ORDER BY username")]

    # ------------------------------------------------------------------ 稽核
    def audit(self, **kw) -> str:
        trace_id = kw.pop("trace_id", None) or uuid.uuid4().hex[:12]
        if isinstance(kw.get("tools"), list | dict):
            kw["tools"] = json.dumps(kw["tools"], ensure_ascii=False)
        cols = ["ts", "trace_id", *kw.keys()]
        with self._con() as c:
            c.execute(f"INSERT INTO audit_log({','.join(cols)}) VALUES({','.join('?' * len(cols))})", [now(), trace_id, *kw.values()])
        return trace_id

    def audit_rows(self, limit: int = 500) -> list[dict]:
        with self._con() as c:
            return [dict(r) for r in c.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))]

    def usage_summary(self) -> list[dict]:
        with self._con() as c:
            return [dict(r) for r in c.execute(
                "SELECT model, COUNT(*) queries, SUM(llm_calls) llm_calls, SUM(input_tokens) input_tokens, "
                "SUM(output_tokens) output_tokens, SUM(cache_read_tokens) cache_read_tokens, "
                "SUM(cache_write_tokens) cache_write_tokens, ROUND(SUM(cost_usd),4) cost_usd, "
                "ROUND(AVG(latency_ms)/1000.0,1) avg_latency_s FROM audit_log WHERE event='question' GROUP BY model")]

    # ------------------------------------------------------------------ 人工審核
    def decide(self, review_key: str, decision: str, reviewer: str, item: dict, note: str = "") -> None:
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision 必須為 approved 或 rejected")
        with self._con() as c:
            c.execute(
                "INSERT INTO review_decisions(review_key,item_type,part_no,commodity,decision,reviewer,note,summary,ts) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(review_key) DO UPDATE SET decision=excluded.decision, "
                "reviewer=excluded.reviewer, note=excluded.note, ts=excluded.ts",
                (review_key, item.get("item_type"), item.get("part_no"), item.get("commodity"), decision, reviewer, note,
                 item.get("summary"), now()),
            )
            c.execute("INSERT INTO review_history(review_key,decision,reviewer,note,ts) VALUES(?,?,?,?,?)",
                      (review_key, decision, reviewer, note, now()))

    def decisions(self) -> dict[str, str]:
        with self._con() as c:
            return {r["review_key"]: r["decision"] for r in c.execute("SELECT review_key, decision FROM review_decisions")}

    def decision_rows(self) -> list[dict]:
        with self._con() as c:
            return [dict(r) for r in c.execute("SELECT * FROM review_decisions ORDER BY ts DESC")]
