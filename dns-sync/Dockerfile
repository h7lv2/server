FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dns_sync.py .
RUN chmod +x dns_sync.py

CMD ["python", "-u", "dns_sync.py"]
