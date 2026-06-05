# Build stage: install dependencies into /install so they can be copied to the
# runtime image without carrying pip or build tooling into the final layer.
FROM python:3.14.5-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# Runtime stage: copy only the installed packages, not the build tooling.
FROM python:3.14.5-slim

RUN apt-get update && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*

# Minimal system user with no home directory or login shell.
RUN useradd --no-create-home --shell /bin/false meteo

WORKDIR /app
COPY --from=builder /install /usr/local
COPY main.py .

# Generate a self-signed cert baked into the image so the container serves HTTPS by default.
# Outside Docker, mount real certs via SSL_KEY/SSL_CERT, or point them at non-existent paths for plain HTTP.
RUN openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem \
    -days 3650 -nodes -subj "/CN=meteo-server" \
    && chown meteo:meteo key.pem cert.pem

USER meteo
EXPOSE 8080

CMD ["python", "main.py"]
