# MTAR on Railway. Railway builds this Dockerfile automatically; no other
# build or start settings are needed.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Railway sets PORT; start.py serves Streamlit on it and starts the
# Gmail/Drive checker. Data goes to the Railway volume when one is attached.
CMD ["python", "start.py"]
