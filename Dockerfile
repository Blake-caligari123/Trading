# 24/7 hosting (VPS, Fly.io, Railway, Render, a home server…)
FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV HOST=0.0.0.0 PORT=8000 PYTHONUNBUFFERED=1
EXPOSE 8000
# DASHBOARD_TOKEN is required when listening on 0.0.0.0 (the bot refuses to start without it).
# Mount ./strategies and ./data as volumes to keep evolved strategies and logs across restarts.
CMD ["python", "-m", "bot"]
