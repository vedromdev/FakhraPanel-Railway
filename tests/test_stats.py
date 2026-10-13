"""Tests for the Xray StatsService client and counter-reset-safe delta math."""

from concurrent import futures

import grpc
import pytest

from panel.xray import stats
from panel.xray.pb import command_pb2, command_pb2_grpc


def test_stat_name_format():
    assert stats.stat_name("u-1", "vless") == "user<<u-1>>@in-vless"


def test_compute_delta_normal():
    assert stats.compute_delta(1000, 400, 5000) == (5600, 1000)


def test_compute_delta_reset():
    # Counter reset (xray restarted): the raw value went backwards, so the
    # whole current reading counts as new traffic.
    assert stats.compute_delta(300, 5_000_000, 10_000) == (10_300, 300)


def test_compute_delta_first_seen():
    assert stats.compute_delta(0, 0, 0) == (0, 0)


class _FakeChannel:
    """Transport double implementing the grpc channel unary_unary seam.

    Records the *serialized* request bytes so tests assert on the exact
    wire payload, and either returns a canned response or raises.
    """

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.serialized_requests = []
        self.timeouts = []

    def unary_unary(self, path, request_serializer, response_deserializer,
                    **kwargs):
        def call(request, timeout=None, metadata=None, **kwargs):
            self.serialized_requests.append(request_serializer(request))
            self.timeouts.append(timeout)
            if self.error is not None:
                raise self.error
            return self.response

        return call


def test_get_bytes_uses_stub_response():
    channel = _FakeChannel(
        response=command_pb2.GetStatsResponse(
            stat=command_pb2.Stat(name=stats.stat_name("u-1", "vless"), value=1234)
        )
    )
    client = stats.StatsClient(channel_factory=lambda addr: channel)

    assert client.get_bytes("u-1", "vless") == 1234

    sent = command_pb2.GetStatsRequest.FromString(channel.serialized_requests[0])
    assert sent.name == stats.stat_name("u-1", "vless")
    assert sent.reset is False
    assert channel.timeouts[0] == stats.RPC_TIMEOUT_S  # RPCs are bounded


def test_get_bytes_raises_stats_error_on_rpc_failure():
    channel = _FakeChannel(error=grpc.RpcError("boom"))
    client = stats.StatsClient(channel_factory=lambda addr: channel)

    with pytest.raises(stats.StatsError):
        client.get_bytes("u-1", "vless")


def test_get_inbound_bytes_uses_inbound_stat_name():
    channel = _FakeChannel(
        response=command_pb2.GetStatsResponse(
            stat=command_pb2.Stat(name="inbound>>>in-vless>>>traffic>>>uplink", value=77)
        )
    )
    client = stats.StatsClient(channel_factory=lambda addr: channel)

    assert client.get_inbound_bytes("vless") == 77

    sent = command_pb2.GetStatsRequest.FromString(channel.serialized_requests[0])
    assert sent.name == "inbound>>>in-vless>>>traffic>>>uplink"


class _Servicer(command_pb2_grpc.StatsServiceServicer):
    def __init__(self, values):
        self.values = values

    def GetStats(self, request, context):
        return command_pb2.GetStatsResponse(
            stat=command_pb2.Stat(
                name=request.name, value=self.values.get(request.name, 0)
            )
        )


@pytest.fixture
def stats_server_addr():
    """A real in-process gRPC server speaking the vendored StatsService proto."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    command_pb2_grpc.add_StatsServiceServicer_to_server(
        _Servicer(
            {
                stats.stat_name("u-1", "vless"): 4242,
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

    assert client.get_bytes("u-1", "vless") == 4242
    assert client.get_inbound_bytes("vless") == 99


def test_rpc_failure_against_dead_server_raises_stats_error():
    # A channel to a port nothing listens on: must surface StatsError, not hang.
    client = stats.StatsClient(addr="127.0.0.1:1")

    with pytest.raises(stats.StatsError):
        client.get_bytes("u-1", "vless")
