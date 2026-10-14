"""Public subscription endpoint: GET /sub/{token} -> base64(share link).

No session auth — the per-user token IS the credential (generated at user
creation, stable for the account's life). Rate-limited per client, never
cached, 404 for unknown tokens.
"""

import base64

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from panel import db, links
from panel.ratelimit import rate_limit_key

router = APIRouter()


@router.get("/sub/{token}")
def subscription(token: str, request: Request) -> Response:
    if not request.app.state.sub_rate.allow(f"sub:{rate_limit_key(request)}"):
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    user = db.get_user_by_sub_token(token)
    if user is None:
        raise HTTPException(status_code=404, detail="unknown subscription token")
    inbound = db.get_inbound(user.get("inbound_id"))
    if inbound is None:
        raise HTTPException(status_code=409, detail="user has no inbound")
    public_host = request.app.state.settings.PUBLIC_HOST
    link = links.build_link(user, inbound, public_host)
    encoded = base64.b64encode(link.encode()).decode("ascii")
    return Response(
        content=encoded,
        media_type="text/plain",
        headers={"Cache-Control": "no-store"},
    )
