"""組裝層：建立 Engine（疊加人工審核結果）、AppStore、QAAgent；UI、CLI、評測、MCP 共用。"""

from __future__ import annotations

import sqlite3

import pandas as pd

from .agent import QAAgent
from .app_store import AppStore
from .config import Settings, get_settings
from .engine import Engine
from .permissions import UserCtx


def build_engine(s: Settings, store: AppStore) -> Engine:
    dec = store.decisions()
    approved = {k for k, v in dec.items() if v == "approved"}
    rejected = {k for k, v in dec.items() if v == "rejected"}
    return Engine(s.clean_db, approved, rejected)


def build_agent(s: Settings | None = None, client=None) -> QAAgent:
    s = s or get_settings()
    store = AppStore(s.app_db)
    return QAAgent(build_engine(s, store), store, s, client=client)


def review_queue(s: Settings, ctx: UserCtx, store: AppStore, include_decided: bool = False) -> pd.DataFrame:
    """R4 待人工確認清單：只列負責物料類別的項目（依主料／料號類別）。"""
    if ctx.role != "R4":
        raise PermissionError("只有 R4 零件工程師可審核")
    con = sqlite3.connect(s.clean_db)
    try:
        df = pd.read_sql("select review_key, item_type, part_no, commodity, summary from review_items", con)
    finally:
        con.close()
    # 無物料類別的項目（例如半成品之間的 BOM 循環）沒有專屬負責群組，開放所有 R4 確認
    df = df[df["commodity"].isin(ctx.commodities) | df["commodity"].fillna("").eq("")]
    dec = store.decisions()
    df = df.assign(decision=df["review_key"].map(dec).fillna("pending"))
    if not include_decided:
        df = df[df["decision"] == "pending"]
    return df.reset_index(drop=True)
