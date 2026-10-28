FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DATA_DIR=/app/data
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc build-essential && rm -rf /var/lib/apt/lists/*
COPY requirements-host.txt requirements.txt ./
RUN pip install -r requirements-host.txt && pip install -r requirements.txt
COPY app.py supervisor.py ./
COPY dashboard ./dashboard
RUN mkdir -p /app/data
EXPOSE 8000
CMD ["python", "supervisor.py"]
