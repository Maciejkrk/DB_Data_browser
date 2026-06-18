FROM python:3.12-slim

WORKDIR /app

COPY server.py /app/server.py
COPY static /app/static

EXPOSE 8788

CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8788", "--data-dir", "/data"]
