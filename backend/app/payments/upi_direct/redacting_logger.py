"""Logger adapter that redacts known secrets and proxy userinfo.

`logging.LoggerAdapter.process()` only sees `msg`/`kwargs` — the printf-style
positional `*args` are handed straight to the underlying logger AFTER
`process()` runs. Redacting inside `process()` alone therefore leaks any
secret passed as a positional argument (e.g. `logger.info("token=%s", tok)`).
This adapter overrides `log()` so it formats the message with its args FIRST,
then value-redacts known secrets and strips proxy userinfo on the fully
rendered string before it can reach the job log buffer / SSE stream.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.proxy_format import sanitize_proxy_text
from app.core.redaction import redact_message


class RedactingLogger(logging.LoggerAdapter):
    def _redact(self, text: str) -> str:
        secrets: list[str] = self.extra.get("secrets") or []  # type: ignore[assignment,union-attr]
        return sanitize_proxy_text(redact_message(text, secrets))

    def log(self, level: int, msg: Any, *args: Any, **kwargs: Any) -> None:
        if not self.isEnabledFor(level):
            return
        text = str(msg)
        if args:
            try:
                text = text % args
            except Exception:
                # Malformed format string — never drop the args unredacted;
                # append their reprs so redaction still covers them.
                text = text + " " + " ".join(repr(a) for a in args)
        # Pass no positional args downstream — the message is already rendered
        # and redacted, so the underlying logger must not re-expand anything.
        self.logger.log(level, self._redact(text), **kwargs)

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        # Retained for any direct `.process()` callers; the `log()` override is
        # the real redaction path.
        return self._redact(str(msg)), kwargs


__all__ = ["RedactingLogger"]
