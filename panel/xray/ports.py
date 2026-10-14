"""Bind-probe: can this panel claim (listen, port) right now?

The gate that rejects a save BEFORE anything is persisted — a port someone
else holds fails a bind+close probe with no SO_REUSEADDR, so a config that
could never start never reaches xray, the DB, or a link.
"""

import socket


def port_available(port: int, listen: str = "0.0.0.0") -> bool:
    """True iff a TCP bind+close probe succeeds right now.

    No SO_REUSEADDR: an actively-held port must fail, including when the
    holder listens on a narrower address than the wildcard probe.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((listen, port))
        except OSError:
            return False
    return True
