"""示範模式：免密碼角色登入只在示範模式有效、新環境自動準備、範例問題與重播回答。"""

from partsqa import demo
from partsqa.app_store import AppStore


def test_demo_login_only_in_demo_mode(mini, monkeypatch):
    from partsqa.auth import seed_users

    store = AppStore(mini[0].app_db)
    seed_users(store, "test-password-123")
    monkeypatch.setenv("PQA_DEMO_MODE", "0")
    assert demo.demo_login(store, "r2_srv") is None
    monkeypatch.setenv("PQA_DEMO_MODE", "1")
    ctx = demo.demo_login(store, "r2_srv")
    assert ctx.role == "R2" and ctx.scope == "SRV"          # 角色與範圍仍由帳號資料決定
    assert demo.demo_login(store, "not_a_user") is None
    assert store.audit_rows(1)[0]["event"] == "demo_login"


def test_ensure_ready_bootstraps_empty_environment(mini, tmp_path, monkeypatch):
    from partsqa.config import get_settings

    monkeypatch.setenv("PQA_APP_DB", str(tmp_path / "app.db"))
    monkeypatch.setenv("PQA_CLEAN_DB", str(tmp_path / "clean.db"))
    monkeypatch.setenv("PQA_REPORT_DIR", str(tmp_path / "out"))
    s = get_settings()
    store = AppStore(s.app_db)
    done = demo.ensure_ready(s, store)
    assert done == ["已建立範例帳號", "已執行初始資料清理"]
    assert s.clean_db.exists() and len(store.list_users()) > 0
    # 隨機密碼：常見密碼無法登入
    from partsqa.auth import login

    assert login(store, "r1_scm", "password") is None
    assert demo.ensure_ready(s, store) == []                  # 第二次不重做


def test_sample_questions_and_replay():
    qs = demo.sample_questions("r2_srv")
    assert len(qs) >= 2 and all(isinstance(q, str) for q in qs)
    if demo.DEMO_ANSWERS.exists():
        rec = demo.replay_answer("r2_srv", qs[0])
        assert rec is not None and rec["answer"] and rec["model"]
    assert demo.replay_answer("r2_srv", "不存在的問題") is None
