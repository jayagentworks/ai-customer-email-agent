ARG PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.12-slim
FROM ${PYTHON_IMAGE}

ARG APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

WORKDIR /app

RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i "s|http://deb.debian.org/debian|${APT_MIRROR}|g; s|http://deb.debian.org/debian-security|${APT_MIRROR}-security|g" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
      fonts-noto-cjk \
      libreoffice-writer \
      poppler-utils \
      tesseract-ocr \
      tesseract-ocr-chi-sim \
      tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /app/backend/requirements.txt
RUN python -m pip install --upgrade pip -i ${PIP_INDEX_URL} \
    && python -m pip install -r /app/backend/requirements.txt -i ${PIP_INDEX_URL}

COPY backend /app/backend
WORKDIR /app/backend

EXPOSE 8010
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010"]
