"""`moqt.moqt` の codec と sans I/O セッション状態機械のテスト。"""

import pytest
from moqt.moqt import (
    PADDING_DATAGRAM_TYPE,
    Event,
    Session,
    classify_data_stream_type,
    decode_message,
    decode_varint,
    decode_varint_prefix,
    encode_varint,
    is_padding_datagram,
    setup_stream_type,
)

# 実装名がワイヤ長の上限を超えるケースで使う長さ。
# draft-ietf-moq-transport-21 §8.3 (Key-Value-Pair Structure) の値長上限は 2^16-1 である。
IMPLEMENTATION_WIRE_LIMIT = 2**16


def _request_id(event: Event) -> int:
    """イベントの Request ID を取り出す。"""
    assert event.request_id is not None
    return event.request_id


def _message_data(event: Event) -> bytes:
    """イベントのメッセージバイト列を取り出す。"""
    assert event.message_data is not None
    return event.message_data


def _event_data(event: Event) -> bytes:
    """イベントの送信バイト列を取り出す。"""
    assert event.data is not None
    return event.data


def _setup() -> tuple[Session, Session]:
    """SETUP 交換を終えた client / server の組を作る。"""
    client = Session.client("c")
    server = Session.server("s")
    client_setup = client.start()
    server_setup = server.start()
    server.receive_control(client_setup)
    client.receive_control(server_setup)
    return client, server


def _round_trip(
    client: Session,
    server: Session,
    stream_id: int,
    events: list[Event],
    request_id: int,
) -> list[Event]:
    """request を送り、REQUEST_OK を受け取るまでを往復させる。"""
    client.register_local_request_stream(stream_id, request_id)
    server.receive_request_stream(stream_id, _message_data(events[0]), "peer")
    ok = server.send_request_ok(request_id, {}, {})
    return client.receive_request_stream(stream_id, _message_data(ok[0]), "local")


def _subscribe_round_trip(
    client: Session,
    server: Session,
    stream_id: int,
    track_alias: int = 1,
) -> int:
    """SUBSCRIBE を送り、SUBSCRIBE_OK を受け取るまでを往復させる。"""
    events = client.send_subscribe([b"ns"], b"t", {})
    request_id = _request_id(events[0])
    client.register_local_request_stream(stream_id, request_id)
    server.receive_request_stream(stream_id, _message_data(events[0]), "peer")
    ok = server.send_subscribe_ok(request_id, track_alias, {}, {})
    client.receive_request_stream(stream_id, _message_data(ok[0]), "local")
    return request_id


def test_request_stream_close_is_reported_for_a_local_request() -> None:
    """
    自側が開始した request stream の終端がセッションへ通知されることを確認する。

    状態機械は request を Request ID で識別する。ストリーム ID と Request ID が
    異なる場合でも、終端の通知を未知の Request ID として拒否してはならない。
    """
    client, server = _setup()
    stream_id = 4
    request_id = _subscribe_round_trip(client, server, stream_id)
    assert request_id != stream_id

    # FIN として終端を通知する。未知の Request ID を渡していると PROTOCOL_VIOLATION になる
    client.receive_request_stream_closed(stream_id, False, None)


def test_request_stream_close_is_reported_for_a_peer_request() -> None:
    """
    peer が開始した request stream の終端がセッションへ通知されることを確認する。

    自側が開始していない request でも、最初のメッセージが運んだ Request ID で
    終端を通知しなければならない。
    """
    client, server = _setup()
    stream_id = 4
    _subscribe_round_trip(client, server, stream_id)

    server.receive_request_stream_closed(stream_id, False, None)


def test_request_stream_close_is_ignored_for_an_unknown_stream() -> None:
    """
    自側が把握していない request stream の終端を無視することを確認する。

    状態機械もそのストリームを知らないため、通知してはならない。
    """
    client, _server = _setup()

    client.receive_request_stream_closed(42, False, None)


def test_request_stream_close_is_ignored_after_the_first_notification() -> None:
    """
    同じ request stream の終端を 2 回通知しても拒否しないことを確認する。

    状態機械は 2 回目を未知の Request ID として拒否するため、I/O 層が
    1 回目で対応を破棄して 2 回目を通知しないようにしている。
    """
    client, server = _setup()
    stream_id = 4
    _subscribe_round_trip(client, server, stream_id)

    client.receive_request_stream_closed(stream_id, False, None)
    client.receive_request_stream_closed(stream_id, True, 0)


# ─── SETUP と制御ストリーム ─────────────────────────────────


def test_start_emits_control_stream_type_and_implementation_option() -> None:
    """
    自側制御ストリームが仕様どおりの形式で始まることを確認する。

    制御ストリームは stream type (vi64) で始まり、SETUP は
    Type (vi64) + Length (u16 big-endian) + Message Body が続く。
    実装名は SETUP の MOQT_IMPLEMENTATION option で通知する。
    """
    implementation = "moqt-py-test"
    session = Session.client(implementation)
    data = session.start()

    # draft-ietf-moq-transport-21 §6.4.1 (Unidirectional Streams) Table 3 の
    # SETUP 制御ストリームは 0x2F00 で、vi64 では 2 バイトになる。
    assert data[:2] == b"\xaf\x00"

    # draft-ietf-moq-transport-21 §9.1 (SETUP) の SETUP メッセージは
    # stream type と同じ 0x2F00 を使う。
    assert data[2:4] == b"\xaf\x00"

    # draft-ietf-moq-transport-21 §9.1.5 (MOQT IMPLEMENTATION) の option type は
    # 0x07 で、値は長さ付きバイト列として実装名を運ぶ。
    value = implementation.encode()
    assert b"\x07" + encode_varint(len(value)) + value in data[4:]


def test_receive_control_waits_for_the_complete_setup_message() -> None:
    """
    peer 制御ストリームの断片が揃うまで SETUP を処理しないことを確認する。

    WebTransport の受信 fragment 境界は MoQT メッセージ境界と一致しないため、
    途中まで受信した制御メッセージは保持して続きの到着を待つ。
    """
    client = Session.client("moqt-py-test-client")
    server = Session.server("moqt-py-test-server")
    client.start()
    server_setup = server.start()

    # 先頭の 1 バイトは stream type の vi64 の途中であり、まだ何も処理できない。
    assert client.receive_control(server_setup[:1]) == []
    assert not client.established

    # 残りをまとめて渡すと SETUP 交換が完了する。
    events = client.receive_control(server_setup[1:])
    assert [event.kind for event in events] == ["established"]
    assert client.established


def test_receive_control_rejects_an_unexpected_stream_type() -> None:
    """
    peer 制御ストリームの stream type が SETUP でない場合に拒否することを確認する。

    制御ストリームの stream type は SETUP (0x2F00) でなければならない。
    """
    client = Session.client("moqt-py-test-client")
    client.start()

    # stream type 0x00 は制御ストリームでもデータストリームでもない。
    with pytest.raises(RuntimeError, match="unexpected control stream type"):
        client.receive_control(b"\x00\x00")


def test_client_rejects_an_implementation_option_over_the_wire_limit() -> None:
    """
    SETUP option の値長上限を超える実装名を拒否することを確認する。

    draft-ietf-moq-transport-21 §8.3 (Key-Value-Pair Structure) の値長上限は
    2^16-1 バイトである。
    """
    with pytest.raises(ValueError, match="implementation is too long"):
        Session.client("a" * IMPLEMENTATION_WIRE_LIMIT)


# ─── varint ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, b"\x00"),
        (127, b"\x7f"),
        (128, b"\x80\x80"),
        (16383, b"\xbf\xff"),
        (16384, b"\xc0\x40\x00"),
        (2**62 - 1, b"\xff\x3f\xff\xff\xff\xff\xff\xff\xff"),
        (2**62, b"\xff\x40\x00\x00\x00\x00\x00\x00\x00"),
        (2**64 - 1, b"\xff\xff\xff\xff\xff\xff\xff\xff\xff"),
    ],
    ids=[
        "zero",
        "7bit-max",
        "2byte-min",
        "2byte-max",
        "3byte-min",
        "9byte-min-1",
        "9byte-min",
        "max",
    ],
)
def test_encode_varint_uses_the_minimum_length(value: int, expected: bytes) -> None:
    """
    vi64 が先頭 1 ビット列の長さで最小バイト数にエンコードされることを確認する。

    先頭バイトの leading-1-bits がエンコード長を決める
    (draft-ietf-moq-transport-21 §8.1 (Variable-Length Integers) Table 3)。
    """
    assert encode_varint(value) == expected


@pytest.mark.parametrize(
    "value",
    [0, 127, 128, 16383, 16384, 2**28 - 1, 2**28, 2**62 - 1, 2**62, 2**64 - 1],
    ids=[
        "zero",
        "7bit-max",
        "2byte-min",
        "2byte-max",
        "3byte-min",
        "4byte-max",
        "5byte-min",
        "9byte-min-1",
        "9byte-min",
        "max",
    ],
)
def test_varint_round_trips(value: int) -> None:
    """
    vi64 の encode と decode が任意の u64 で往復することを確認する。

    消費バイト数がエンコード長と一致することも検証する。
    """
    encoded = encode_varint(value)
    decoded, consumed = decode_varint(encoded)
    assert decoded == value
    assert consumed == len(encoded)


def test_decode_varint_accepts_a_non_minimal_encoding() -> None:
    """
    非最小エンコーディングの vi64 を受理することを確認する。

    draft-ietf-moq-transport-21 Appendix A.3 (Since draft-ietf-moq-transport-17) は
    非最小エンコーディングを許容する。
    """
    # 2 バイト表現の 10xxxxxx xxxxxxxx で値 1 を表す。
    assert decode_varint(b"\x80\x01") == (1, 2)


def test_decode_varint_rejects_a_truncated_value() -> None:
    """
    途中で切れた vi64 を `ValueError` で拒否することを確認する。

    先頭バイトが 3 バイト表現を宣言しているのに 2 バイトしかない場合は
    デコードできない。
    """
    with pytest.raises(ValueError, match="unexpected end of buffer"):
        decode_varint(b"\xc0\x40")


def test_decode_varint_prefix_reports_an_incomplete_value_as_none() -> None:
    """
    途中で切れた vi64 を `None` として扱うことを確認する。

    ストリーム種別の判定では、続きの到着を待つために未完成であることを
    例外ではなく `None` で受け取る必要がある。
    """
    assert decode_varint_prefix(b"") is None
    assert decode_varint_prefix(b"\xc0\x40") is None
    assert decode_varint_prefix(b"\xc0\x40\x00") == (16384, 3)


# ─── 制御メッセージのデコード ───────────────────────────────


def test_decode_message_reads_a_setup_from_the_wire() -> None:
    """
    制御ストリームの生バイト列から SETUP をデコードできることを確認する。

    `moqt.moqt.decode_message` はセッションを介さずにメッセージ 1 件を取り出す。
    制御ストリームの先頭 2 バイトは stream type であるため読み飛ばす。
    """
    server = Session.server("moqt-py-test-server")
    data = server.start()

    # 先頭 2 バイトは制御ストリームの stream type (0x2F00) である。
    message, consumed = decode_message(data[2:])

    assert message.kind == "setup"
    assert message.type_id == 0x2F00
    # SETUP は request ではないため Request ID を持たない。
    assert message.request_id is None
    assert message.raw == data[2 : 2 + consumed]
    assert b"moqt-py-test-server" in data[2:]


def test_decode_message_consumes_exactly_one_message() -> None:
    """
    連結された制御メッセージから 1 件だけを取り出すことを確認する。

    制御ストリームはメッセージを連結して運ぶため、消費バイト数を使って
    次のメッセージの位置が分かる必要がある。
    """
    client = Session.client("c")
    server = Session.server("moqt-py-test-server")
    client_setup = client.start()
    server_setup = server.start()
    server.receive_control(client_setup)
    client.receive_control(server_setup)

    # SETUP 交換が済むと GOAWAY を送れるようになる。
    first = server_setup[2:]
    goaway = _event_data(server.send_goaway(b"", 5000)[0])

    message, consumed = decode_message(first + goaway)

    assert message.kind == "setup"
    assert consumed == len(first)

    following, following_consumed = decode_message((first + goaway)[consumed:])
    assert following.kind == "goaway"
    assert following_consumed == len(goaway)
    assert following.body["timeout"] == 5000


def test_decode_message_rejects_a_truncated_message() -> None:
    """
    本文が揃っていない制御メッセージを `ValueError` で拒否することを確認する。

    エラーメッセージには期待バイト数と実際のバイト数が入る。
    """
    data = Session.server("moqt-py-test-server").start()[2:]
    truncated = data[:-1]

    with pytest.raises(
        ValueError, match=rf"expected {len(data)} bytes, got {len(truncated)} bytes"
    ):
        decode_message(truncated)


def test_decode_message_reports_a_missing_header_as_incomplete() -> None:
    """
    Type の直後で切れた入力を未完成として拒否することを確認する。

    Length (u16) が揃わないとメッセージ長が決まらないため、その旨を報告する。
    """
    with pytest.raises(ValueError, match="do not fit in 3 bytes"):
        decode_message(b"\xaf\x00\xaf")


def test_message_equality_is_based_on_the_wire_bytes() -> None:
    """
    同じバイト列から作った `Message` が等しいと判定されることを確認する。

    `Message` は frozen な値オブジェクトであり、テストで受信結果を
    期待値と比較するために使う。
    """
    data = Session.server("moqt-py-test-server").start()[2:]

    first, _ = decode_message(data)
    second, _ = decode_message(data)

    assert first == second
    assert first != "setup"
    assert "kind=setup" in repr(first)


# ─── ストリーム種別の判定 ───────────────────────────────────


@pytest.mark.parametrize(
    ("type_id", "expected"),
    [
        (0x2F00, None),
        (0x05, "fetch"),
        (0x10, "subgroup"),
        (0x30, "subgroup"),
        (0x7F, "subgroup"),
        (0x132B3E28, "padding"),
        (0x08, None),
        (0x00, None),
        (0x99, None),
    ],
    ids=[
        "setup",
        "fetch",
        "subgroup-0x10",
        "subgroup-0x30",
        "subgroup-0x7f",
        "padding",
        "bit4-clear",
        "unassigned",
        "unknown",
    ],
)
def test_classify_data_stream_type(type_id: int, expected: str | None) -> None:
    """
    stream type の varint からデータストリームの種別を判定することを確認する。

    制御ストリームと未知の値は `None` になる。subgroup は bit4 が立つ値である。
    (draft-ietf-moq-transport-21 §6.4.1 (Unidirectional Streams) Table 3)
    """
    assert classify_data_stream_type(type_id) == expected


def test_setup_stream_type_returns_the_control_stream_type() -> None:
    """
    制御ストリームの stream type が SETUP (0x2F00) であることを確認する。

    (draft-ietf-moq-transport-21 §6.4.1 (Unidirectional Streams) Table 3)
    """
    assert setup_stream_type() == 0x2F00


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (encode_varint(PADDING_DATAGRAM_TYPE) + b"\x00", True),
        (b"\x00\x00", False),
        (b"", False),
    ],
    ids=["padding", "object", "empty"],
)
def test_is_padding_datagram(data: bytes, expected: bool) -> None:
    """
    データグラムの先頭 varint からパディングかを判定することを確認する。

    データグラムは stream type を持たないため、先頭の varint だけで判定する。
    (draft-ietf-moq-transport-21 §11.5.2 (Padding Datagrams))
    """
    assert is_padding_datagram(data) is expected


# ─── request の往復 ─────────────────────────────────────────


def test_track_status() -> None:
    """TRACK_STATUS の応答を確認する。"""
    client, server = _setup()
    events = client.send_track_status([b"ns"], b"t", {})
    request_id = _request_id(events[0])
    result = _round_trip(client, server, 4, events, request_id)
    assert [event.kind for event in result] == ["request_ok"]


def test_subscribe_namespace() -> None:
    """SUBSCRIBE_NAMESPACE と NAMESPACE 通知を確認する。"""
    client, server = _setup()
    events = client.send_subscribe_namespace([b"ns"], {})
    request_id = _request_id(events[0])
    _round_trip(client, server, 4, events, request_id)
    notice = server.send_namespace(request_id, [b"suffix"])
    kinds = [
        event.kind for event in client.receive_request_stream(4, _message_data(notice[0]), "local")
    ]
    assert "namespace" in kinds


def test_publish_namespace() -> None:
    """PUBLISH_NAMESPACE の応答を確認する。"""
    client, server = _setup()
    events = client.send_publish_namespace([b"ns"], {})
    request_id = _request_id(events[0])
    result = _round_trip(client, server, 4, events, request_id)
    assert [event.kind for event in result] == ["request_ok"]


def test_subscribe_tracks() -> None:
    """SUBSCRIBE_TRACKS の応答を確認する。"""
    client, server = _setup()
    events = client.send_subscribe_tracks([b"ns"], {})
    request_id = _request_id(events[0])
    result = _round_trip(client, server, 4, events, request_id)
    assert [event.kind for event in result] == ["request_ok"]


def test_goaway() -> None:
    """GOAWAY の送受信を確認する。"""
    client, server = _setup()
    events = server.send_goaway(b"", 5000)
    kinds = [event.kind for event in client.receive_control(_event_data(events[0]))]
    assert "goaway" in kinds


def test_padding() -> None:
    """パディングの送信要求を確認する。"""
    client, _server = _setup()
    events = client.send_padding_stream(64)
    assert [event.kind for event in events] == ["send_padding_stream"]
    events = client.send_padding_datagram(32)
    assert [event.kind for event in events] == ["send_padding_datagram"]
