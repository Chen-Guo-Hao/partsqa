"""Integration/Security：權限過濾、工具註冊、Mock LLM 對話迴圈、稽核與金鑰保護。"""

import re
from pathlib import Path

import pytest

from partsqa.app_store import AppStore
from partsqa.permissions import Scope, UserCtx
from partsqa.tools import ToolEnv, run_tool, tool_schemas

R1 = UserCtx("u1", "R1")
R2SRV = UserCtx("u2", "R2", "SRV")
R2EDGE = UserCtx("u2a", "R2", "EDGE")
R3TT = UserCtx("u3", "R3", "TW-HC")
R4P = UserCtx("u4", "R4", "電源與被動元件")
R4S = UserCtx("u4s", "R4", "運算晶片")
R5 = UserCtx("u5", "R5")


def _run(engine, ctx, name, args):
    return run_tool(ToolEnv(engine, Scope(engine), ctx), name, args)


def test_tool_schemas_valid():
    names = {t["name"] for t in tool_schemas()}
    assert {"shortage_check", "check_alternates", "where_used", "get_supplier_price"} <= names
    for t in tool_schemas():
        assert t["input_schema"]["type"] == "object"


def test_r2_cannot_see_other_product_line(engine):
    out, log = _run(engine, R2EDGE, "get_bom", {"part_no": "FGS100-01"})
    assert log["status"] == "denied" and out["permission_denied"]
    # 反查共用料時不揭露 DC 成品、也不提示有被隱藏的上階
    out, _ = _run(engine, R2EDGE, "where_used", {"part_no": "RES-000001"})
    assert set(out["finished_goods_affected"]) == {"FGE100-01"} and "note" not in out


def test_r3_plant_restriction(engine):
    _, log = _run(engine, R3TT, "get_stock_and_transit", {"part_no": "CAP-000001", "plant": "TW-KH"})
    assert log["status"] == "denied"
    out, log = _run(engine, R3TT, "get_stock_and_transit", {"part_no": "CAP-000001", "plant": "TWHC", "prod_date": "2026-11-20"})
    assert log["status"] == "ok" and "unit_price_usd" not in out["po_in_time"][0] and "supplier" in out["po_in_time"][0]


def test_price_visibility_by_role(engine):
    parts = {"part_nos": ["CAP-000001", "CAP-000002"]}
    r4, _ = _run(engine, R4P, "get_supplier_price", parts)
    assert r4["unit_cost_usd"]["CAP-000001"] == 0.5 and r4["price_difference_usd"] == pytest.approx(0.1)
    r3, _ = _run(engine, R3TT, "get_supplier_price", parts)
    assert "unit_cost_usd" not in r3 and "suppliers" in r3
    r2, _ = _run(engine, R2SRV, "get_supplier_price", parts)
    assert r2["supplier_count"] == "少於 3" and "suppliers" not in r2
    r1, _ = _run(engine, R1, "get_supplier_price", parts)
    # R1：只有物料類別維度的統計（不指名料號、不含 min/max），且不含個別單價
    assert "unit_cost_usd" not in r1 and "suppliers" not in r1
    stats = r1["unit_cost_stats_by_commodity"]["CAP"]
    assert set(stats) == {"n", "avg_unit_cost_usd"} and stats["n"] >= 3


def test_r4_scope_is_commodity_plus_parents(engine):
    out, log = _run(engine, R4S, "where_used", {"part_no": "GPU-000001"})
    assert log["status"] == "ok"
    _, log = _run(engine, R4S, "get_part_info", {"part_nos": ["CAP-000001"]})
    out, _ = _run(engine, R4S, "get_part_info", {"part_nos": ["CAP-000001"]})
    assert out.get("hidden_out_of_scope") == 1


def test_r5_cannot_query_data(engine):
    out, log = _run(engine, R5, "get_part_info", {"part_nos": ["CAP-000001"]})
    assert log["status"] == "denied"


def test_invalid_args_reported(engine):
    out, log = _run(engine, R1, "shortage_check", {"root": "FGS100-01", "qty": -1, "plant": "XX"})
    assert log["status"] == "invalid_args" and out["error"] == "參數錯誤"


def test_agent_loop_with_mock_and_audit(mini, engine):
    from partsqa.agent import QAAgent
    from partsqa.llm_client import MockLLM

    store = AppStore(mini[0].app_db)
    mock = MockLLM([{"tool": "shortage_check", "input": {"root": "FGS100-01", "qty": 100, "plant": "TW-HC", "prod_date": "2026-11-20"}},
                    {"text": "CAP-000001 缺 104"}])
    agent = QAAgent(engine, store, mini[0], client=mock)
    res = agent.ask(R2SRV, "新竹廠 11/20 生產 FGS100-01 100 台缺什麼？")
    assert res.answer == "CAP-000001 缺 104"
    assert res.tool_logs[0]["status"] == "ok" and res.usage.calls == 2
    # 工具結果以 tool_result 回傳，且與工具呼叫放在同一輪
    last = mock.calls[1]["messages"][-1]["content"][0]
    assert last["type"] == "tool_result" and "104" in last["content"]
    row = store.audit_rows(1)[0]
    assert row["event"] == "question" and row["username"] == "u2" and row["input_tokens"] == 200


def test_r5_answer_is_local_and_audited(mini, engine):
    from partsqa.agent import QAAgent
    from partsqa.llm_client import MockLLM

    mock = MockLLM()
    agent = QAAgent(engine, AppStore(mini[0].app_db), mini[0], client=mock)
    res = agent.ask(R5, "列出 CAP-000001 庫存")
    assert "無法查看任何料件資料" in res.answer and mock.calls == []


def test_login_and_password_hashing(mini, monkeypatch):
    from partsqa.auth import login, seed_users

    store = AppStore(mini[0].app_db)
    seed_users(store, "test-password-123")
    assert store.get_user("r2_srv")["password_hash"].startswith("$2")
    assert login(store, "r2_srv", "wrong") is None
    ctx = login(store, "r2_srv", "test-password-123")
    assert ctx.role == "R2" and ctx.scope == "SRV"


def test_no_secrets_in_source():
    root = Path(__file__).resolve().parent.parent
    pat = re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}|ANTHROPIC_API_KEY\s*=\s*[A-Za-z0-9]{20,}")
    for p in list(root.glob("partsqa/*.py")) + list(root.glob("scripts/*.py")) + [root / "app.py", root / ".env.example"]:
        if p.exists():
            assert not pat.search(p.read_text(encoding="utf-8")), p


def test_cleaning_cli_requires_r5(mini, monkeypatch, capsys):
    import sys

    from partsqa.auth import seed_users
    from scripts import run_clean

    seed_users(AppStore(mini[0].app_db), "test-password-123")
    monkeypatch.setenv("PQA_LOGIN_PASSWORD", "test-password-123")
    monkeypatch.setattr(sys, "argv", ["run_clean", "--user", "r4_power"])
    assert run_clean.main() == 1
    assert "僅 R5" in capsys.readouterr().out


def test_model_routing_and_fallback(mini, engine, monkeypatch):
    from partsqa.agent import QAAgent, choose_model
    from partsqa.config import get_settings
    from partsqa.llm_client import MockLLM

    monkeypatch.setenv("PQA_MODEL_AGENT", "claude-haiku-5-5")
    monkeypatch.setenv("PQA_MODEL_ESCALATE", "claude-sonnet-5-5")
    monkeypatch.setenv("PQA_ROUTING", "auto")
    s = get_settings()
    assert choose_model(s, "FGS100-01 用到哪些電容？") == ("claude-haiku-5-5", "default")
    assert choose_model(s, "交期拉長我們該怎麼應變？")[0] == "claude-sonnet-5-5"
    # 主模型拒答 → 自動以升級模型重做
    mock = MockLLM([{"refusal": True}, {"text": "由升級模型回答"}])
    res = QAAgent(engine, AppStore(s.app_db), s, client=mock).ask(R1, "CAP-000001 的屬性？")
    assert res.answer == "由升級模型回答" and res.route == "default+fallback"
    assert [c["model"] for c in mock.calls] == ["claude-haiku-5-5", "claude-sonnet-5-5"]
