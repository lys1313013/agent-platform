"""Public endpoints — serve chat attachment images behind ownership checks."""

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import Response

from aio_agent_platform.auth.jwt_handler import TokenError, decode_token
from aio_agent_platform.storage.client import ObjectStorage

router = APIRouter(prefix="/api/public", tags=["public"])


def _extract_owner_id(key: str) -> str | None:
    """Return the user id embedded in a chat-attachment object key, if any."""
    parts = key.split("/")
    if len(parts) >= 2 and parts[0] == "chat-attachments":
        return parts[1]
    return None


@router.get("/images/{key:path}")
async def get_public_image(
    key: str,
    token: str | None = Query(default=None),
    signed: str | None = Query(default=None, alias="t"),
    authorization: Annotated[str | None, Header()] = None,
):
    """Serve a chat attachment image behind a scoped, short-lived token.

    ``?t=`` is an HMAC token bound to this object key, so it is safe to appear
    in URLs and logs. A Bearer access token is still accepted for API clients.
    """
    if not key.startswith("chat-attachments/"):
        raise HTTPException(status_code=404, detail="Not found")

    from aio_agent_platform.core.signed_urls import verify_resource_token

    authorized = False
    scoped = signed or token
    if scoped and verify_resource_token(scoped, key):
        authorized = True

    if not authorized:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="缺少访问凭证")
        try:
            payload = decode_token(authorization[len("Bearer "):])
        except TokenError:
            raise HTTPException(status_code=401, detail="凭证无效或已过期")
        if payload.type != "access":
            raise HTTPException(status_code=401, detail="无效的凭证类型")
        owner_id = _extract_owner_id(key)
        if not owner_id or payload.sub != owner_id:
            raise HTTPException(status_code=403, detail="无权访问该附件")

    try:
        storage = ObjectStorage()
        data = storage.get(key)
        stat = storage.stat(key)
        content_type = stat.content_type if stat and stat.content_type else "image/png"

        return Response(
            content=data,
            media_type=content_type,
            headers={
                # Short-lived + private: the URL carries an expiring token.
                "Cache-Control": "private, max-age=300",
            },
        )
    except Exception as e:
        raise HTTPException(status_code=404, detail="Not found") from e
