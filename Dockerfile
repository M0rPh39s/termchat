# termchat server image.
#
# Runs the TCP chat server and (by default) the browser client in one
# container. The SQLite database lives on a volume at /data so it survives
# container restarts and rebuilds.
#
#   docker build -t termchat .
#   docker run -d --name termchat -p 9009:9009 -p 8080:8080 \
#       -v termchat-data:/data termchat
#
# or just `docker compose up -d`.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first so this layer is cached across code changes.
# cryptography ships manylinux wheels, so no compiler/Rust toolchain needed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY . .

# Run as a non-root user and let it own the data volume.
RUN useradd --system --create-home termchat \
    && mkdir -p /data \
    && chown -R termchat:termchat /data /app
USER termchat

# Persist the database outside the image.
VOLUME ["/data"]

# 9009: TCP clients (TUI/GUI).  8080: browser client.
EXPOSE 9009 8080

# Override the CMD to change ports, drop --web-port, etc.
CMD ["python", "run_server.py", \
     "--host", "0.0.0.0", "--port", "9009", \
     "--web-host", "0.0.0.0", "--web-port", "8080", \
     "--db", "/data/termchat.db"]
