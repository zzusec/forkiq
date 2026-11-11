FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --uid 10001 --create-home forkiq && mkdir /state && chown forkiq:forkiq /state
COPY app ./app
USER forkiq
ENV FORKIQ_STATE_DIR=/state FORKIQ_PEM_PATH=/state/app.pem PYTHONUNBUFFERED=1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8792", "--workers", "1"]
