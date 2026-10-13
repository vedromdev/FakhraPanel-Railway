"""Client for Xray's gRPC StatsService (per-user / per-inbound traffic counters).

The stubs in ``panel.xray.pb`` are generated from the vendored Xray proto by
``scripts/gen_protos.sh``; the service is the StatsService exposed by the
dokodemo-door "api" inbound on 127.0.0.1:10010 (see config_builder).
"""

import grpc

from panel.xray.pb import command_pb2, command_pb2_grpc

DEFAULT_ADDR = "127.0.0.1:10010"
RPC_TIMEOUT_S = 3.0  # every RPC is bounded: a wedged xray must never hang us


class StatsError(Exception):
    """Raised when the Xray StatsService cannot be reached or errors."""


def stat_name(email: str, direction: str) -> str:
    """Xray's per-user traffic counter name.

    Literal locked by a real-binary probe (Xray 26.3.27 debug log prints
    "create new counter user>>>u-1>>>traffic>>>uplink" on first traffic;
    the bracketed variant user>>>[u-1]>>>... is NOT_FOUND).
    """
    return f"user>>>{email}>>>traffic>>>{direction}"


def compute_delta(raw: int, last_raw: int, used_bytes: int) -> tuple[int, int]:
    """Counter-reset-safe accumulation: returns (new_used_bytes, new_last_raw).

    Xray's counters are cumulative but reset to ~0 when it restarts, so a raw
    value below the last observation means a restart — everything seen so far
    counts as new traffic. First observation (last_raw == 0) is the same math.
    """
    if raw >= last_raw:
        return used_bytes + (raw - last_raw), raw
    return used_bytes + raw, raw


class StatsClient:
    """Blocking gRPC client for Xray's StatsService.

    ``channel_factory`` is injectable so tests can substitute a fake
    transport; it defaults to ``grpc.insecure_channel`` (the api inbound is
    plain-text on localhost only).
    """

    def __init__(self, addr: str = DEFAULT_ADDR, channel_factory=None):
        self.addr = addr
        self._channel_factory = channel_factory or grpc.insecure_channel
        self._channel = None

    def _stub(self) -> command_pb2_grpc.StatsServiceStub:
        if self._channel is None:
            try:
                self._channel = self._channel_factory(self.addr)
            except Exception as exc:
                raise StatsError(
                    f"cannot open xray stats channel to {self.addr}: {exc}"
                ) from exc
        return command_pb2_grpc.StatsServiceStub(self._channel)

    def _get_counter(self, name: str) -> int | None:
        """Raw counter value, or None when the counter isn't registered.

        A NOT_FOUND status means Xray never created this counter (no traffic
        has flowed for it yet) — not an error. Everything else (connection
        refused, deadline, ...) is a transport failure and raises StatsError.
        """
        request = command_pb2.GetStatsRequest(name=name, reset=False)
        try:
            response = self._stub().GetStats(request, timeout=RPC_TIMEOUT_S)
        except grpc.RpcError as exc:
            code = getattr(exc, "code", None)
            if code is not None and code() == grpc.StatusCode.NOT_FOUND:
                return None
            raise StatsError(f"stats query {name!r} failed: {exc}") from exc
        return response.stat.value

    def get_bytes(self, user_id: str) -> int:
        """Current raw counter value (bytes): user uplink + downlink.

        The email-keyed counter is protocol-independent; a missing direction
        (counter never registered) counts as 0.
        """
        uplink = self._get_counter(stat_name(user_id, "uplink")) or 0
        downlink = self._get_counter(stat_name(user_id, "downlink")) or 0
        return uplink + downlink

    def get_inbound_bytes(self, protocol: str) -> int:
        """Current raw counter value (bytes) of one inbound's uplink."""
        return self._get_counter(
            f"inbound>>>in-{protocol}>>>traffic>>>uplink"
        ) or 0
