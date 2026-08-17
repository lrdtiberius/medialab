from __future__ import annotations

import base64
import secrets

from fastapi import Request
from starlette.responses import PlainTextResponse

from .config import Settings


class BasicAuthMiddleware:
    def __init__(self, app, settings: Settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.settings.web_username:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/health":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        header = request.headers.get("authorization", "")
        valid = False
        if header.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
                username, password = decoded.split(":", 1)
                valid = secrets.compare_digest(username, self.settings.web_username) and secrets.compare_digest(
                    password, self.settings.web_password
                )
            except (ValueError, UnicodeDecodeError, base64.binascii.Error):
                valid = False
        if valid:
            await self.app(scope, receive, send)
            return
        response = PlainTextResponse(
            "Anmeldung erforderlich",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="MediaLab", charset="UTF-8"'},
        )
        await response(scope, receive, send)
