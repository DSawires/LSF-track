"""How much request body the app will read.

FastAPI reads and parses a request's body *before* it runs the endpoint's
dependencies -- which is where the session is checked. So without a limit,
anyone who can reach the server can make it read as much as they care to send:
a JSON body is held in memory, a multipart upload is spooled to disk, and only
then does the 401 go out. On a small box on the public internet that is a way
to stop the app for everyone.

This sits in front of all of that and counts. The allowance depends on who is
asking, and telling that apart costs no database call: the session cookie is
signed, so a valid signature is checked on its own.

- No valid session: `ANONYMOUS_BYTES`. The only thing a signed-out client
  sends is a login, or a phone with an expired session trying to drain its
  queue -- which must get its 401, not a 413, so the allowance is well above
  the largest batch (500 entries).
- A session, sending a file (multipart): the larger of the import limit and
  the photo limit, plus room for the form around it. The endpoints still
  apply their own, smaller limits with their own messages.
- A session, anything else: `JSON_BYTES`, several times the largest thing
  the app itself sends (a 500-line work order in two languages).

Over the allowance the answer is 413, and reading stops there.
"""

from __future__ import annotations

from app.config import get_settings

ANONYMOUS_BYTES = 1024 * 1024
JSON_BYTES = 16 * 1024 * 1024
_FORM_OVERHEAD = 1024 * 1024
_BODILESS = {"GET", "HEAD", "OPTIONS"}


def _has_session(headers: dict[bytes, bytes]) -> bool:
    """Is there a session cookie this server signed, and not yet expired?
    Whether its user still exists is the endpoint's question, not this one's."""
    # Function-local: app.deps imports the database layer, which this module
    # has no other reason to load.
    from http.cookies import SimpleCookie

    from app.deps import COOKIE_NAME, _serializer

    raw = headers.get(b"cookie")
    if not raw:
        return False
    try:
        cookie = SimpleCookie()
        cookie.load(raw.decode("latin-1"))
        morsel = cookie.get(COOKIE_NAME)
        if morsel is None:
            return False
        _serializer().loads(morsel.value, max_age=get_settings().session_max_age_seconds)
    except Exception:  # a bad signature, an expired one, an unreadable header:
        return False   # all of them are simply "no session"
    return True


def allowance(headers: dict[bytes, bytes]) -> int:
    if not _has_session(headers):
        return ANONYMOUS_BYTES
    if headers.get(b"content-type", b"").lower().startswith(b"multipart/"):
        from app.services.imports import MAX_BYTES as import_bytes

        return max(import_bytes, get_settings().max_upload_bytes) + _FORM_OVERHEAD
    return JSON_BYTES


class BodyLimitMiddleware:
    """Plain ASGI, so it sees the body as it arrives rather than after it has
    been collected."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["method"] in _BODILESS:
            await self.app(scope, receive, send)
            return

        headers = dict(scope["headers"])
        limit = allowance(headers)

        # The honest case: the client says how much is coming. Refused before
        # a byte of it is read.
        declared = headers.get(b"content-length", b"")
        if declared.isdigit() and int(declared) > limit:
            await _too_large(send, limit)
            return

        # No length given (or a false one): count what actually arrives.
        seen = 0
        over = False
        started = False

        async def counted_receive():
            nonlocal seen, over
            if over:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    # Stop feeding the app. It sees the client go away, and
                    # whatever it makes of that is replaced below.
                    over = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message):
            nonlocal started
            if over and not started:
                return  # the app's own answer to a body we cut off is not the answer
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted_receive, guarded_send)
        except Exception:
            if not over:
                raise
        if over and not started:
            await _too_large(send, limit)


async def _too_large(send, limit: int) -> None:
    body = (
        b'{"detail":"request body too large; the limit here is %d MB"}'
        % max(1, limit // (1024 * 1024))
    )
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
