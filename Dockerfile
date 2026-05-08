FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py config.py dashboard_server.py make_call.py create_trunk.py setup_trunk.py list_trunks.py ./
COPY web ./web

CMD ["python", "agent.py", "start"]
