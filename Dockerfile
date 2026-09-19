# syntax=docker/dockerfile:1
#
# ideal_qr_tool — multi-stage Docker build.
#
# Mục đích: đóng gói tool chạy trong 1 container Linux đồng nhất, tránh lỗi
# runtime khác nhau giữa host OS (VD: 403 khi chạy trực tiếp trên Windows do
# TLS/JA3 fingerprint của `curl_cffi` build khác glibc/OS so với macOS). Chạy
# trong container Linux luôn cho kết quả giống nhau bất kể host là
# macOS/Windows/Linux.
#
# Stage 1 build frontend (Node) — không có trong image runtime cuối (giảm
# size + attack surface). Stage 2 backend (Python) copy `frontend/dist/`
# build sẵn + cài Python deps, serve cả API + UI qua 1 process uvicorn
# (giống production.sh — main.py tự mount static + SPA fallback).

# ============================================================================
# Stage 1: Build frontend (Vue + Vite)
# ============================================================================
FROM node:20-slim AS frontend-build

WORKDIR /app/frontend

# Cài deps trước — cache layer riêng, đổi source code không phải install lại.
# Dùng `npm install` (không phải `npm ci`) vì package-lock.json hiện tại
# không đồng bộ tuyệt đối với package.json — khớp convention `npm install`
# đã dùng trong setup.sh/build.sh của project.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm install --no-audit --no-fund

COPY frontend/ ./
# Bypass vue-tsc type-check khi build (giống dev.sh/setup.sh/build.sh) — vite
# build thuần bundle JS/CSS, không sinh sourcemap (leak source .vue).
RUN npx vite build

# ============================================================================
# Stage 2: Backend runtime (FastAPI + uvicorn)
# ============================================================================
FROM python:3.13-slim AS backend

# curl: dùng cho HEALTHCHECK. KHÔNG cần build-essential — curl_cffi/Pillow
# publish wheel manylinux sẵn cho cp313 (không compile từ source trên máy
# build image → build nhanh + image nhỏ).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend

# Cài Python deps trước (cache layer riêng tách khỏi COPY source app/) —
# rebuild source code không phải install lại toàn bộ dependencies.
COPY backend/pyproject.toml ./
COPY backend/app ./app
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

# Seed script (ideal.default_issuer + 1 device profile NL) — chạy ở
# entrypoint mỗi lần container start. Idempotent: tự SKIP nếu DB đã có giá
# trị (VD sau restart, volume runtime/ đã persist DB từ lần chạy trước).
COPY backend/test/setup_default_settings.py ./seed_settings.py

# Frontend build từ stage 1 — `app/main.py._backend_root().parent /
# "frontend" / "dist"` = /app/frontend/dist (khớp WORKDIR /app/backend).
COPY --from=frontend-build /app/frontend/dist /app/frontend/dist

# Runtime dirs (SQLite DB, session cache, QR PNG) — thường được override bởi
# volume mount trong docker-compose.yml để persist dữ liệu qua lần restart.
RUN mkdir -p runtime/session_cache runtime/qr

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV IDEAL_QR_TOOL_BIND_HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1

EXPOSE 8989

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8989/ || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8989"]
