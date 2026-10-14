"""Bind-probe: refuse a port another process already holds (gate 2)."""

import socket

from panel.xray import ports


def test_port_available_free():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    assert ports.port_available(free_port)


def test_port_available_occupied():
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        assert not ports.port_available(port, "127.0.0.1")
        # a wildcard probe must fail too: 0.0.0.0 overlaps a loopback hold
        assert not ports.port_available(port)
    finally:
        holder.close()
