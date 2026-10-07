"""HTTP JSON fetching, with credentials kept out of every error message.

Two error types, because the relay treats them alike but a reader should not:
FetchError means the source could not be read at all; ShapeError means it
answered with something its collector does not understand. Both count against
the collector and both keep its high-water mark where it was.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable

Fetch = Callable[[str, dict[str, str]], object]

SNIPPET_CHARS = 2048
_KEY_PARAM = re.compile(r"(?i)\b(apikey|api_key|token)=[^&]*")


class FetchError(Exception):
    """A source could not be read: unreachable, non-2xx, or not JSON."""


class ShapeError(Exception):
    """A source answered, but not in the shape its collector parses."""


def redact(url: str) -> str:
    return _KEY_PARAM.sub(lambda match: f"{match.group(1)}=REDACTED", url)


def snippet(value: object) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text[:SNIPPET_CHARS]


def get_json(url: str, headers: dict[str, str], timeout: float = 15.0) -> object:
    request = urllib.request.Request(url, headers={"Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code} from {redact(url)}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"{type(exc).__name__} reaching {redact(url)}") from None
    try:
        return json.loads(body)
    except ValueError:
        text = body.decode("utf-8", "replace")
        raise FetchError(f"non-JSON reply from {redact(url)}: {snippet(text)}") from None
