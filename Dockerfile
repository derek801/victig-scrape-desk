FROM python:3.11-slim
WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 DATA_DIR=/data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY data/counties.csv data/calibration.json data/pb_history.json ./data/
# One worker on purpose: the scheduler is a thread of this process and SQLite lives on one disk.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
