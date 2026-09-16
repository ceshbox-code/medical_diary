FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# GigaChat API uses certificates issued by the Russian Trusted Root CA.
# Install the official MinDigital root/subordinate CA certificates into
# Debian's system trust store; TLS verification remains enabled at runtime.
RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl fonts-dejavu-core \
  && mkdir -p /usr/local/share/ca-certificates/russian-trusted \
  && curl -fsSL -k "https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt" \
       -o /usr/local/share/ca-certificates/russian-trusted/russian_trusted_root_ca.crt \
  && curl -fsSL -k "https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt" \
       -o /usr/local/share/ca-certificates/russian-trusted/russian_trusted_sub_ca.crt \
  && update-ca-certificates \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py backup.py ./

RUN mkdir -p /data /backups

EXPOSE 8000

CMD ["gunicorn", "--workers", "1", "--threads", "8", "--timeout", "120", "--bind", "0.0.0.0:8000", "app:app"]
