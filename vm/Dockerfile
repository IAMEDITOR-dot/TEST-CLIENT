FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates \
       python3 \
       python3-venv \
       qemu-system-x86 \
       qemu-utils \
    && python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir fastapi uvicorn websockets pycryptodome \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /app
COPY vm/server.py vm/manager.html /app/
COPY vm/guest-setup.sh /setup/guest-setup.sh

EXPOSE 6080
CMD ["python3", "/app/server.py", "serve"]
