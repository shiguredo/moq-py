"""MoQT の Sans I/O facade が制御ストリームを仕様どおり扱うかのテスト。"""

from __future__ import annotations

import pytest
from moqt._native import _CoreSession


def test_start_emits_control_stream_type_and_implementation_option() -> None:
    """
    自側制御ストリームが仕様どおりの形式で始まることを確認する。

    制御ストリームは stream type (vi64) で始まり、SETUP は
    Type (vi64) + Length (u16 big-endian) + Message Body が続く。
    実装名は SETUP の MOQT_IMPLEMENTATION option で通知する。
    """
    implementation = "moqt-py-test"
    session = _CoreSession.client(implementation)
    data = session.start()

    # draft-ietf-moq-transport-21 §6.4.1 (Unidirectional Streams) Table 3 の
    # SETUP 制御ストリームは 0x2F00 で、vi64 では 2 バイトになる。
    assert data[:2] == b"\xaf\x00"

    # draft-ietf-moq-transport-21 §9.1 (SETUP) Figure 6 の SETUP メッセージは
    # stream type と同じ 0x2F00 を使う。
    assert data[2:4] == b"\xaf\x00"

    # draft-ietf-moq-transport-21 §9.1.5 (MOQT IMPLEMENTATION) の option type は
    # 0x07 で、値は長さ付きバイト列として実装名を運ぶ。
    assert b"\x07\x0c" + implementation.encode() in data[4:]


def test_receive_control_waits_for_the_complete_setup_message() -> None:
    """
    peer 制御ストリームの断片が揃うまで SETUP を処理しないことを確認する。

    WebTransport の受信 fragment 境界は MoQT メッセージ境界と一致しないため、
    途中まで受信した制御メッセージは保持して続きの到着を待つ。
    """
    client = _CoreSession.client("moqt-py-test-client")
    server = _CoreSession.server("moqt-py-test-server")
    client.start()
    server_setup = server.start()

    # 先頭の 1 バイトは stream type の vi64 の途中であり、まだ何も処理できない。
    assert client.receive_control(server_setup[:1]) == []
    assert not client.established

    # 残りをまとめて渡すと SETUP 交換が完了する。
    events = client.receive_control(server_setup[1:])
    assert [event.kind for event in events] == ["established"]
    assert client.established


def test_receive_control_rejects_a_control_stream_buffer_over_the_limit() -> None:
    """
    制御ストリームの未完成データが上限を超えた場合に拒否することを確認する。

    peer が壊れたストリームを送り続けてもメモリを使い切らないようにする。
    """
    client = _CoreSession.client("moqt-py-test-client")
    client.start()

    # 128 KiB を超える未完成データは、デコードを試みる前に拒否する。
    with pytest.raises(ValueError, match="control stream buffer is too large"):
        client.receive_control(b"\x00" * (128 * 1024 + 1))


def test_client_rejects_an_implementation_option_over_the_wire_limit() -> None:
    """
    SETUP option の値長上限を超える実装名を拒否することを確認する。

    draft-ietf-moq-transport-21 §8.3 (Key-Value-Pair Structure) の値長上限は
    2^16-1 バイトである。
    """
    with pytest.raises(ValueError, match="implementation is too long"):
        _CoreSession.client("a" * (2**16))
