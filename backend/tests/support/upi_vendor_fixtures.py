"""NDJSON stream fixtures for the UPI vendor client (`payments/upi`).

Synthesised from the documented `pix.capybara.cv` HAR decode (the raw `.har` is
not in the repo): a `/api/chatgpt/run` response is a newline-delimited JSON
stream — progress objects in the middle, one final `{"stage":"done","result":…}`.

These fixtures deliberately re-chunk the stream at arbitrary BYTE boundaries
(not on line boundaries) so the production line-buffer is exercised against a
JSON object split across two network chunks — a faithful model of how curl_cffi
hands over `aiter_content()` chunks of unpredictable size.

Three streams are provided:
    * success   — 4 progress lines + a final `upi_qr_ready` with a small valid
                  base64 PNG.
    * failure   — 4 progress lines + a final `result.ok=false` (business error).
    * truncated — progress lines then a final `done` line cut off mid-object
                  (no trailing newline, invalid JSON) → the client must raise a
                  stream error, NOT return a result.

Plus a cancellation stream: one complete progress line, then a long silent gap
before the next chunk (models the vendor's 45-90s `fetching` hold), so a test
can prove `run()` is interruptible mid-gap rather than only between lines.
"""

from __future__ import annotations

import json

# A minimal but genuinely valid 1x1 RGB PNG (verified: decodes to a real PNG with
# the correct signature). Small enough to keep the fixture readable while proving
# the client returns a decodable `data:image/png;base64,…` payload.
SMALL_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ"
    "/pLvAAAAAElFTkSuQmCC"
)

#: The `qr_image_png` field value on a successful run — a data URI.
EXPECTED_QR_DATA_URI = f"data:image/png;base64,{SMALL_PNG_B64}"

#: The `hosted_url` field value on a successful run → `JobResult.payment_link`.
EXPECTED_HOSTED_URL = "https://payments.stripe.com/upi/instructions/pi_3AbCdEfGhIjK"

#: The vendor's success signal on the final `done` event.
EXPECTED_RUN_CODE = "upi_qr_ready"

#: Progress lines shared by the success/failure streams, as
#: `(stage, percent, label, attempts)`. The first line carries no label/attempts
#: (the vendor omits them for `starting`).
PROGRESS_LINES: tuple[tuple[str, int, str | None, int | None], ...] = (
    ("starting", 5, None, None),
    ("creating", 25, "[w1] init...", 1),
    ("confirming", 50, "[w1] confirm (UPI)...", 1),
    ("fetching", 80, "[w1] polling for UPI QR...", 1),
)


def _progress_obj(stage: str, percent: int, label: str | None, attempts: int | None) -> dict:
    obj: dict = {"stage": stage, "percent": percent}
    if label is not None:
        obj["label"] = label
    if attempts is not None:
        obj["attempts"] = attempts
    return obj


def _progress_ndjson() -> str:
    """The 4 progress lines as NDJSON text, each terminated by a newline."""
    lines = [_progress_obj(*p) for p in PROGRESS_LINES]
    return "".join(json.dumps(obj) + "\n" for obj in lines)


def _success_done_obj() -> dict:
    return {
        "stage": "done",
        "percent": 100,
        "result": {
            "ok": True,
            "code": EXPECTED_RUN_CODE,
            "qr_image_png": EXPECTED_QR_DATA_URI,
            "qr_image_svg": "<svg xmlns='http://www.w3.org/2000/svg'></svg>",
            "hosted_url": EXPECTED_HOSTED_URL,
            "amount": 0,
            "currency": "INR",
            "intent_type": "setup_intent",
            "expires_at": "2026-07-12T15:30:00Z",
            "elapsed_ms": 51234,
            "attempts": 1,
        },
    }


def _failure_done_obj() -> dict:
    return {
        "stage": "done",
        "percent": 100,
        "result": {
            "ok": False,
            "code": "upi_create_failed",
            "error": "vendor could not create the UPI intent",
            "elapsed_ms": 8123,
            "attempts": 3,
        },
    }


def success_ndjson() -> str:
    """Full success stream as NDJSON text (progress + final `upi_qr_ready`)."""
    return _progress_ndjson() + json.dumps(_success_done_obj()) + "\n"


def failure_ndjson() -> str:
    """Full failure stream as NDJSON text (progress + final `result.ok=false`)."""
    return _progress_ndjson() + json.dumps(_failure_done_obj()) + "\n"


def truncated_ndjson() -> str:
    """Progress lines then a final `done` line cut off mid-object.

    The last line has no trailing newline and is not valid JSON, modelling a
    dropped connection right as the vendor started emitting the result — the
    client must surface a stream error rather than a (partial) result.
    """
    done_text = json.dumps(_success_done_obj())
    return _progress_ndjson() + done_text[: len(done_text) // 2]


def byte_chunks(text: str, size: int = 13) -> list[bytes]:
    """Split `text` into fixed-size BYTE chunks.

    A small `size` (default 13) relative to each line guarantees chunk
    boundaries fall INSIDE JSON objects — i.e. a single object is split across
    two chunks — which is exactly what the production line-buffer must survive.
    """
    blob = text.encode("utf-8")
    return [blob[i : i + size] for i in range(0, len(blob), size)]


def success_chunks(size: int = 13) -> list[bytes]:
    """Success stream as byte chunks with objects split across chunk boundaries."""
    return byte_chunks(success_ndjson(), size)


def failure_chunks(size: int = 13) -> list[bytes]:
    """Failure stream (`result.ok=false`) as byte chunks."""
    return byte_chunks(failure_ndjson(), size)


def truncated_chunks(size: int = 13) -> list[bytes]:
    """Truncated stream (final line cut mid-object) as byte chunks."""
    return byte_chunks(truncated_ndjson(), size)


def spans_object_boundary(chunks: list[bytes]) -> bool:
    """True if at least one JSON object is split across two chunks.

    Used by tests to assert the fixture actually exercises the partial-object
    path (a chunk that neither starts a fresh line nor ends on a newline).
    """
    for i, chunk in enumerate(chunks[:-1]):
        # A chunk that does not end on a newline leaves a partial line for the
        # next chunk to complete → object spans the boundary.
        if chunk and not chunk.endswith(b"\n"):
            return True
    return False


def cancellation_chunks_and_delays(
    gap_seconds: float = 10.0,
) -> tuple[list[bytes], list[float]]:
    """A stream that emits ONE complete progress line, then goes silent.

    Returns `(chunks, delays)`: the first chunk is a full `starting` progress
    line (so `on_progress` fires once), then a `gap_seconds` silent hold before
    any further bytes. A cancellation raised during that gap must interrupt the
    read promptly — proving the reader does not merely poll between lines.
    """
    first_line = json.dumps(_progress_obj(*PROGRESS_LINES[0])) + "\n"
    rest = "".join(
        json.dumps(_progress_obj(*p)) + "\n" for p in PROGRESS_LINES[1:]
    ) + json.dumps(_success_done_obj()) + "\n"
    chunks = [first_line.encode("utf-8"), rest.encode("utf-8")]
    delays = [0.0, gap_seconds]
    return chunks, delays
