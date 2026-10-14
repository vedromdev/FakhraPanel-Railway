"""Admin JSON API: user CRUD, bulk create, toggle, regenerate, reset, stats, health.

The DB is the source of truth: every mutation persists first, then hands the
full user list to `manager.apply_config`. A rejected swap cannot be undone
cleanly, so the route answers `500 {"detail": "config rejected: <msg>",
"saved": true}` — the change is kept and xray catches up on the next
successful apply (same divergence-logging rule as panel/jobs.py).
"""

import logging
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from panel import auth, db, inbounds, links
from panel.constants import DEFAULT_SS_METHOD

logger = logging.getLogger(__name__)

USERNAME_RE = r"^[a-zA-Z0-9_.-]{1,32}$"
# Characters that break trojan:// or ss:// URI parsing (or invite trouble in
# a URL): generated credentials must never contain them, and an admin-provided
# password is rejected (422) if it does.
UNSAFE_PASSWORD_CHARS = "@:#%/&?+"

router = APIRouter(prefix="/api", dependencies=[Depends(auth.require_admin)])
health_router = APIRouter(prefix="/api")  # /api/health is public (Railway)


# --- request bodies ----------------------------------------------------------


def _validate_password(password: str) -> None:
    """Reject empty, bcrypt-unsafe (>72 bytes), or share-URI-unsafe passwords."""
    if not password:
        raise ValueError("password must not be empty")
    if len(password.encode("utf-8")) > 72:
        raise ValueError("password must be at most 72 bytes")
    if any(char in UNSAFE_PASSWORD_CHARS or char.isspace() for char in password):
        raise ValueError(
            "password must not contain '@ : # % / & ? +' or whitespace"
        )


class UserCreate(BaseModel):
    username: str = Field(pattern=USERNAME_RE)
    protocol: Literal["vless", "vmess", "trojan", "ss"]
    inbound_id: str | None = None  # None -> the protocol's default inbound
    expiry_days: int | None = Field(default=None, ge=1, le=3650)
    quota_gb: float | None = Field(default=None, ge=0.001, le=100000)
    note: str = ""
    password: str | None = None  # optional admin-provided; else server generates

    @field_validator("password")
    @classmethod
    def _password_is_safe(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_password(value)
        return value


class UserBulk(BaseModel):
    prefix: str = Field(pattern=USERNAME_RE)
    count: int = Field(ge=1, le=100)
    protocol: Literal["vless", "vmess", "trojan", "ss"]
    inbound_id: str | None = None  # resolved once, applied to every row
    expiry_days: int | None = Field(default=None, ge=1, le=3650)
    quota_gb: float | None = Field(default=None, ge=0.001, le=100000)
    note: str = ""

    @model_validator(mode="after")
    def _generated_usernames_fit_the_regex(self) -> "UserBulk":
        # f"{prefix}-{i:03d}" appends 4 characters to the prefix.
        if len(self.prefix) + 4 > 32:
            raise ValueError("prefix too long: generated username exceeds 32 chars")
        return self


class UserPatch(BaseModel):
    expires_at: str | None = None
    quota_gb: float | None = Field(default=None, ge=0.001, le=100000)
    note: str | None = None

    @field_validator("expires_at")
    @classmethod
    def _expires_at_is_iso(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                datetime.fromisoformat(value)
            except ValueError:
                raise ValueError("expires_at must be an ISO 8601 datetime")
        return value


class UserToggle(BaseModel):
    enabled: bool


# --- helpers -----------------------------------------------------------------


def _expiry_iso(expiry_days: int | None) -> str | None:
    if expiry_days is None:
        return None
    return (datetime.now(timezone.utc) + timedelta(days=expiry_days)).isoformat()


def _new_row(
    username: str,
    protocol: str,
    plain_password: str,
    expires_at: str | None,
    quota_gb: float | None,
    note: str,
    inbound_id: str,
) -> dict:
    """Build one user row; protocol fields are NEVER null for a created user."""
    return {
        "id": str(uuid.uuid4()),
        "username": username,
        "password": auth.hash_password(plain_password),
        "protocol": protocol,
        "uuid": str(uuid.uuid4()) if protocol in ("vless", "vmess") else None,
        "credential": secrets.token_urlsafe(24)
        if protocol in ("trojan", "ss")
        else None,
        "ss_method": DEFAULT_SS_METHOD if protocol == "ss" else None,
        "enabled": 1,
        "disable_reason": None,
        "expires_at": expires_at,
        "quota_gb": quota_gb,
        "note": note,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "inbound_id": inbound_id,
    }


def _resolve_inbound(protocol: str, requested: str | None) -> str:
    """Default/explicit inbound pick; ValueError becomes a 400."""
    try:
        return inbounds.resolve_inbound_id(
            protocol, requested, db.list_inbounds()
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _get_or_404(user_id: str) -> dict:
    user = db.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return user


def _inbound_or_404(user: dict) -> dict:
    """The inbound this user's link derives from; 409 when dangling."""
    inbound = db.get_inbound(user.get("inbound_id"))
    if inbound is None:
        raise HTTPException(status_code=409, detail="user has no inbound")
    return inbound


def _link_and_qr(user: dict, public_host: str) -> tuple[str, str]:
    link = links.build_link(user, _inbound_or_404(user), public_host)
    return link, links.build_qr_data_uri(link)


def _apply(request: Request) -> JSONResponse | None:
    """Re-apply the xray config; on rejection return the 500-with-saved body."""
    ok, msg = request.app.state.manager.apply_config(
        db.list_users(), db.list_inbounds()
    )
    if not ok:
        logger.error("apply_config failed after API mutation: %s", msg)
        return JSONResponse(
            status_code=500,
            content={"detail": f"config rejected: {msg}", "saved": True},
        )
    return None


def _conflict_response(conflicts: list[str]) -> JSONResponse:
    return JSONResponse(status_code=409, content={"conflicts": conflicts})


# --- routes ------------------------------------------------------------------


@router.get("/users")
def list_users() -> dict:
    return {"users": db.list_users()}


@router.post("/users", status_code=201)
def create_user(body: UserCreate, request: Request):
    if any(user["username"] == body.username for user in db.list_users()):
        raise HTTPException(status_code=409, detail="username already exists")

    plain_password = body.password or secrets.token_urlsafe(16)
    row = _new_row(
        body.username,
        body.protocol,
        plain_password,
        _expiry_iso(body.expiry_days),
        body.quota_gb,
        body.note,
        _resolve_inbound(body.protocol, body.inbound_id),
    )
    try:
        user = db.create_user(row)
    except sqlite3.IntegrityError:  # lost the race against a concurrent create
        raise HTTPException(status_code=409, detail="username already exists")

    rejected = _apply(request)
    if rejected is not None:
        return rejected

    link, qr = _link_and_qr(user, request.app.state.settings.PUBLIC_HOST)
    return {"user": user, "link": link, "qr": qr, "password": plain_password}


@router.post("/users/bulk", status_code=201)
def bulk_create_users(body: UserBulk, request: Request):
    usernames = [f"{body.prefix}-{index:03d}" for index in range(1, body.count + 1)]
    existing = {user["username"] for user in db.list_users()}
    conflicts = [name for name in usernames if name in existing]
    if conflicts:
        return _conflict_response(conflicts)

    expires_at = _expiry_iso(body.expiry_days)
    inbound_id = _resolve_inbound(body.protocol, body.inbound_id)
    rows = [
        _new_row(name, body.protocol, secrets.token_urlsafe(16), expires_at,
                 body.quota_gb, body.note, inbound_id)
        for name in usernames
    ]
    try:
        users = db.create_users(rows)  # one transaction: any conflict rolls back
    except sqlite3.IntegrityError:
        existing = {user["username"] for user in db.list_users()}
        return _conflict_response(
            [name for name in usernames if name in existing]
        )

    rejected = _apply(request)
    if rejected is not None:
        return rejected

    public_host = request.app.state.settings.PUBLIC_HOST
    built = [
        links.build_link(user, _inbound_or_404(user), public_host)
        for user in users
    ]
    return {"users": users, "links": built}


@router.get("/users/{user_id}")
def get_user(user_id: str, request: Request) -> dict:
    user = _get_or_404(user_id)
    link, qr = _link_and_qr(user, request.app.state.settings.PUBLIC_HOST)
    return {"user": user, "link": link, "qr": qr}


@router.patch("/users/{user_id}")
def patch_user(user_id: str, body: UserPatch, request: Request):
    _get_or_404(user_id)
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=422, detail="no updatable fields provided")
    updated = db.update_user(user_id, fields)
    if updated is None:
        raise HTTPException(status_code=404, detail="user not found")

    rejected = _apply(request)
    if rejected is not None:
        return rejected
    return {"user": updated}


@router.post("/users/{user_id}/toggle")
def toggle_user(user_id: str, body: UserToggle, request: Request):
    _get_or_404(user_id)
    fields = (
        {"enabled": 1, "disable_reason": None}
        if body.enabled
        else {"enabled": 0, "disable_reason": "manual"}
    )
    updated = db.update_user(user_id, fields)
    if updated is None:
        raise HTTPException(status_code=404, detail="user not found")

    rejected = _apply(request)
    if rejected is not None:
        return rejected
    return {"user": updated}


@router.delete("/users/{user_id}")
def delete_user(user_id: str, request: Request) -> dict:
    if not db.delete_user(user_id):
        raise HTTPException(status_code=404, detail="user not found")

    rejected = _apply(request)
    if rejected is not None:
        return rejected
    return {"success": True}


@router.post("/users/{user_id}/regenerate")
def regenerate_user(user_id: str, request: Request):
    user = _get_or_404(user_id)
    fields = (
        {"uuid": str(uuid.uuid4())}
        if user["protocol"] in ("vless", "vmess")
        else {"credential": secrets.token_urlsafe(24)}
    )
    updated = db.update_user(user_id, fields)
    if updated is None:
        raise HTTPException(status_code=404, detail="user not found")

    rejected = _apply(request)
    if rejected is not None:
        return rejected

    link, qr = _link_and_qr(updated, request.app.state.settings.PUBLIC_HOST)
    return {"user": updated, "link": link, "qr": qr}


@router.post("/users/{user_id}/reset-password")
def reset_user_password(user_id: str, request: Request) -> dict:
    _get_or_404(user_id)
    plain_password = secrets.token_urlsafe(16)
    updated = db.update_user(
        user_id, {"password": auth.hash_password(plain_password)}
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="user not found")

    rejected = _apply(request)
    if rejected is not None:
        return rejected
    return {"password": plain_password}


@router.get("/stats")
def get_stats() -> dict:
    users = db.list_users()
    return {
        "users_total": len(users),
        "users_enabled": sum(1 for user in users if user.get("enabled")),
        "used_bytes_total": sum(user.get("used_bytes") or 0 for user in users),
        "quota_bytes_total": sum(
            user["quota_gb"] * 1e9
            for user in users
            if user.get("quota_gb") is not None
        ),
    }


@health_router.get("/health")
def get_health(request: Request) -> dict:
    manager = request.app.state.manager
    running = manager.is_running()
    return {
        "status": "ok" if running else "degraded",
        "xray": {
            "running": running,
            "last_error": manager.last_error,
            "uptime_s": manager.uptime_s(),
        },
    }
