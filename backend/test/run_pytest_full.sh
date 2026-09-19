#!/usr/bin/env bash
# Wrapper để chạy pytest full suite trong background — user rule cấm dùng
# 2>&1 | tail cho lệnh dài. Output stream ra 2 nơi: stdout (control_bash_process
# đọc được) + file log /tmp/ideal_qr_pytest.log để backup.

set -u
cd "$(dirname "$0")/.."

exec .venv/bin/python -m pytest \
    -v --tb=short --color=no \
    2>&1 | tee /tmp/ideal_qr_pytest.log
