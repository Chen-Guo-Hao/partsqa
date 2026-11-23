"""AI 伺服器料件智慧問答系統（PartsQA）：Streamlit 介面。

啟動：streamlit run app.py
- 登入（帳號密碼，bcrypt 驗證）→ 依角色顯示功能：
  R1–R4：問答機器人；R4 另有「人工審核」；R5：資料清理、稽核紀錄、token 用量、帳號清單（看不到料件資料）。
- 示範模式（PQA_DEMO_MODE=1）：一鍵以範例角色登入、範例問題按鈕；未設定 API Key 時重播評測中的真實回答。
"""

from __future__ import annotations

import json
import uuid

import pandas as pd
import streamlit as st

from partsqa import demo
from partsqa.agent import QAAgent
from partsqa.app_store import AppStore
from partsqa.auth import login
from partsqa.cleaning import run_cleaning
from partsqa.config import get_settings
from partsqa.domain import get_domain
from partsqa.permissions import ROLE_NAMES, UserCtx
from partsqa.report import write_cleaning_report
from partsqa.service import build_engine, review_queue

st.set_page_config(page_title="PartsQA", page_icon=":material/memory:", layout="wide")
settings = get_settings()


@st.cache_resource
def get_store() -> AppStore:
    return AppStore(settings.app_db)


@st.cache_resource(max_entries=1)
def get_engine(version: int):
    """引擎載入約數秒，以快取共用；version 改變（清理或審核後）時重新載入。"""
    return build_engine(settings, get_store())


if "engine_version" not in st.session_state:
    st.session_state.engine_version = 0
store = get_store()
DEMO = demo.is_demo()


@st.cache_resource
def demo_bootstrap() -> list[str]:
    """示範模式：新部署環境自動建立範例帳號與初始清理（每個伺服器程序只做一次）。"""
    return demo.ensure_ready(settings, store)


if DEMO:
    demo_bootstrap()


def start_session(user: UserCtx) -> None:
    st.session_state.user = user
    st.session_state.messages = []
    st.session_state.conversation_id = uuid.uuid4().hex[:12]
    st.session_state.live_count = 0


def engine_or_none():
    try:
        return get_engine(st.session_state.engine_version)
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------- 登入
ctx: UserCtx | None = st.session_state.get("user")
if ctx is None:
    st.title(get_domain().system_name)
    if DEMO:
        st.info("示範模式：選一個角色直接登入。不同角色能看到的資料不同（例如產品線經理看不到單價、系統管理員看不到料件資料）。"
                + ("" if settings.has_api_key else "目前未連線模型，範例問題會顯示 Claude Haiku 在評測中實際產生的回答。"),
                icon=":material/science:")
        users = [u for u in store.list_users() if u["active"]]
        cols = st.columns(3)
        for i, u in enumerate(users):
            with cols[i % 3].container(border=True):
                st.markdown(f"**{u['display_name']}**")
                st.caption(f"`{u['username']}` · {u['role']}")
                if st.button("以此角色登入", key=f"demo_{u['username']}", icon=":material/login:", width="stretch"):
                    user = demo.demo_login(store, u["username"])
                    if user:
                        start_session(user)
                        st.rerun()
    with st.expander("以帳號密碼登入", expanded=not DEMO):
        with st.form("login"):
            username = st.text_input("帳號", key="login_user")
            password = st.text_input("密碼", type="password", key="login_pw")
            submitted = st.form_submit_button("登入", icon=":material/login:")
        if submitted:
            user = login(store, username, password)
            if user:
                start_session(user)
                st.rerun()
            st.error("帳號或密碼錯誤（連續失敗 5 次將鎖定 5 分鐘）")
    st.stop()

with st.sidebar:
    st.subheader(f"{ctx.username}")
    st.caption(f"{ctx.role} {ROLE_NAMES[ctx.role]}{'（' + ctx.scope + '）' if ctx.scope else ''}")
    if ctx.can_see_parts and st.button("新對話", icon=":material/add_comment:", width="stretch"):
        st.session_state.messages = []
        st.session_state.conversation_id = uuid.uuid4().hex[:12]
        st.rerun()
    if DEMO:
        st.caption("示範模式" + ("（即時呼叫模型，每次登入最多 " + str(demo.max_live_questions()) + " 題）"
                                 if settings.has_api_key else "（重播評測回答，不呼叫模型）"))
    if st.button("登出", icon=":material/logout:", width="stretch"):
        store.audit(username=ctx.username, role=ctx.role, event="logout")
        st.session_state.clear()
        st.rerun()

# ---------------------------------------------------------------- 依角色選頁
views = ["問答機器人"] if ctx.can_see_parts else []
if ctx.role == "R4":
    views.append("人工審核")
if ctx.role == "R5":
    views += ["資料清理", "稽核紀錄", "帳號"]
view = st.segmented_control("功能", views, default=views[0], key="view", label_visibility="collapsed") or views[0]

# ---------------------------------------------------------------- 問答
if view == "問答機器人":
    st.header("料件問答機器人")
    engine = engine_or_none()
    if engine is None:
        st.warning("尚未產生清理後資料，請由系統管理員（R5）執行資料清理。")
        st.stop()
    agent = QAAgent(engine, store, settings)
    for m in st.session_state.messages:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])
    picked = None
    if DEMO:
        samples = demo.sample_questions(ctx.username)
        if samples:
            with st.expander("範例問題（點選直接提問）", expanded=not st.session_state.messages, icon=":material/lightbulb:"):
                for i, q in enumerate(samples):
                    if st.button(q, key=f"sample_{i}", width="stretch"):
                        picked = q
    typed = st.chat_input("輸入問題，例如：新竹廠 10 月 20 日生產 FGS208-01 共 50 台會缺哪些料？", submit_mode="disable", key="chat")
    prompt = typed or picked
    if prompt:
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            replay = DEMO and not settings.has_api_key
            if replay:
                rec = demo.replay_answer(ctx.username, prompt)
                if rec:
                    answer = rec["answer"]
                    st.markdown(answer)
                    st.caption(f"示範重播：{rec['model']} 於評測中實際產生的回答（未經修改）｜ tokens 輸入 {rec['input_tokens']:,}／輸出 "
                               f"{rec['output_tokens']:,} ｜ 工具呼叫 {rec['tool_calls']} 次 ｜ {rec['latency_s']} 秒")
                else:
                    answer = "示範模式目前未連線模型，只能重播範例問題的回答。請從上方「範例問題」選擇，或於自行部署時設定 ANTHROPIC_API_KEY。"
                    st.markdown(answer)
                store.audit(username=ctx.username, role=ctx.role, event="demo_replay", question=prompt,
                            conversation_id=st.session_state.conversation_id)
            elif DEMO and st.session_state.get("live_count", 0) >= demo.max_live_questions():
                answer = f"示範模式每次登入最多提問 {demo.max_live_questions()} 題，請登出後重新登入。"
                st.markdown(answer)
            else:
                st.session_state.live_count = st.session_state.get("live_count", 0) + 1
                with st.status(":shimmer[查詢中]", type="compact") as status:
                    history = [{"role": m["role"], "content": m["content"]} for m in st.session_state.messages]
                    res = agent.ask(ctx, prompt, history, st.session_state.conversation_id)
                    for t in res.tool_logs:
                        st.write(f"{t['tool']}（{t['status']}，{t['ms']} ms）")
                    status.update(label=f"完成：{len(res.tool_logs)} 次查詢，{res.latency_ms / 1000:.1f} 秒", state="complete")
                answer = res.answer
                st.markdown(answer)
                st.caption(f"trace {res.trace_id} ｜ 模型 {res.model} ｜ tokens 輸入 {res.usage.total_input:,}／輸出 "
                           f"{res.usage.output_tokens:,} ｜ US$ {res.usage.cost_usd:.4f}")
        st.session_state.messages += [{"role": "user", "content": prompt}, {"role": "assistant", "content": answer}]

# ---------------------------------------------------------------- R4 人工審核
elif view == "人工審核":
    st.header("待人工確認資料")
    st.caption(f"只顯示您負責的物料類別：{', '.join(sorted(ctx.commodities))}。核准後立即生效並保留核准紀錄。")
    show_all = st.toggle("顯示已處理項目")
    try:
        q = review_queue(settings, ctx, store, include_decided=show_all)
    except FileNotFoundError:
        st.warning("尚未產生清理後資料。")
        st.stop()
    types = sorted(q["item_type"].unique()) if len(q) else []
    pick = st.pills("類型", types, selection_mode="multi", default=types)
    q = q[q["item_type"].isin(pick or [])]
    st.metric("項目數", len(q))
    note = st.text_input("審核備註（選填）")
    page = q.head(50)
    for r in page.itertuples():
        with st.container(border=True, horizontal=True, vertical_alignment="center"):
            st.markdown(f"**{r.item_type}** · `{r.part_no}` · {r.summary}  \n狀態：{r.decision}")
            if st.button("核准", key=f"ap_{r.review_key}", icon=":material/check:"):
                store.decide(r.review_key, "approved", ctx.username, r._asdict(), note)
                store.audit(username=ctx.username, role=ctx.role, event="review_approved", question=r.review_key)
                st.session_state.engine_version += 1
                st.rerun()
            if st.button("駁回", key=f"rj_{r.review_key}", icon=":material/close:"):
                store.decide(r.review_key, "rejected", ctx.username, r._asdict(), note)
                store.audit(username=ctx.username, role=ctx.role, event="review_rejected", question=r.review_key)
                st.session_state.engine_version += 1
                st.rerun()
    if len(q) > 50:
        st.caption(f"僅顯示前 50 筆（共 {len(q)} 筆），請用類型篩選。")
    st.subheader("核准紀錄")
    rows = [d for d in store.decision_rows() if d["commodity"] in ctx.commodities]
    st.dataframe(pd.DataFrame(rows), alt="核准紀錄")

# ---------------------------------------------------------------- R5 資料清理
elif view == "資料清理":
    st.header("資料清理")
    st.caption("只有 R5 可啟動。此頁只顯示筆數統計與問題代碼，不顯示任何料件內容。")
    if st.button("執行資料清理", icon=":material/cleaning_services:", type="primary"):
        with st.spinner("清理中（約 30–60 秒）"):
            try:
                stats = run_cleaning(settings.data_dir, settings.clean_db, settings.report_dir)
                write_cleaning_report(stats, settings.report_dir)
                store.audit(username=ctx.username, role=ctx.role, event="cleaning_run",
                            answer=json.dumps({k: {x: v[x] for x in ["raw_rows", "ok", "invalid", "pending"]}
                                               for k, v in stats["counts"].items()}, ensure_ascii=False))
                st.session_state.engine_version += 1
                st.session_state.clean_stats = stats
                st.success(f"完成，耗時 {stats['elapsed_s']} 秒；待人工確認 {stats['review_items']} 筆")
            except FileNotFoundError as e:
                store.audit(username=ctx.username, role=ctx.role, event="cleaning_failed", error=str(e))
                st.error(str(e))
    stats = st.session_state.get("clean_stats")
    if stats:
        st.dataframe(pd.DataFrame([{"檔案": k, **{x: v[x] for x in ["raw_rows", "stored_rows", "ok", "invalid", "pending"]}}
                                   for k, v in stats["counts"].items()]), alt="清理前後筆數")
        # 只顯示問題代碼與筆數，不顯示範例值（範例含料號等資料內容）
        st.dataframe(pd.DataFrame(stats["issues"])[["file", "code", "desc", "count", "handling"]], alt="清理問題統計")

# ---------------------------------------------------------------- R5 稽核
elif view == "稽核紀錄":
    st.header("稽核紀錄與 token 用量")
    st.subheader("模型用量與成本")
    st.dataframe(pd.DataFrame(store.usage_summary()), alt="模型用量彙總")
    st.subheader("查詢紀錄")
    df = pd.DataFrame(store.audit_rows(1000))
    if len(df):
        cols = ["ts", "trace_id", "username", "role", "event", "conversation_id", "question", "denied_count", "model",
                "llm_calls", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd",
                "latency_ms", "error", "tools"]
        st.dataframe(df[cols], alt="稽核紀錄")

elif view == "帳號":
    st.header("帳號清單")
    st.dataframe(pd.DataFrame(store.list_users()), alt="帳號清單")
    st.caption("新增／重設帳號：編輯 config/users.json 後執行 python -m scripts.init_users（密碼以環境變數提供，雜湊後存放）。")
