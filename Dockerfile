FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    WEBHOOK_HOST=0.0.0.0 \
    WEBHOOK_PORT=8787

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY gerar_contrato.py padrao_lote.py webhook_server.py excel_import.py ./
COPY entrada/ ./entrada/

RUN mkdir -p saida

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health').read()"

CMD ["python", "webhook_server.py"]
