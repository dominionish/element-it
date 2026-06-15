FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv ffmpeg git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
COPY constraints.txt /app/constraints.txt
RUN python3 -m pip install --upgrade pip \
    && python3 -m pip install torch==2.7.1+cu126 --index-url https://download.pytorch.org/whl/cu126 \
    && python3 -m pip install -r /app/requirements.txt -c /app/constraints.txt --extra-index-url https://download.pytorch.org/whl/cu126
COPY app /app

EXPOSE 7861
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "7861"]
