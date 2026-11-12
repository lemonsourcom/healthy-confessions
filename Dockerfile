FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends fonts-liberation fonts-noto-core fonts-noto-cjk libcairo2 libpango-1.0-0 libpangocairo-1.0-0 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "server.py"]
