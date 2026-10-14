"""Inbound domain rules shared by the admin routes.

Pure functions over plain row dicts — no DB, no settings. The validation
module (validate_settings, reserved ports, firewall hints) grows here with
the CRUD API in Task 5.
"""


def resolve_inbound_id(
    protocol: str, requested: str | None, inbounds: list[dict]
) -> str:
    """Pick the inbound a new user of `protocol` attaches to.

    `requested` must name an ENABLED inbound of the same protocol; otherwise
    the oldest enabled inbound of that protocol is chosen. ValueError carries
    the route's 400 message verbatim.
    """
    matching = [
        row
        for row in inbounds
        if row["protocol"] == protocol and row.get("enabled", 1)
    ]
    if requested is not None:
        if any(row["id"] == requested for row in matching):
            return requested
        raise ValueError(
            f"inbound {requested!r} is not an enabled {protocol} inbound"
        )
    if not matching:
        raise ValueError(f"no enabled {protocol} inbound — create one first")
    chosen = min(matching, key=lambda row: (row["created_at"], row["id"]))
    return chosen["id"]
