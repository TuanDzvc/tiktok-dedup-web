FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    IS_CLOUD=true \
    PORT=10000

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium

COPY . .

EXPOSE 10000

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-10000}"]
