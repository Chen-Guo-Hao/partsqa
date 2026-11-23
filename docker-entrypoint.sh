#!/bin/sh
# 啟動流程：首次啟動時依 config/users.json 建立帳號（密碼取自 PQA_SEED_PASSWORD，雜湊後存入 app DB），再啟動 UI。
# 資料清理不在此自動執行：依規定只有 R5 系統管理員可在 UI「資料清理」頁啟動。
set -e
python -m scripts.init_users --if-empty
exec streamlit run app.py --server.address=0.0.0.0 --server.port=8501 --server.headless=true
