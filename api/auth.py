"""Token auth for the localhost-bound API (SPEC.md §12)."""
from __future__ import annotations

import hmac

from fastapi import HTTPException, Request


def make_token_dependency(token: str):
    if not token:
        raise ValueError("API token must be non-empty")

    def require_token(request: Request) -> None:
        header = request.headers.get("Authorization", "")
        provided = header[7:].strip() if header.startswith("Bearer ") else (
            request.headers.get("X-API-Token", "")
        )
        if not provided or not hmac.compare_digest(provided, token):
            raise HTTPException(status_code=401, detail="invalid or missing token")

    return require_token
