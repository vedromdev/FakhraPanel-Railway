"""Inbound CRUD behind the five-gate save flow (spec §5).

Gates per mutation: validate settings (400) → reserved port (409) → db
duplicate (409) → bind probe (409) → `manager.apply_inbound_change`, which
re-runs validation against the RUNNING config (`xray -test` failure → 422
`{"detail": …, "saved": false}`, nothing persisted) and reverts the DB if
the swap's restart fails.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from panel import auth, db, inbounds
from panel.xray import ports

router = APIRouter(prefix="/api", dependencies=[Depends(auth.require_admin)])


class InboundCreate(BaseModel):
    protocol: Literal["vless", "vmess", "trojan", "ss"]
    port: int = Field(ge=1, le=65535)
    listen: str = "0.0.0.0"
    remark: str = ""
    enabled: bool = True
    settings: dict = {}


def _validated_row(body: InboundCreate, row_id: str | None = None) -> dict:
    """Settings validation (400) + reserved-port gate (409) → full row dict."""
    try:
        settings = inbounds.validate_settings(body.protocol, body.settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if body.port in inbounds.RESERVED_PORTS:
        raise HTTPException(
            status_code=409,
            detail=f"port {body.port} is reserved for the panel/edge",
        )
    return {
        "id": row_id or f"{body.protocol}-{uuid.uuid4().hex[:8]}",
        "protocol": body.protocol,
        "listen": body.listen,
        "port": body.port,
        "remark": body.remark,
        "settings": json.dumps(settings),
        "enabled": int(body.enabled),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _check_port_free_in_db(port: int, exclude_id: str | None = None) -> None:
    for row in db.list_inbounds():
        if row["port"] == port and row["id"] != exclude_id:
            raise HTTPException(
                status_code=409,
                detail=f"port {port} already used by inbound {row['id']}",
            )


def _apply_failure(msg: str) -> JSONResponse:
    """Map an apply_inbound_change failure to its gate's status code."""
    if "already in use" in msg:
        raise HTTPException(status_code=409, detail=msg)
    return JSONResponse(status_code=422, content={"detail": msg, "saved": False})


@router.get("/inbounds")
def list_inbounds() -> dict:
    return {
        "inbounds": db.list_inbounds(),
        "user_counts": db.count_users_per_inbound(),
    }


@router.post("/inbounds")
def create_inbound(body: InboundCreate, request: Request) -> JSONResponse:
    row = _validated_row(body)
    _check_port_free_in_db(row["port"])
    if not ports.port_available(row["port"], row["listen"]):
        raise HTTPException(status_code=409, detail=f"port {row['port']} already in use")

    proposed = db.list_inbounds() + [row]
    ok, msg = request.app.state.manager.apply_inbound_change(
        db.list_users(),
        proposed,
        commit=lambda: db.create_inbound(row),
        revert=lambda: db.delete_inbound(row["id"]),
    )
    if not ok:
        return _apply_failure(msg)
    return JSONResponse(
        status_code=201,
        content={
            "inbound": db.get_inbound(row["id"]),
            "firewall_hint": inbounds.firewall_hint(row["port"]),
        },
    )


@router.put("/inbounds/{inbound_id}")
def update_inbound(inbound_id: str, body: InboundCreate, request: Request) -> JSONResponse:
    prior = db.get_inbound(inbound_id)
    if prior is None:
        raise HTTPException(status_code=404, detail=f"inbound {inbound_id!r} not found")
    # full replace of the mutable fields; id and created_at are identity
    row = _validated_row(body, row_id=prior["id"])
    row["created_at"] = prior["created_at"]
    fields = {c: row[c] for c in ("protocol", "listen", "port", "remark", "settings", "enabled")}
    prior_fields = {c: prior[c] for c in fields}
    _check_port_free_in_db(row["port"], exclude_id=inbound_id)
    if row["port"] != prior["port"] and not ports.port_available(row["port"], row["listen"]):
        raise HTTPException(status_code=409, detail=f"port {row['port']} already in use")

    proposed = [row if r["id"] == inbound_id else r for r in db.list_inbounds()]
    ok, msg = request.app.state.manager.apply_inbound_change(
        db.list_users(),
        proposed,
        commit=lambda: db.update_inbound(inbound_id, fields),
        revert=lambda: db.update_inbound(inbound_id, prior_fields),
    )
    if not ok:
        return _apply_failure(msg)
    return JSONResponse(content={"inbound": db.get_inbound(inbound_id)})


@router.delete("/inbounds/{inbound_id}")
def delete_inbound(inbound_id: str, request: Request) -> dict:
    prior = db.get_inbound(inbound_id)
    if prior is None:
        raise HTTPException(status_code=404, detail=f"inbound {inbound_id!r} not found")
    assigned = db.count_users_per_inbound().get(inbound_id, 0)
    if assigned:
        raise HTTPException(
            status_code=409, detail=f"{assigned} users still assigned"
        )

    proposed = [r for r in db.list_inbounds() if r["id"] != inbound_id]
    ok, msg = request.app.state.manager.apply_inbound_change(
        db.list_users(),
        proposed,
        commit=lambda: db.delete_inbound(inbound_id),
        revert=lambda: db.create_inbound(prior),
    )
    if not ok:
        return _apply_failure(msg)
    return {"deleted": True}
