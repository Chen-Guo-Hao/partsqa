# PartsQA：AI 伺服器料件智慧問答系統（Streamlit UI + 問答 Agent；資料清理由 R5 在 UI 啟動）
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY partsqa/ partsqa/
COPY scripts/ scripts/
COPY eval/ eval/
COPY config/ config/
COPY data/data_dictionary.md data/data_dictionary.md
COPY app.py mcp_server.py docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh && useradd -m appuser && mkdir -p data/raw data/processed data/app && chown -R appuser /app
USER appuser

EXPOSE 8501
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health')" || exit 1
ENTRYPOINT ["./docker-entrypoint.sh"]
