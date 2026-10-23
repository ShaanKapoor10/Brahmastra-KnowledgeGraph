"""
What an error may say outside the server.

Brahmastra shows failures on purpose: a note's extraction error, a Notion page
that would not sync, the model call that failed while naming speakers. A 413
was once misdiagnosed for two days because nothing recorded the message, so
hiding errors behind "something went wrong" is not the answer.

But the text of an exception is written by whatever raised it (an LLM SDK, the
Notion client, psycopg, the filesystem), and it reaches every signed-in viewer
through /ask, /diagnostics, /pipeline and the meeting routes. CodeQL flagged
seven such paths (py/stack-trace-exposure, 2026-10-23). `public_error` keeps
the part that diagnoses (the exception's type and message) and removes what
must not travel: credentials in any of their common shapes, and where the
server's files live. It never returns a traceback.
"""

from __future__ import annotations

import re

LIMIT = 300

# scheme://user:password@host -> scheme://user:***@host. Splits on the LAST
# "@" before the host, so a password containing "@" is covered whole.
_URL_CREDENTIALS = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s/:@]*:)[^\s/]*@", re.I)
# password=... in a key-value connection string or a query string.
_PASSWORD_PARAM = re.compile(r"(?i)\b((?:password|passwd|pwd)\s*=\s*)('[^']*'|[^\s&;,]+)")
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
# Absolute file paths say where the server is installed and who runs it. The
# file name is kept: it is usually what makes the error useful.
_WINDOWS_PATH = re.compile(r"\b[A-Za-z]:\\(?:[^\s\\'\"<>|]+\\)+")
_POSIX_PATH = re.compile(r"(?<![\w/:])/(?:home|Users|root|usr|opt|app|srv|var|tmp|etc|mnt)/(?:[^\s/'\"<>|]+/)+")


def scrub(text: str) -> str:
    """Remove credentials and server paths from any text bound for a client."""
    from brahmastra.sessions import redact

    text = _URL_CREDENTIALS.sub(r"\1***@", text)
    text = _PASSWORD_PARAM.sub(r"\1***", text)
    text = _BEARER.sub(r"\1***", text)
    text = _WINDOWS_PATH.sub("", text)
    text = _POSIX_PATH.sub("", text)
    return redact(text)


def public_error(exc: BaseException, limit: int = LIMIT) -> str:
    """`Type: message`, scrubbed and bounded: what a client is told about a failure."""
    message = str(exc)
    text = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    return scrub(text)[:limit]
