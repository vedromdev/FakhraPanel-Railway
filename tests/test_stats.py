"""Tests for the Xray StatsService client and counter-reset-safe delta math."""

from concurrent import futures

import grpc
import pytest

# Guard BEFORE importing panel.xray.stats (stats.py imports the pb stubs):
# a fresh checkout without generated stubs must skip with an actionable
# reason, not die with a raw ImportError.
command_pb2 = pytest.importorskip(
    "panel.xray.pb.command_pb2",
    reason="generated stubs missing — run: bash scripts/gen_protos.sh",
)
command_pb2_grpc = pytest.importorskip(
    "panel.xray.pb.command_pb2_grpc",
    reason="generated stubs missing — run: bash scripts/gen_protos.sh",
)

from panel.xray import stats  # noqa: E402


def test_stat_name_format():
    # Literal locked by real-binary probe (Xray 26.3.27 debug log):
    # "app/stats: create new counter user>>>u-1>>>traffic>>>uplink"
    assert stats.stat_name("u-1", "uplink") == "user>>>u-1>>>traffic>>>uplink"
    assert stats.stat_name("u-1", "downlink") == "user>>>u-1>>>traffic>>>downlink"


def test_compute_delta_normal():
    assert stats.compute_delta(1000, 400, 5000) == (5600, 1000)


def test_compute_delta_reset():
    # Counter reset (xray restarted): the raw value went backwards, so the
    # whole current reading counts as new traffic.
    assert stats.compute_delta(300, 5_000_000, 10_000) == (10_300, 300)


def test_compute_delta_first_seen():
    assert stats.compute_delta(0, 0, 0) == (0, 0)


class _RpcError(grpc.RpcError):
    """Mimics grpc's RpcError with a status code (real ones carry .code())."""

    def __init__(self, code):
        self._code = code

    def code(self):
        return self._code

    def details(self):
        return "synthetic rpc error"

    def __str__(self):
        return f"synthetic rpc error: {self._code.name}"


class _FakeChannel:
    """Transport double implementing the grpc channel unary_unary seam.

    ``values`` maps stat name -> value; a name missing from ``values`` raises
    NOT_FOUND (what real xray does for unregistered counters). ``error``, when
    set, is raised for every call (transport-level failure). Requests are
    recorded as parsed messages so tests assert on the exact wire payload.
    """

    def __init__(self, values=None, error=None):
        self.values = values or {}
        self.error = error
        self.requests = []
        self.timeouts = []

    def unary_unary(self, path, request_serializer, response_deserializer,
                    **kwargs):
        def call(request, timeout=None, metadata=None, **kwargs):
            self.requests.append(
                command_pb2.GetStatsRequest.FromString(request_serializer(request))
            )
            self.timeouts.append(timeout)
            if self.error is not None:
                raise self.error
            if request.name not in self.values:
                raise _RpcError(grpc.StatusCode.NOT_FOUND)
            return response_deserializer(
                command_pb2.GetStatsResponse(
                    stat=command_pb2.Stat(
                        name=request.name, value=self.values[request.name]
                    )
                ).SerializeToString()
            )

        return call


def test_get_bytes_sums_both_directions():
    channel = _FakeChannel(
        values={
            stats.stat_name("u-1", "uplink"): 1000,
            stats.stat_name("u-1", "downlink"): 234,
        }
    )
    client = stats.StatsClient(channel_factory=lambda addr: channel)

    assert client.get_bytes("u-1") == 1234

    assert [r.name for r in channel.requests] == [
        stats.stat_name("u-1", "uplink"),
        stats.stat_name("u-1", "downlink"),
    ]
    assert all(r.reset is False for r in channel.requests)
    assert all(t == stats.RPC_TIMEOUT_S for t in channel.timeouts)  # bounded


def test_get_bytes_not_found_returns_0():
    # No counters registered at all -> 0, not an error.
    empty = _FakeChannel(values={})
    assert stats.StatsClient(channel_factory=lambda addr: empty).get_bytes("u-1") == 0

    # Only one direction registered -> the missing side counts 0.
    half = _FakeChannel(values={stats.stat_name("u-1", "uplink"): 500})
    assert stats.StatsClient(channel_factory=lambda addr: half).get_bytes("u-1") == 500


def test_get_bytes_transport_failure_raises_stats_error():
    # Transport-level failure (NOT not-found) must surface as StatsError.
    channel = _FakeChannel(error=_RpcError(grpc.StatusCode.UNAVAILABLE))
    client = stats.StatsClient(channel_factory=lambda addr: channel)

    with pytest.raises(stats.StatsError):
        client.get_bytes("u-1")


def test_get_inbound_bytes_uses_inbound_stat_name():
    channel = _FakeChannel(
        values={"inbound>>>in-vless>>>traffic>>>uplink": 77}
    )
    client = stats.StatsClient(channel_factory=lambda addr: channel)

    assert client.get_inbound_bytes("vless") == 77

    sent = channel.requests[0]
    assert sent.name == "inbound>>>in-vless>>>traffic>>>uplink"
    assert sent.reset is False


class _Servicer(command_pb2_grpc.StatsServiceServicer):
    def __init__(self, values):
        self.values = values

    def GetStats(self, request, context):
        if request.name not in self.values:
            context.abort(grpc.StatusCode.NOT_FOUND, f"{request.name} not found")
        return command_pb2.GetStatsResponse(
            stat=command_pb2.Stat(
                name=request.name, value=self.values[request.name]
            )
        )


@pytest.fixture
def stats_server_addr():
    """A real in-process gRPC server speaking the vendored StatsService proto."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    command_pb2_grpc.add_StatsServiceServicer_to_server(
        _Servicer(
            {
                stats.stat_name("u-1", "uplink"): 4000,
                stats.stat_name("u-1", "downlink"): 242,
                "inbound>>>in-vless>>>traffic>>>uplink": 99,
            }
        ),
        server,
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        server.stop(grace=None)


def test_get_bytes_against_real_grpc_server(stats_server_addr):
    client = stats.StatsClient(addr=stats_server_addr)

    assert client.get_bytes("u-1") == 4242
    assert client.get_inbound_bytes("vless") == 99


def test_rpc_failure_against_dead_server_raises_stats_error():
    # A channel to a port nothing listens on: must surface StatsError, not hang.
    client = stats.StatsClient(addr="127.0.0.1:1")

    with pytest.raises(stats.StatsError):
        client.get_bytes("u-1")
