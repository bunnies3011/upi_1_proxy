"""Package `app.cli` — Debug CLI cục bộ (Requirement 15).

Module marker (không side-effect). Public entry được expose qua `app.cli.main`
và `app.cli.__main__`. KHÔNG import runtime module ở đây để giữ `python -m
app.cli` khởi động nhẹ, KHÔNG kéo theo `app.main`/FastAPI (Requirement 15.9).
"""
