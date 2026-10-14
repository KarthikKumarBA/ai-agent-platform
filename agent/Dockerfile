FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code lives in /app/agent so the audit log goes to /app/audit
COPY . /app/agent

RUN useradd -m appuser \
    && mkdir -p /app/audit \
    && chown -R appuser:appuser /app/audit
USER appuser

EXPOSE 8090

HEALTHCHECK --interval=15s --timeout=3s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/health')"

CMD ["uvicorn", "server:app", "--app-dir", "/app/agent", "--host", "0.0.0.0", "--port", "8090"]
