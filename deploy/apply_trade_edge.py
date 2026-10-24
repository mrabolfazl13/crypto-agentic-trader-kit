"""Install the trade.absadeghi.ir edge: unpack the bundle, wire compose and nginx.

Idempotent: every edit is anchored on text that is present once, a timestamped
backup is written before the first change, and re-running with an already
patched file is a no-op. Run on the server as the ``ubuntu`` user.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

HOME = Path("/home/ubuntu")
TARGET = HOME / "trade"
BUNDLE = Path("/tmp/trade-bundle.tgz")
COMPOSE = HOME / "docker-compose.yml"
NGINX = HOME / "nginx" / "nginx.conf"

COMPOSE_SERVICE = """  # ---------------- crypto agentic trader (trade.absadeghi.ir) ----------------
  trade_backend:
    build:
      context: ./trade/backend
      dockerfile: Dockerfile
    image: trade-backend:latest
    container_name: trade_backend
    restart: always
    volumes:
      - ./trade/state:/app/state
    networks: [edge]
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health')"]
      interval: 30s
      timeout: 5s
      retries: 3
"""

NGINX_BLOCK = """# ─── Agentic Trader (trade.absadeghi.ir): SPA at /, API under /api/ ───
server {
    listen 80;
    listen 443 ssl;
    ssl_certificate     /etc/nginx/certs/edge.crt;
    ssl_certificate_key /etc/nginx/certs/edge.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:SSL:10m;
    http2 on;
    server_name trade.absadeghi.ir;
    resolver 127.0.0.11 valid=30s;
    client_max_body_size 16m;
    root /usr/share/nginx/trade-frontend;
    index index.html;

    location /api/ {
        set $up http://trade_backend:8000;
        proxy_pass $up;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }

    location /assets/ {
        expires 30d;
        add_header Cache-Control "public, immutable";
        try_files $uri =404;
    }

    location = /index.html {
        add_header Cache-Control "no-store";
    }

    location / {
        try_files $uri $uri/ /index.html;
    }
}

"""

NGINX_ANCHOR = "# ─── Futsal main SPA (default) ───"
MOUNT_ANCHOR = "      - ./repairos-web/dist:/usr/share/nginx/repairos-frontend:ro\n"
MOUNT_LINE = "      - ./trade/frontend/dist:/usr/share/nginx/trade-frontend:ro\n"


def backup(path: Path) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    shutil.copy2(path, path.with_name(f"{path.name}.bak-trade-{stamp}"))


def unpack() -> None:
    if not BUNDLE.exists():
        raise SystemExit(f"bundle missing: {BUNDLE}")
    TARGET.mkdir(parents=True, exist_ok=True)
    with tarfile.open(BUNDLE, "r:gz") as tar:
        tar.extractall(TARGET, filter="data")  # our own archive, fixed destination
    print(f"unpacked: {TARGET}")


def clear_proxy() -> None:
    path = TARGET / "backend" / "config.yaml"
    text = path.read_text(encoding="utf-8")
    replaced, count = re.subn(r'(?m)^(\s*)proxy:\s*"[^"]*".*$', r'\1proxy: ""', text, count=1)
    if count:
        path.write_text(replaced, encoding="utf-8")
        print("config: proxy cleared for the server (Gate is reachable directly)")
    else:
        print("config: proxy line already clear or absent")


def patch_compose() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    if "trade_backend:" in text:
        print("compose: trade_backend already present")
    else:
        backup(COMPOSE)
        if not text.endswith("\n"):
            text += "\n"
        text = f"{text}\n{COMPOSE_SERVICE}"
        COMPOSE.write_text(text, encoding="utf-8")
        print("compose: trade_backend appended")
    text = COMPOSE.read_text(encoding="utf-8")
    if MOUNT_LINE.strip() in text:
        print("compose: nginx mount already present")
        return
    if MOUNT_ANCHOR not in text:
        raise SystemExit("compose: nginx mount anchor not found")
    backup(COMPOSE)
    text = text.replace(MOUNT_ANCHOR, MOUNT_ANCHOR + MOUNT_LINE, 1)
    COMPOSE.write_text(text, encoding="utf-8")
    print("compose: nginx volume for trade-frontend added")


def patch_nginx() -> None:
    text = NGINX.read_text(encoding="utf-8")
    if "server_name trade.absadeghi.ir;" in text:
        print("nginx: trade vhost already present")
        return
    if NGINX_ANCHOR not in text:
        raise SystemExit("nginx: default server anchor not found")
    backup(NGINX)
    text = text.replace(NGINX_ANCHOR, NGINX_BLOCK + NGINX_ANCHOR, 1)
    NGINX.write_text(text, encoding="utf-8")
    print("nginx: trade vhost inserted before the default server")


def check_nginx() -> None:
    result = subprocess.run(
        ["docker", "run", "--rm", "--network", "ubuntu_futsal_network",
         "-v", f"{HOME}/nginx/nginx.conf:/etc/nginx/conf.d/default.conf:ro",
         "-v", f"{HOME}/nginx/ssl:/etc/nginx/certs:ro",
         "nginx:alpine", "nginx", "-t"],
        capture_output=True, text=True,
    )
    print(result.stderr.strip() or result.stdout.strip())
    if result.returncode != 0:
        raise SystemExit("nginx config test failed; nothing was recreated")


def main() -> int:
    unpack()
    clear_proxy()
    patch_compose()
    patch_nginx()
    check_nginx()
    print("edge: configuration written; run the compose commands next")
    return 0


if __name__ == "__main__":
    sys.exit(main())
