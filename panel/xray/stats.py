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


def stat_name(user_id: str, protocol: str) -> str:
    """Xray's per-user traffic counter name for one inbound."""
    return f"user<<{user_id}>>@in-{protocol}"


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

    def _get_stat(self, name: str) -> int:
        request = command_pb2.GetStatsRequest(name=name, reset=False)
        try:
            response = self._stub().GetStats(request, timeout=RPC_TIMEOUT_S)
        except grpc.RpcError as exc:
            raise StatsError(f"stats query {name!r} failed: {exc}") from exc
        return response.stat.value

    def get_bytes(self, user_id: str, protocol: str) -> int:
        """Current raw counter value (bytes) for one user on one inbound."""
        return self._get_stat(stat_name(user_id, protocol))

    def get_inbound_bytes(self, protocol: str) -> int:
        """Current raw counter value (bytes) of one inbound's uplink."""
        return self._get_stat(f"inbound>>>in-{protocol}>>>traffic>>>uplink")
