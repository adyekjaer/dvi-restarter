FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY dvi_restarter.py dvi_watchdog.py ./

RUN useradd --system --no-create-home watchdog
USER watchdog

# Healthy if a check succeeded within the last 3 check intervals (default 15 min).
HEALTHCHECK --interval=5m --timeout=10s --start-period=2m \
  CMD python -c "import os,sys,time; f=os.environ.get('HEARTBEAT_FILE','/tmp/dvi-watchdog.heartbeat'); sys.exit(0 if time.time()-os.path.getmtime(f) < 3*int(os.environ.get('CHECK_INTERVAL','300'))+600 else 1)"

CMD ["python", "dvi_watchdog.py"]
