FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV HOST=0.0.0.0
ENV PORT=8787
ENV KONDO_FOLK_RELOAD=false

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY kondo_folk_sync ./kondo_folk_sync

CMD ["python", "-m", "kondo_folk_sync.run"]
