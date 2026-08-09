"""MoQT SETUP の Sans I/O facade に対する Property-Based Testing。"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st
from moqt._native import _CoreSession


def _split(data: bytes, sizes: list[int]) -> list[bytes]:
    """指定されたサイズ列を繰り返して bytes を空でない断片へ分割する。"""
    chunks: list[bytes] = []
    offset = 0
    index = 0
    while offset < len(data):
        size = sizes[index % len(sizes)]
        chunks.append(data[offset : offset + size])
        offset += size
        index += 1
    return chunks


@given(
    client_sizes=st.lists(st.integers(min_value=1, max_value=16), min_size=1, max_size=16),
    server_sizes=st.lists(st.integers(min_value=1, max_value=16), min_size=1, max_size=16),
)
def prop_setup_establishes_for_arbitrary_stream_fragmentation(
    client_sizes: list[int],
    server_sizes: list[int],
) -> None:
    """SETUP が任意の stream fragment 境界でも client/server 双方で成立する。"""
    client = _CoreSession.client("moqt-py-test-client")
    server = _CoreSession.server("moqt-py-test-server")
    client_setup = client.start()
    server_setup = server.start()

    # WebTransport の受信 fragment 境界は MoQT メッセージ境界と一致しない。
    for chunk in _split(client_setup, client_sizes):
        server.receive_control(chunk)
    for chunk in _split(server_setup, server_sizes):
        client.receive_control(chunk)

    assert client.established
    assert server.established
