"""`moqt.moqt` の codec と sans I/O セッション状態機械のテスト。"""

import pytest
from moqt import _native, moqt
from moqt.moqt import (
    MANDATORY_TRACK_PROPERTY_MIN,
    PADDING_DATAGRAM_TYPE,
    PROP_PRIOR_GROUP_ID_GAP,
    PROP_PRIOR_OBJECT_ID_GAP,
    Event,
    LocationFilter,
    MessageParameters,
    ObjectProperties,
    Session,
    TrackProperties,
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


def _fetch_round_trip(client: Session, server: Session, stream_id: int) -> int:
    """FETCH を送り、FETCH_OK を受け取るまでを往復させる。

    購読と同じ Track への FETCH は、まだ配信実績が無い間は DOES_NOT_EXIST で
    拒否されるため、購読とは別の Track を使う。
    """
    events = client.send_fetch([b"fetch-ns"], b"fetch-track", {})
    request_id = _request_id(events[0])
    client.register_local_request_stream(stream_id, request_id)
    server.receive_request_stream(stream_id, _message_data(events[0]), "peer")
    ok = server.send_fetch_ok(request_id, False, (0, 0), {}, {})
    client.receive_request_stream(stream_id, _message_data(ok[0]), "local")
    return request_id


def _object_datagram(
    track_alias: int,
    group_id: int,
    object_id: int,
    payload: bytes,
    publisher_priority: int | None,
) -> bytes:
    """OBJECT_DATAGRAM のバイト列を組み立てる。

    Type Flags の並びは Type Flags (vi64)、Track Alias (vi64)、Group ID (vi64)、
    Object ID (vi64)、Publisher Priority (8 bits、省略可能)、Payload である。
    Object ID が 0 の場合は ZERO_OBJECT_ID bit を立てて Object ID を省略し、
    Publisher Priority が `None` の場合は DEFAULT_PRIORITY bit を立てて省略する
    (draft-ietf-moq-transport-21 §11.2.1 (Object Datagram))。
    """
    type_byte = 0x00
    if object_id == 0:
        type_byte |= 0x04
    if publisher_priority is None:
        type_byte |= 0x08
    data = bytearray()
    data += encode_varint(type_byte)
    data += encode_varint(track_alias)
    data += encode_varint(group_id)
    if object_id != 0:
        data += encode_varint(object_id)
    if publisher_priority is not None:
        data.append(publisher_priority)
    data += payload
    return bytes(data)


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


def test_setup_options_are_sent_and_observed() -> None:
    """
    SETUP で送った Setup Option が peer から参照できることを確認する。

    偶数型は varint、奇数型は長さ付きバイト列で表現する
    (draft-ietf-moq-transport-21 §9.1 (SETUP) / §16.4 (Setup Options))。
    MOQT_IMPLEMENTATION は `implementation` 引数が担う。
    """
    client = Session.client(
        "c",
        {
            moqt.SETUP_OPTION_MAX_AUTH_TOKEN_CACHE_SIZE: 16,
            moqt.SETUP_OPTION_AUTHORIZATION_TOKEN: {
                "kind": "use_value",
                "token_type": 1,
                "token_value": b"secret",
            },
        },
    )
    server = Session.server("s", {moqt.SETUP_OPTION_MAX_FILTER_RANGES: 8})

    # SETUP を受信する前は peer の Setup Option が分からない
    assert client.peer_setup_options() == {}

    client_setup = client.start()
    server_setup = server.start()
    server.receive_control(client_setup)
    client.receive_control(server_setup)

    assert client.peer_setup_options()[moqt.SETUP_OPTION_MOQT_IMPLEMENTATION] == b"s"
    assert client.peer_setup_options()[moqt.SETUP_OPTION_MAX_FILTER_RANGES] == 8
    assert server.peer_setup_options()[moqt.SETUP_OPTION_MOQT_IMPLEMENTATION] == b"c"
    assert server.peer_setup_options()[moqt.SETUP_OPTION_MAX_AUTH_TOKEN_CACHE_SIZE] == 16
    # AUTHORIZATION_TOKEN は Token 構造の辞書であり、複数指定できるためリストになる
    assert server.peer_setup_options()[moqt.SETUP_OPTION_AUTHORIZATION_TOKEN] == [
        {"kind": "use_value", "token_type": 1, "token_value": b"secret"}
    ]


def test_setup_option_rejects_moqt_implementation() -> None:
    """
    MOQT_IMPLEMENTATION を setup_options で二重に指定できないことを確認する。

    MOQT_IMPLEMENTATION は `implementation` 引数が担う
    (draft-ietf-moq-transport-21 §9.1.5 (MOQT_IMPLEMENTATION))。
    """
    with pytest.raises(ValueError, match="MOQT_IMPLEMENTATION is specified"):
        Session.client("c", {moqt.SETUP_OPTION_MOQT_IMPLEMENTATION: b"other"})


def test_range_filter_requires_the_peer_to_declare_max_filter_ranges() -> None:
    """
    peer が MAX_FILTER_RANGES を宣言している場合だけ Range Filter を送れることを確認する。

    draft-ietf-moq-transport-21 §9.1.6 (MAX FILTER RANGES): Range Filter は
    自側 SETUP の MAX_FILTER_RANGES が 0 でない場合だけ許される。
    """
    # SetID=0 で Object ID 0..=1 を指定する Range Filter
    # (draft-ietf-moq-transport-21 §3.3.2 (Range Filters))
    object_id_filter = bytes([0]) + encode_varint(0) + encode_varint(1)

    # 宣言が無ければ送信できない
    client, _server = _setup()
    with pytest.raises(RuntimeError, match="MAX_FILTER_RANGES"):
        client.send_subscribe([b"ns"], b"t", {moqt.PARAM_OBJECTID_FILTER: object_id_filter})

    # 宣言があれば送信できる
    client = Session.client("c")
    server = Session.server("s", {moqt.SETUP_OPTION_MAX_FILTER_RANGES: 8})
    client_setup = client.start()
    server_setup = server.start()
    server.receive_control(client_setup)
    client.receive_control(server_setup)

    events = client.send_subscribe([b"ns"], b"t", {moqt.PARAM_OBJECTID_FILTER: object_id_filter})
    assert [event.kind for event in events] == ["send_request"]


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
    """TRACK_STATUS の送信を確認する。

    TRACK_STATUS は relay が返す応答であり、endpoint は受信しない
    (moqt-rs の `recv_request` が `subscribe` / `publish` / `fetch` だけを扱う)。
    """
    client, _server = _setup()
    events = client.send_track_status([b"ns"], b"t", {})
    assert [event.kind for event in events] == ["send_request"]
    assert client.next_local_request_id() >= 0


def test_peer_request_rejects_track_status() -> None:
    """peer から届いた TRACK_STATUS を endpoint が拒否することを確認する。"""
    client, server = _setup()
    events = client.send_track_status([b"ns"], b"t", {})
    request_id = _request_id(events[0])
    client.register_local_request_stream(4, request_id)
    with pytest.raises(RuntimeError, match="unsupported request message"):
        server.receive_request_stream(4, _message_data(events[0]), "peer")


def test_request_update_uses_a_separate_request_id() -> None:
    """REQUEST_UPDATE が購読とは別の Request ID を消費することを確認する。

    REQUEST_UPDATE の wire Request ID は対象 request のものと一致しない。対象 request
    は同じ bidi stream 上で送ることで識別されるため、受信側はストリームに紐付けた
    Request ID で解決しなければならない
    (draft-ietf-moq-transport-21 §6.4.2.1 (Request ID) / §9.5 (REQUEST_UPDATE))。
    """
    client, server = _setup()
    request_id = _subscribe_round_trip(client, server, 4)

    updates = client.send_request_update(request_id, {})
    assert [event.kind for event in updates] == ["send_on_stream"]

    kinds = [
        event.kind for event in server.receive_request_stream(4, _message_data(updates[0]), "peer")
    ]
    assert kinds == ["request_update"]


def test_object_properties_round_trip() -> None:
    """Object Properties の encode / decode が往復することを確認する。

    偶数型は varint、奇数型は長さ付きバイト列である
    (draft-ietf-moq-transport-21 §8.3 (Key-Value-Pair Structure))。
    """
    properties = ObjectProperties()
    properties.add(PROP_PRIOR_GROUP_ID_GAP, 2)
    properties.add(PROP_PRIOR_OBJECT_ID_GAP, 7)
    properties.add(0x0D, b"\x01\x02\x03")

    encoded = properties.encode()
    decoded, consumed = ObjectProperties.decode(encoded)

    assert consumed == len(encoded)
    assert decoded == properties
    assert decoded.prior_group_id_gap == 2
    assert decoded.prior_object_id_gap == 7
    assert decoded.to_dict()[0x0D] == b"\x01\x02\x03"


def test_object_properties_rejects_a_truncated_block() -> None:
    """切り詰めたプロパティブロックを decode が拒否することを確認する。

    宣言された Properties Length より後続が短い場合は `ValueError` になる。
    """
    properties = ObjectProperties()
    properties.add(PROP_PRIOR_GROUP_ID_GAP, 2)
    encoded = properties.encode()

    with pytest.raises(ValueError, match="unexpected end of buffer"):
        ObjectProperties.decode(encoded[:-1])


def test_object_properties_rejects_a_value_that_is_too_long() -> None:
    """65535 バイトを超える奇数型の値を encode が拒否することを確認する。

    (draft-ietf-moq-transport-21 §8.3 (Key-Value-Pair Structure))
    """
    properties = ObjectProperties()
    properties.add(0x0D, b"\x00" * (2**16))

    with pytest.raises(ValueError, match="too long"):
        properties.encode()


def test_track_properties_accessors() -> None:
    """Track Properties の型付きアクセサを確認する。"""
    properties = TrackProperties()
    properties.add(moqt.PROP_DEFAULT_PUBLISHER_PRIORITY, 200)
    properties.add(moqt.PROP_DEFAULT_PUBLISHER_GROUP_ORDER, 0x2)
    properties.add(moqt.PROP_DYNAMIC_GROUPS, 1)

    assert len(properties) == 3
    assert properties.default_publisher_priority == 200
    assert properties.default_publisher_group_order == 0x2
    assert properties.dynamic_groups == 1
    assert properties.to_dict()[moqt.PROP_DYNAMIC_GROUPS] == 1
    assert properties.has_unknown_mandatory is False


def test_track_properties_detects_an_unknown_mandatory_property() -> None:
    """未知の必須 Track Property を検出することを確認する。

    必須の範囲は 0x4000-0x7FFF である
    (draft-ietf-moq-transport-21 §3.6 (Mandatory Track Properties))。
    """
    properties = TrackProperties()
    properties.add(MANDATORY_TRACK_PROPERTY_MIN, 1)

    assert properties.has_unknown_mandatory is True


def test_track_properties_round_trip() -> None:
    """Track Properties の encode / decode が往復することを確認する。

    Track Properties には長さプレフィックスが無く、encode 結果はセッションが運ぶ
    KVP 列そのものになる (draft-ietf-moq-transport-21 §8.4 (Track and Object Properties))。
    """
    properties = TrackProperties()
    properties.add(moqt.PROP_DEFAULT_PUBLISHER_PRIORITY, 200)
    properties.add(0x0D, b"\x01\x02\x03")
    properties.add(0x20, 5)

    encoded = properties.encode()
    decoded = TrackProperties.decode(encoded)

    assert decoded == properties
    assert decoded.encode() == encoded
    assert decoded.to_dict() == {
        moqt.PROP_DEFAULT_PUBLISHER_PRIORITY: 200,
        0x0D: b"\x01\x02\x03",
        0x20: 5,
    }
    # decode 後はワイヤ上の型番号の昇順で列挙される
    assert decoded.items() == [
        (0x0D, b"\x01\x02\x03"),
        (moqt.PROP_DEFAULT_PUBLISHER_PRIORITY, 200),
        (0x20, 5),
    ]


def test_track_properties_encode_of_an_empty_set_is_empty() -> None:
    """空の Track Properties が 0 バイトへエンコードされることを確認する。

    Track scope のプロパティは任意であり、省略時は長さプレフィックスも書かない
    (draft-ietf-moq-transport-21 §8.4 (Track and Object Properties))。
    """
    assert TrackProperties().encode() == b""
    assert len(TrackProperties.decode(b"")) == 0


def test_track_properties_rejects_a_duplicate_type() -> None:
    """同一の型番号を 2 度持つ Track Properties を encode が拒否することを確認する。

    型番号は delta encoding の差分 0 で表現され、受信側では重複として扱われる
    (draft-ietf-moq-transport-21 §8.3 (Key-Value-Pair Structure))。
    """
    properties = TrackProperties()
    properties.add(0x20, 1)
    properties.add(0x20, 2)

    with pytest.raises(ValueError, match="duplicate"):
        properties.encode()


def test_track_properties_rejects_a_nested_immutable_properties() -> None:
    """入れ子の IMMUTABLE_PROPERTIES を decode が拒否することを確認する。

    (draft-ietf-moq-transport-21 §10.7 (Immutable Properties))
    """
    # 内側に 0x0B 自身を含む KVP 列を IMMUTABLE_PROPERTIES の値として組み立てる
    inner = encode_varint(moqt.PROP_IMMUTABLE_PROPERTIES) + encode_varint(0)
    encoded = encode_varint(moqt.PROP_IMMUTABLE_PROPERTIES) + encode_varint(len(inner)) + inner

    with pytest.raises(ValueError, match="nested IMMUTABLE_PROPERTIES"):
        TrackProperties.decode(encoded)


def test_track_properties_rejects_a_truncated_value() -> None:
    """途中で切れた Track Properties を decode が拒否することを確認する。"""
    properties = TrackProperties()
    properties.add(0x0D, b"\x01\x02\x03")

    with pytest.raises(ValueError, match="unexpected end of buffer"):
        TrackProperties.decode(properties.encode()[:-1])


def test_track_properties_encode_rejects_a_type_value_parity_mismatch() -> None:
    """型番号と値の形式が食い違う Track Property を encode が拒否することを確認する。

    偶数型は varint、奇数型は長さ付きバイト列である
    (draft-ietf-moq-transport-21 §8.4 (Track and Object Properties))。
    """
    properties = TrackProperties()
    properties.add(0x20, b"\x01")

    with pytest.raises(ValueError, match="invalid parameter"):
        properties.encode()


def test_object_properties_iteration_matches_items() -> None:
    """`ObjectProperties` の列挙と `items()` が同じ列を返すことを確認する。"""
    properties = ObjectProperties()
    properties.add(PROP_PRIOR_GROUP_ID_GAP, 2)
    properties.add(0x0D, b"\x01\x02")

    assert list(properties) == [(PROP_PRIOR_GROUP_ID_GAP, 2), (0x0D, b"\x01\x02")]
    assert properties.items() == list(properties)
    assert list(iter(properties)) == list(properties)


def test_object_properties_iteration_uses_a_snapshot() -> None:
    """列挙の途中で追加しても列挙中の列が変わらないことを確認する。"""
    properties = ObjectProperties()
    properties.add(PROP_PRIOR_GROUP_ID_GAP, 2)
    iterator = iter(properties)

    properties.add(0x0D, b"\x01\x02")

    assert next(iterator) == (PROP_PRIOR_GROUP_ID_GAP, 2)
    with pytest.raises(StopIteration):
        next(iterator)


def test_object_properties_find_varint_returns_none_for_a_byte_value() -> None:
    """バイト列型の型番号を `find_varint` が引けないことを確認する。"""
    properties = ObjectProperties()
    properties.add(0x0D, b"\x01\x02")

    assert properties.find_varint(0x0D) is None
    assert properties.find_varint(PROP_PRIOR_GROUP_ID_GAP) is None


def test_track_properties_find_varint_reads_immutable_properties() -> None:
    """`find_varint` が IMMUTABLE_PROPERTIES の内側の値を引くことを確認する。

    draft-ietf-moq-transport-21 §10.7 (Immutable Properties) の「MUST search both」
    に従い、外側の値が優先される。
    """
    # 内側には 0x04 (偶数型) だけを置く
    inner = encode_varint(moqt.PROP_MAX_CACHE_DURATION) + encode_varint(9)
    properties = TrackProperties()
    properties.add(moqt.PROP_IMMUTABLE_PROPERTIES, inner)

    assert properties.find_varint(moqt.PROP_MAX_CACHE_DURATION) == 9

    # 外側に同じ型番号があれば外側の値が優先される
    properties.add(moqt.PROP_MAX_CACHE_DURATION, 3)

    assert properties.find_varint(moqt.PROP_MAX_CACHE_DURATION) == 3
    assert properties.find_varint(moqt.PROP_DYNAMIC_GROUPS) is None


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


# ─── 定数 ───────────────────────────────────────────────────


def test_session_termination_codes_are_drafted_values() -> None:
    """Session Termination のコードが draft の値と一致することを確認する。

    (draft-ietf-moq-transport-21 §16.11.1 (Session Termination Codes))
    """
    assert moqt.SESSION_NO_ERROR == 0x0
    assert moqt.SESSION_INTERNAL_ERROR == 0x1
    assert moqt.SESSION_UNAUTHORIZED == 0x2
    assert moqt.SESSION_PROTOCOL_VIOLATION == 0x3
    assert moqt.SESSION_INVALID_REQUEST_ID == 0x4
    assert moqt.SESSION_DUPLICATE_TRACK_ALIAS == 0x5
    assert moqt.SESSION_KEY_VALUE_FORMATTING_ERROR == 0x6
    assert moqt.SESSION_INVALID_PATH == 0x8
    assert moqt.SESSION_MALFORMED_PATH == 0x9
    assert moqt.SESSION_GOAWAY_TIMEOUT == 0x10
    assert moqt.SESSION_CONTROL_MESSAGE_TIMEOUT == 0x11
    assert moqt.SESSION_DATA_STREAM_TIMEOUT == 0x12
    assert moqt.SESSION_TOO_MANY_REQUEST_UPDATES == 0x1B

    # 自側の判断で使うコードは wire の値域の外にある
    assert moqt.SESSION_LOCAL_FILTER_MISMATCH == 0xFFFF_FFFF_FFFF_FF01
    assert moqt.SESSION_LOCAL_DATAGRAM_TIMEOUT == 0xFFFF_FFFF_FFFF_FF02


def test_request_error_codes_are_drafted_values() -> None:
    """REQUEST_ERROR のコードが draft の値と一致することを確認する。

    (draft-ietf-moq-transport-21 §16.11.2 (REQUEST_ERROR Codes))
    """
    assert moqt.REQUEST_INTERNAL_ERROR == 0x0
    assert moqt.REQUEST_UNAUTHORIZED == 0x1
    assert moqt.REQUEST_TIMEOUT == 0x2
    assert moqt.REQUEST_NOT_SUPPORTED == 0x3
    assert moqt.REQUEST_MALFORMED_AUTH_TOKEN == 0x4
    assert moqt.REQUEST_EXPIRED_AUTH_TOKEN == 0x5
    assert moqt.REQUEST_GOING_AWAY == 0x6
    assert moqt.REQUEST_EXCESSIVE_LOAD == 0x9
    assert moqt.REQUEST_DOES_NOT_EXIST == 0x10
    assert moqt.REQUEST_INVALID_RANGE == 0x11
    assert moqt.REQUEST_MALFORMED_TRACK == 0x12
    assert moqt.REQUEST_UNINTERESTED == 0x20
    assert moqt.REQUEST_PREFIX_OVERLAP == 0x30
    assert moqt.REQUEST_NAMESPACE_TOO_LARGE == 0x31
    assert moqt.REQUEST_UNSUPPORTED_EXTENSION == 0x33
    assert moqt.REQUEST_REDIRECT == 0x34
    assert moqt.REQUEST_CONFLICTING_FILTERS == 0x35
    assert moqt.REQUEST_INVALID_FILTER == 0x36


def test_publish_done_and_stream_codes_are_drafted_values() -> None:
    """PUBLISH_DONE と stream reset のコードが draft の値と一致することを確認する。

    (draft-ietf-moq-transport-21 §16.11.3 (PUBLISH_DONE Codes) /
     §16.11.4 (Stream Reset Codes))
    """
    assert moqt.PUBLISH_DONE_INTERNAL_ERROR == 0x0
    assert moqt.PUBLISH_DONE_UNAUTHORIZED == 0x1
    assert moqt.PUBLISH_DONE_TRACK_ENDED == 0x2
    assert moqt.PUBLISH_DONE_GOING_AWAY == 0x4
    assert moqt.PUBLISH_DONE_TOO_FAR_BEHIND == 0x5
    assert moqt.PUBLISH_DONE_EXPIRED == 0x6
    assert moqt.PUBLISH_DONE_UPDATE_FAILED == 0x8
    assert moqt.PUBLISH_DONE_EXCESSIVE_LOAD == 0x9
    assert moqt.PUBLISH_DONE_MALFORMED_TRACK == 0x12

    assert moqt.STREAM_INTERNAL_ERROR == 0x0
    assert moqt.STREAM_CANCELLED == 0x1
    assert moqt.STREAM_DELIVERY_TIMEOUT == 0x2
    assert moqt.STREAM_SESSION_CLOSED == 0x3
    assert moqt.STREAM_GOING_AWAY == 0x4
    assert moqt.STREAM_TOO_FAR_BEHIND == 0x5
    assert moqt.STREAM_UNKNOWN_OBJECT_STATUS == 0x6
    assert moqt.STREAM_EXPIRED_AUTH_TOKEN == 0x7
    assert moqt.STREAM_EXCESSIVE_LOAD == 0x9
    assert moqt.STREAM_MALFORMED_TRACK == 0x12


def test_setup_option_types_are_drafted_values() -> None:
    """SETUP オプションの型番号が draft の値と一致することを確認する。

    (draft-ietf-moq-transport-21 §9.1 (SETUP))
    """
    assert moqt.SETUP_OPTION_PATH == 0x01
    assert moqt.SETUP_OPTION_AUTHORIZATION_TOKEN == 0x03
    assert moqt.SETUP_OPTION_MAX_AUTH_TOKEN_CACHE_SIZE == 0x04
    assert moqt.SETUP_OPTION_AUTHORITY == 0x05
    assert moqt.SETUP_OPTION_MAX_FILTER_RANGES == 0x06
    assert moqt.SETUP_OPTION_MOQT_IMPLEMENTATION == 0x07
    assert moqt.SETUP_OPTION_MAX_REQUEST_UPDATES == 0x08


# ─── パラメータのデコード ───────────────────────────────────


def test_decode_parameter_reads_varint_values() -> None:
    """偶数型のパラメータを varint としてデコードすることを確認する。

    (draft-ietf-moq-transport-21 §9.20.6 (SUBSCRIBER_PRIORITY Parameter))
    """
    assert moqt.decode_parameter(moqt.PARAM_SUBSCRIBER_PRIORITY, encode_varint(128)) == 128


def test_decode_parameter_reads_uint8_values() -> None:
    """uint8 で表現するパラメータを `int` としてデコードすることを確認する。

    FORWARD と GROUP_ORDER は uint8 である
    (draft-ietf-moq-transport-21 §9.20.7 (FORWARD Parameter))。
    """
    assert moqt.decode_parameter(moqt.PARAM_FORWARD, b"\x01") == 1


def test_decode_parameter_reads_a_location() -> None:
    """LARGEST_OBJECT を `(Group ID, Object ID)` としてデコードすることを確認する。

    (draft-ietf-moq-transport-21 §9.20.5 (LARGEST_OBJECT Parameter))
    """
    value = encode_varint(3) + encode_varint(7)

    assert moqt.decode_parameter(moqt.PARAM_LARGEST_OBJECT, value) == (3, 7)


def test_decode_parameter_reads_a_track_namespace_prefix() -> None:
    """TRACK_NAMESPACE_PREFIX を namespace のフィールド列としてデコードすることを確認する。

    (draft-ietf-moq-transport-21 §9.20.21 (TRACK_NAMESPACE_PREFIX Parameter))
    """
    value = b"\x01\x02ns"

    assert moqt.decode_parameter(moqt.PARAM_TRACK_NAMESPACE_PREFIX, value) == [b"ns"]


def test_decode_parameter_rejects_a_malformed_value() -> None:
    """型に合わない値のバイト列を拒否することを確認する。"""
    with pytest.raises(ValueError, match="unexpected end of buffer"):
        moqt.decode_parameter(moqt.PARAM_LOCATION_FILTER, b"\xff")


# ─── MessageParameters の型付きアクセサ ─────────────────────


def test_message_parameters_reads_typed_values() -> None:
    """
    パラメータを draft の意味付けで読めることを確認する。

    LARGEST_OBJECT は (Group ID, Object ID)、FORWARD と GROUP_ORDER は uint8、
    OBJECT_DELIVERY_TIMEOUT と SUBGROUP_DELIVERY_TIMEOUT はミリ秒の vi64 である
    (draft-ietf-moq-transport-21 §9.20 (Control Message Parameters))。
    """
    parameters = MessageParameters(
        {
            moqt.PARAM_LARGEST_OBJECT: b"\x03\x07",
            moqt.PARAM_FORWARD: b"\x01",
            moqt.PARAM_GROUP_ORDER: b"\x01",
            moqt.PARAM_SUBSCRIBER_PRIORITY: encode_varint(128),
            moqt.PARAM_OBJECT_DELIVERY_TIMEOUT: encode_varint(1500),
            moqt.PARAM_SUBGROUP_DELIVERY_TIMEOUT: encode_varint(2500),
        }
    )

    assert parameters.largest_object == (3, 7)
    assert parameters.forward == 1
    assert parameters.group_order == 1
    assert parameters.subscriber_priority == 128
    assert parameters.object_delivery_timeout == 1500
    assert parameters.subgroup_delivery_timeout == 2500
    assert len(parameters) == 6


def test_message_parameters_reports_absent_values_as_none() -> None:
    """パラメータを持たない集合では型付きアクセサが `None` を返すことを確認する。"""
    parameters = MessageParameters()

    assert parameters.largest_object is None
    assert parameters.forward is None
    assert parameters.expires is None
    assert parameters.has_expires is False
    assert parameters.group_order is None
    assert parameters.object_delivery_timeout is None
    assert parameters.subgroup_delivery_timeout is None
    assert parameters.location_filter_typed is None
    assert parameters.fill_parameters is None
    assert parameters.track_namespace_prefix is None
    assert len(parameters) == 0


def test_message_parameters_distinguishes_expires_zero() -> None:
    """
    EXPIRES=0 を「パラメータが無い」と区別できることを確認する。

    draft-ietf-moq-transport-21 §9.20.17 (EXPIRES Parameter): EXPIRES が 0 または
    不在なら subscription は expire しない。`expires` はどちらも `None` にするため、
    0 が指定されたことは `has_expires` で判定する。
    """
    parameters = MessageParameters({moqt.PARAM_EXPIRES: b"\x00"})

    assert parameters.has_expires is True
    assert parameters.expires is None


def test_message_parameters_round_trips_the_encoded_dictionary() -> None:
    """
    エンコード済みバイト列の辞書と相互に変換できることを確認する。

    辞書の形式は `Event.parameters` / `Message.parameters` が返すものと同じである。
    """
    encoded = {
        moqt.PARAM_LARGEST_OBJECT: b"\x03\x07",
        moqt.PARAM_FORWARD: b"\x01",
        moqt.PARAM_LOCATION_FILTER: b"\x02\x05\x09",
        moqt.PARAM_TRACK_NAMESPACE_PREFIX: b"\x02\x01a\x01b",
        moqt.PARAM_AUTHORIZATION_TOKEN: [b"\x05\x03\x01tok"],
    }

    parameters = MessageParameters(encoded)

    assert parameters.largest_object == (3, 7)
    assert parameters.forward == 1
    assert parameters.track_namespace_prefix == [b"a", b"b"]
    assert parameters.authorization_tokens() == [
        {"kind": "use_value", "token_type": 1, "token_value": b"tok"}
    ]
    assert parameters.to_dict() == encoded
    assert MessageParameters(parameters.to_dict()) == parameters


def test_message_parameters_accepts_typed_values() -> None:
    """型ごとの Python 表現からも構築できることを確認する。"""
    parameters = MessageParameters(
        {
            moqt.PARAM_LARGEST_OBJECT: (3, 7),
            moqt.PARAM_FILL_PARAMETERS: {moqt.PARAM_FILL_TIMEOUT: 1000},
            moqt.PARAM_AUTHORIZATION_TOKEN: [(1, b"tok")],
        }
    )

    assert parameters.largest_object == (3, 7)
    assert parameters.fill_parameters is not None
    assert parameters.fill_parameters.fill_timeout == 1000
    assert parameters.authorization_tokens() == [
        {"kind": "use_value", "token_type": 1, "token_value": b"tok"}
    ]
    # エンコード済みの辞書へ戻して構築し直しても同じ内容になる
    assert MessageParameters(parameters.to_dict()) == parameters


def test_message_parameters_rejects_raw_filter_bytes() -> None:
    """
    長さプレフィックスを持たないフィルタのバイト列を拒否することを確認する。

    `MessageParameters` が取る辞書の値は `Event.parameters` と同じエンコード済みの
    バイト列であり、`Session.send_subscribe` などが取る長さプレフィックスを含まない
    フィルタ本体とは異なる。取り違えを黙って別の値として解釈しないよう、長さが
    合わない値は拒否する (draft-ietf-moq-transport-21 §8.3 (Key-Value-Pair Structure))。
    """
    with pytest.raises(ValueError, match="requires an encoded value"):
        MessageParameters({moqt.PARAM_LOCATION_FILTER: LocationFilter("next_object").encode()})

    with pytest.raises(ValueError, match="requires an encoded value"):
        MessageParameters({moqt.PARAM_SUBGROUP_FILTER: b"\x00\x01\x64\x00"})


def test_message_parameters_rejects_an_unknown_parameter_type() -> None:
    """
    未知の型番号のパラメータを拒否することを確認する。

    draft-ietf-moq-transport-21 §9.20 (Control Message Parameters) は既知の型だけを
    定めるため、未知の型は PROTOCOL_VIOLATION になる。
    """
    with pytest.raises(ValueError, match="unknown message parameter type"):
        MessageParameters({0x3FFF: b"\x01"})


def test_message_parameters_reads_a_typed_location_filter() -> None:
    """
    LOCATION_FILTER を型付きで読み出せることを確認する。

    wire 形式は Length でフィールド数が決まる optional vi64 群である
    (draft-ietf-moq-transport-21 §9.20.10 (LOCATION FILTER Parameter))。
    """
    parameters = MessageParameters(
        {
            moqt.PARAM_LOCATION_FILTER: LocationFilter(
                "absolute_range", start_group=5, start_object=9, end_group_delta=2
            )
        }
    )

    assert parameters.location_filter_typed == LocationFilter(
        "absolute_range", start_group=5, start_object=9, end_group_delta=2
    )
    assert parameters.location_filter_typed is not None
    assert parameters.location_filter_typed.kind == "absolute_range"
    # `location_filter` は長さプレフィックスを含まないフィルタ本体を返す
    assert parameters.location_filter == b"\x05\x09\x02"
    assert LocationFilter.decode(parameters.location_filter) == parameters.location_filter_typed
    # エンコード済みの辞書の値は長さプレフィックスを含む
    assert parameters.to_dict()[moqt.PARAM_LOCATION_FILTER] == b"\x03\x05\x09\x02"


def test_message_parameters_reports_a_location_filter_update() -> None:
    """
    REQUEST_UPDATE の LOCATION_FILTER の 3 状態を区別できることを確認する。

    draft-ietf-moq-transport-21 §3.3.1 (Location Filters): Length 0 はフィルタの削除を
    表し、パラメータの省略 (値の変更なし) と区別する。
    """
    unchanged = MessageParameters()
    removed = MessageParameters({moqt.PARAM_LOCATION_FILTER: b"\x00"})
    replaced = MessageParameters({moqt.PARAM_LOCATION_FILTER: LocationFilter("next_object")})

    assert unchanged.location_filter_update.kind == "unchanged"
    assert unchanged.location_filter_update.filter is None
    assert removed.location_filter_update.kind == "removed"
    assert removed.location_filter_typed is None
    assert replaced.location_filter_update.kind == "set"
    assert replaced.location_filter_update.filter == LocationFilter("next_object")


def test_message_parameters_reads_nested_fill_parameters() -> None:
    """
    FILL_PARAMETERS の内側のパラメータを型付きで読めることを確認する。

    内側は外側とは別のパラメータスコープである
    (draft-ietf-moq-transport-21 §9.20.16 (FILL PARAMETERS Parameter))。
    """
    parameters = MessageParameters({moqt.PARAM_FILL_PARAMETERS: {moqt.PARAM_FILL_TIMEOUT: 1000}})

    assert parameters.fill_parameters is not None
    assert parameters.fill_parameters.fill_timeout == 1000
    assert parameters.fill_parameters.forward is None


def test_message_parameters_sets_largest_object() -> None:
    """LARGEST_OBJECT を設定でき、既存の値が置き換わることを確認する。"""
    parameters = MessageParameters()

    parameters.set_largest_object(3, 7)

    assert parameters.largest_object == (3, 7)
    assert len(parameters) == 1

    parameters.set_largest_object(4, 8)

    assert parameters.largest_object == (4, 8)
    assert len(parameters) == 1


@pytest.mark.parametrize(
    ("kind", "fields", "encoded"),
    [
        ("relative_group", {"start_group": 2}, b"\x02"),
        ("next_object", {}, b"\x00\x00"),
        ("absolute_start", {"start_group": 5, "start_object": 9}, b"\x05\x09"),
        (
            "absolute_range",
            {"start_group": 5, "start_object": 9, "end_group_delta": 2},
            b"\x05\x09\x02",
        ),
        (
            "absolute_range_with_end",
            {"start_group": 5, "start_object": 9, "end_group_delta": 2, "end_object": 4},
            b"\x05\x09\x02\x04",
        ),
    ],
    ids=[
        "relative-group",
        "next-object",
        "absolute-start",
        "absolute-range",
        "absolute-range-with-end",
    ],
)
def test_location_filter_round_trips_through_bytes(
    kind: str,
    fields: dict[str, int],
    encoded: bytes,
) -> None:
    """
    LOCATION_FILTER が wire 形式のバイト列と往復することを確認する。

    フィールド数が値の意味を決める
    (draft-ietf-moq-transport-21 §9.20.10 (LOCATION FILTER Parameter))。
    """
    location_filter = LocationFilter(kind, **fields)

    assert location_filter.kind == kind
    assert location_filter.encode() == encoded
    assert LocationFilter.decode(encoded) == location_filter


def test_location_filter_reports_its_fields() -> None:
    """LOCATION_FILTER が種別ごとのフィールドを返すことを確認する。"""
    location_filter = LocationFilter(
        "absolute_range_with_end", start_group=5, start_object=9, end_group_delta=2, end_object=4
    )

    assert location_filter.start_group == 5
    assert location_filter.start_object == 9
    assert location_filter.end_group_delta == 2
    assert location_filter.end_object == 4

    # 種別が持たないフィールドは `None` になる
    relative = LocationFilter("relative_group", start_group=2)

    assert relative.start_group == 2
    assert relative.start_object is None
    assert relative.end_group_delta is None
    assert relative.end_object is None


def test_location_filter_normalizes_an_absolute_start_of_zero() -> None:
    """
    Start が両方 0 の absolute_start が next_object として読み直されることを確認する。

    wire 上は 2 フィールドの 0,0 であり区別できない
    (draft-ietf-moq-transport-21 §9.20.10 (LOCATION FILTER Parameter))。
    """
    location_filter = LocationFilter("absolute_start", start_group=0, start_object=0)

    assert LocationFilter.decode(location_filter.encode()) == LocationFilter("next_object")


@pytest.mark.parametrize(
    ("kind", "fields", "expected"),
    [
        ("relative_group", {}, "requires start_group"),
        ("relative_group", {"start_group": 1, "start_object": 2}, "does not take start_object"),
        ("next_object", {"start_group": 1}, "does not take start_group"),
        ("absolute_start", {"start_group": 1}, "requires start_object"),
        ("absolute_range", {"start_group": 1, "start_object": 2}, "requires end_group_delta"),
        (
            "absolute_range_with_end",
            {"start_group": 1, "start_object": 2, "end_group_delta": 3},
            "requires end_object",
        ),
        ("bogus", {}, "unknown LOCATION_FILTER kind"),
    ],
    ids=[
        "relative-group-without-start-group",
        "relative-group-with-start-object",
        "next-object-with-start-group",
        "absolute-start-without-start-object",
        "absolute-range-without-end-group-delta",
        "absolute-range-with-end-without-end-object",
        "unknown-kind",
    ],
)
def test_location_filter_rejects_mismatched_fields(
    kind: str,
    fields: dict[str, int],
    expected: str,
) -> None:
    """種別とフィールドの組み合わせが合わない LOCATION_FILTER を拒否することを確認する。"""
    with pytest.raises(ValueError, match=expected):
        LocationFilter(kind, **fields)


def test_location_filter_rejects_a_malformed_value() -> None:
    """
    フィールド数が 5 以上のバイト列を拒否することを確認する。

    (draft-ietf-moq-transport-21 §9.20.10 (LOCATION FILTER Parameter))
    """
    with pytest.raises(ValueError, match="more than 4 fields"):
        LocationFilter.decode(b"\x01\x01\x01\x01\x01")


# ─── セッションの状態 ───────────────────────────────────────


def test_session_reports_role_and_state() -> None:
    """Session が role と状態を報告することを確認する。"""
    client = Session.client("c")
    server = Session.server("s")

    assert client.role() == "client"
    assert server.role() == "server"
    assert client.established is False

    client_setup = client.start()
    server_setup = server.start()
    server.receive_control(client_setup)

    # 自側の SETUP を送っただけでは確立しない
    assert client.state() == "local_setup_sent"

    client.receive_control(server_setup)

    assert client.established is True
    assert client.state() == "established"


def test_session_reports_the_last_error() -> None:
    """`last_error` が初期状態では空であることを確認する。

    状態機械がセッションを閉じる理由を通知したときにだけ値が入る診断用の値である。
    """
    client, _server = _setup()

    assert client.last_error is None


def test_next_local_request_id_requires_an_established_session() -> None:
    """SETUP が終わるまで `next_local_request_id` が失敗することを確認する。"""
    client = Session.client("c")

    with pytest.raises(RuntimeError, match="established"):
        client.next_local_request_id()


def test_next_local_request_id_advances() -> None:
    """`next_local_request_id` が request の送信で進むことを確認する。"""
    client, _server = _setup()
    first = client.next_local_request_id()
    client.send_subscribe([b"ns"], b"t", {})

    assert client.next_local_request_id() > first


def test_subscription_cleanup_ready_is_false_while_established() -> None:
    """購読が確立している間は cleanup できないことを確認する。

    cleanup できるのは Terminated 状態になった後である
    (draft-ietf-moq-transport-21 §3.1.1 (Subscription State Management))。
    """
    client, server = _setup()
    request_id = _subscribe_round_trip(client, server, 4)

    assert client.subscription_cleanup_ready(request_id) is False
    assert client.subscription_cleanup_ready(9999) is None


# ─── GREASE ─────────────────────────────────────────────────


def test_grease_constants_are_drafted_values() -> None:
    """GREASE の定数が draft の値と一致することを確認する。

    (draft-ietf-moq-transport-21 §13 (Grease))
    """
    assert moqt.GREASE_BASE == 0x9D
    assert moqt.GREASE_INTERVAL == 0x7F
    assert moqt.GREASE_MAX == 0x3FFF_FFFF_FFFF_FFDE


def test_grease_generate_uses_the_drafted_formula() -> None:
    """`generate` が乱数源から `0x7F * N + 0x9D` を作ることを確認する。

    乱数源は `stop` を受け取る呼び出し可能オブジェクトである。渡した N が
    そのまま使われることを、決定的な乱数源で確認する。
    """
    assert moqt.generate(lambda _stop: 0) == moqt.GREASE_BASE
    assert moqt.generate(lambda _stop: 1) == moqt.GREASE_INTERVAL + moqt.GREASE_BASE
    assert moqt.generate(lambda _stop: 42) == moqt.GREASE_INTERVAL * 42 + moqt.GREASE_BASE


def test_grease_generate_requests_the_whole_representable_range() -> None:
    """`generate` が乱数源へ渡す上限を確認する。

    `GREASE_MAX` を超えない最大の N は `(GREASE_MAX - GREASE_BASE) // GREASE_INTERVAL`
    である。`stop` は開区間の上限なので 1 を足した値が渡る。
    """
    drawn: list[int] = []

    def source(stop: int) -> int:
        # 乱数源が受け取った上限を記録してから 0 を返す
        drawn.append(stop)
        return 0

    moqt.generate(source)

    assert drawn == [(moqt.GREASE_MAX - moqt.GREASE_BASE) // moqt.GREASE_INTERVAL + 1]


def test_grease_generate_uses_the_largest_sequence() -> None:
    """乱数源が上限いっぱいの N を返したときに `GREASE_MAX` ちょうどが生成されることを確認する。

    `GREASE_MAX` 自身が値の並びに乗るため、これが生成できる最大の GREASE 値になる。
    """
    upper = (moqt.GREASE_MAX - moqt.GREASE_BASE) // moqt.GREASE_INTERVAL
    value = moqt.generate(lambda stop: stop - 1)

    assert value == moqt.GREASE_INTERVAL * upper + moqt.GREASE_BASE
    assert value == moqt.GREASE_MAX
    assert moqt.is_grease(value) is True


def test_grease_is_grease_accepts_generated_values() -> None:
    """`is_grease` が `generate` の作った値を GREASE と判定することを確認する。"""
    # N を順に変えて、いずれも GREASE と判定されることを確認する
    for n in [0, 1, 2, 41, 100, (moqt.GREASE_MAX - moqt.GREASE_BASE) // moqt.GREASE_INTERVAL]:
        assert moqt.is_grease(moqt.generate(lambda _stop, n=n: n)) is True


def test_grease_is_grease_accepts_a_pattern_match_above_the_upper_bound() -> None:
    """上限を超えても値の並びに合致すれば GREASE と判定されることを確認する。

    受信側は将来 draft の上限が広がった場合にも未知値として無視できる必要がある。
    """
    assert moqt.is_grease(moqt.GREASE_MAX + moqt.GREASE_INTERVAL) is True


def test_grease_is_grease_rejects_values_outside_the_pattern() -> None:
    """値の並びから外れた値を GREASE と判定しないことを確認する。"""
    # 基数より小さい値は GREASE になり得ない
    assert moqt.is_grease(0) is False
    assert moqt.is_grease(moqt.GREASE_BASE - 1) is False
    # 間隔から 1 だけずれた値も GREASE ではない
    assert moqt.is_grease(moqt.GREASE_BASE + 1) is False
    assert moqt.is_grease(moqt.GREASE_BASE + moqt.GREASE_INTERVAL - 1) is False
    assert moqt.is_grease(moqt.GREASE_MAX - 1) is False


def test_grease_generate_rejects_an_out_of_range_sequence() -> None:
    """範囲外の N を返す乱数源を `generate` が拒否することを確認する。

    乱数源の契約は `[0, stop)` の整数を返すことである。契約を破る値を黙って
    丸めずエラーにする。
    """
    with pytest.raises(ValueError, match="exceeds"):
        moqt.generate(lambda stop: stop)


def test_grease_generate_rejects_a_non_integer_sequence() -> None:
    """整数として読めない値を返す乱数源を `generate` が拒否することを確認する。

    `moqt.moqt.generate` の型は `(int) -> int` の呼び出し可能オブジェクトを要求するため、
    契約違反の乱数源は拡張モジュールの関数へ直接渡して検査する。
    """
    with pytest.raises(ValueError, match="must return an int"):
        _native.generate(lambda _stop: "0")


def test_grease_generate_uses_the_standard_random_by_default() -> None:
    """乱数源を省略した場合は標準ライブラリの乱数で生成することを確認する。"""
    values = {moqt.generate() for _ in range(8)}

    assert all(moqt.is_grease(value) for value in values)
    assert all(value <= moqt.GREASE_MAX for value in values)
    # 毎回同じ値では乱数源として機能していない
    assert len(values) > 1


# ─── セッション状態の照会 ───────────────────────────────────


def test_received_datagram_reports_the_publisher_priority() -> None:
    """受信したデータグラムの Publisher Priority がイベントに載ることを確認する。

    DEFAULT_PRIORITY bit が立っていないデータグラムは優先度を明示しており、その値が
    イベントに入る。bit が立っているデータグラムは購読を確立した制御メッセージの
    優先度を継承するため `None` になる
    (draft-ietf-moq-transport-21 §11.2.1 (Object Datagram))。
    """
    client, server = _setup()
    _subscribe_round_trip(client, server, 4)

    # 優先度 10 を明示したデータグラム
    events = client.receive_datagram(_object_datagram(1, 1, 0, b"explicit", 10))
    objects = [event for event in events if event.kind == "object"]
    assert len(objects) == 1
    assert objects[0].publisher_priority == 10
    assert objects[0].group_id == 1
    assert objects[0].object_id == 0
    assert objects[0].track_alias == 1
    # データグラムは subgroup ヘッダを持たない
    assert objects[0].subgroup_id is None
    assert objects[0].stream_id is None

    # DEFAULT_PRIORITY bit が立っているデータグラム
    events = client.receive_datagram(_object_datagram(1, 1, 1, b"default", None))
    objects = [event for event in events if event.kind == "object"]
    assert len(objects) == 1
    assert objects[0].publisher_priority is None


def test_session_state_accessors_report_peer_declared_values() -> None:
    """peer が宣言した値とセッションのタイムアウト設定を照会できることを確認する。

    SETUP で受け取った値はキャッシュせず、状態機械から都度取得する
    (draft-ietf-moq-transport-21 §9.1.3 (MAX_AUTH_TOKEN_CACHE_SIZE))。
    """
    client = Session.client("c", {moqt.SETUP_OPTION_MAX_AUTH_TOKEN_CACHE_SIZE: 4096})
    server = Session.server("s")
    client_setup = client.start()
    server_setup = server.start()
    server.receive_control(client_setup)
    client.receive_control(server_setup)

    # client から見た peer の宣言値。server は宣言していないため 0 になる
    assert client.peer_max_auth_token_cache_size == 0
    # server から見た peer の宣言値
    assert server.peer_max_auth_token_cache_size == 4096

    # 期限は既定では無効であり、設定した値がそのまま読める
    assert client.control_message_timeout_ms is None
    assert client.data_stream_timeout_ms is None
    client.set_control_message_timeout_ms(1500)
    client.set_data_stream_timeout_ms(2500)
    assert client.control_message_timeout_ms == 1500
    assert client.data_stream_timeout_ms == 2500

    # alias の保持期間は既定値を持ち、変更した値がそのまま読める
    assert client.peer_alias_retention_ms == moqt.DEFAULT_PEER_ALIAS_RETENTION_MS
    client.set_peer_alias_retention_ms(1000)
    assert client.peer_alias_retention_ms == 1000


def test_subscription_state_accessors_report_the_subscription() -> None:
    """subscription の状態を Request ID から照会できることを確認する。

    一覧は Request ID をキーにした辞書であり、保持していない Request ID は `None` に
    なる。publisher 側と subscriber 側で `my_role` が入れ替わる。
    """
    client, server = _setup()
    request_id = _subscribe_round_trip(client, server, 4)

    entry = client.subscription(request_id)
    assert entry is not None
    assert entry["request_id"] == request_id
    assert entry["track_alias"] == 1
    assert entry["namespace"] == [b"ns"]
    assert entry["track_name"] == b"t"
    assert entry["state"] == "established"
    assert entry["my_role"] == "subscriber"
    assert entry["initiator"] == "subscriber"
    # FORWARD パラメータを省略した場合は送る側の既定値 1 になる
    # (draft-ietf-moq-transport-21 §9.20.19 (FORWARD Parameter))
    assert entry["forward"] is True
    assert entry["subscriber_priority"] is None
    assert entry["group_order"] is None
    assert entry["largest_location"] is None
    assert entry["largest_received_location"] is None

    # 一覧は Request ID をキーにして同じ内容を返す
    assert client.subscriptions() == {request_id: entry}
    # 保持していない Request ID は取得できない
    assert client.subscription(request_id + 100) is None

    # 同じ subscription が publisher 側からは publisher として見える
    publisher_entry = server.subscription(request_id)
    assert publisher_entry is not None
    assert publisher_entry["my_role"] == "publisher"
    assert publisher_entry["initiator"] == "subscriber"


def test_subscription_state_accessor_reports_a_pending_subscription() -> None:
    """SUBSCRIBE_OK を受信する前の subscription が Pending として見えることを確認する。"""
    client, server = _setup()
    events = client.send_subscribe([b"ns"], b"t", {})
    request_id = _request_id(events[0])
    client.register_local_request_stream(4, request_id)

    entry = client.subscription(request_id)
    assert entry is not None
    assert entry["state"] == "pending"
    # alias は SUBSCRIBE_OK を受信するまで確定しない
    assert entry["track_alias"] is None

    # SUBSCRIBE_OK を受信すると Established になり alias が入る
    server.receive_request_stream(4, _message_data(events[0]), "peer")
    ok = server.send_subscribe_ok(request_id, 7, {}, {})
    client.receive_request_stream(4, _message_data(ok[0]), "local")

    established = client.subscription(request_id)
    assert established is not None
    assert established["state"] == "established"
    assert established["track_alias"] == 7


def test_request_ok_event_reports_the_response_metadata() -> None:
    """
    応答イベントが SUBSCRIBE_OK のパラメータと Track Properties を運ぶことを確認する。

    状態機械のイベントは応答のパラメータしか運ばないため、Track Properties は受信した
    生バイト列から取り出す。パラメータは型番号をキーにしたエンコード済みバイト列の
    辞書であり、Track Properties は偶数型が `int`、奇数型が `bytes` の辞書である
    (draft-ietf-moq-transport-21 §9.20 (Control Message Parameters) /
    §8.4 (Track and Object Properties))。
    """
    # 購読を確立し、SUBSCRIBE_OK にパラメータと Track Properties を載せる
    client, server = _setup()
    subscribe_events = client.send_subscribe([b"ns"], b"t", {})
    request_id = _request_id(subscribe_events[0])
    client.register_local_request_stream(4, request_id)
    server.receive_request_stream(4, _message_data(subscribe_events[0]), "peer")
    ok = server.send_subscribe_ok(
        request_id,
        1,
        {moqt.PARAM_LARGEST_OBJECT: (3, 4)},
        {moqt.PROP_DEFAULT_PUBLISHER_PRIORITY: 200, 0x0D: b"\x01\x02"},
    )

    # 応答は request_ok イベントとして届く
    events = client.receive_request_stream(4, _message_data(ok[0]), "local")
    accepted = [event for event in events if event.kind == "request_ok"]

    assert len(accepted) == 1
    # LARGEST_OBJECT は Group ID と Object ID の 2 つの vi64 である
    # (draft-ietf-moq-transport-21 §9.20.9 (LARGEST_OBJECT Parameter))
    assert accepted[0].parameters is not None
    assert accepted[0].parameters[moqt.PARAM_LARGEST_OBJECT] == encode_varint(3) + encode_varint(4)
    assert accepted[0].track_properties == {
        moqt.PROP_DEFAULT_PUBLISHER_PRIORITY: 200,
        0x0D: b"\x01\x02",
    }


def test_request_ok_event_without_track_properties_reports_an_empty_dict() -> None:
    """
    Track Properties を運ばない応答ではイベントの値が空の辞書になることを確認する。

    REQUEST_OK が Track Properties を運べるのは TRACK_STATUS への応答だけであり、
    TRACK_STATUS の受信側を状態機械は扱わない。ここでは購読の REQUEST_UPDATE_OK を
    使って、Track Properties を運ばない応答を確かめる
    (draft-ietf-moq-transport-21 §9.3 (REQUEST_OK) / §9.5 (REQUEST_UPDATE))。
    """
    client, server = _setup()
    subscribe_events = client.send_subscribe([b"ns"], b"t", {})
    subscription_request_id = _request_id(subscribe_events[0])
    client.register_local_request_stream(4, subscription_request_id)
    server.receive_request_stream(4, _message_data(subscribe_events[0]), "peer")
    ok = server.send_subscribe_ok(subscription_request_id, 1, {}, {})
    client.receive_request_stream(4, _message_data(ok[0]), "local")

    # REQUEST_UPDATE への応答は同じ request stream で届く
    update_events = client.send_request_update(subscription_request_id, {})
    server.receive_request_stream(4, _message_data(update_events[0]), "peer")
    response = server.send_request_ok(subscription_request_id, {moqt.PARAM_EXPIRES: 1000}, {})

    received = client.receive_request_stream(4, _message_data(response[0]), "local")
    accepted = [event for event in received if event.kind == "request_ok"]

    assert len(accepted) == 1
    # 応答が Track Properties を運ばない場合は空の辞書であり、`None` ではない
    assert accepted[0].track_properties == {}


def test_fetch_state_accessors_report_the_fetch() -> None:
    """fetch の状態を Request ID から照会できることを確認する。"""
    client, server = _setup()
    request_id = _fetch_round_trip(client, server, 4)

    entry = client.fetch(request_id)
    assert entry is not None
    assert entry["request_id"] == request_id
    assert entry["state"] == "established"
    assert entry["my_role"] == "subscriber"
    assert entry["namespace"] == [b"fetch-ns"]
    assert entry["track_name"] == b"fetch-track"
    # FETCH_OK で確定した終端情報が入る
    # (draft-ietf-moq-transport-21 §9.12 (FETCH_OK))
    assert entry["end_location"] == (0, 0)
    assert entry["end_of_track"] is False
    assert entry["response_received"] is True

    assert client.fetches() == {request_id: entry}
    assert client.fetch(request_id + 100) is None

    # publisher 側からは publisher として見える
    publisher_entry = server.fetch(request_id)
    assert publisher_entry is not None
    assert publisher_entry["my_role"] == "publisher"


def test_track_status_state_accessors_report_the_response() -> None:
    """TRACK_STATUS の状態を Request ID から照会できることを確認する。

    TRACK_STATUS は relay が応答する request であり、moqt-rs は送信側だけを扱う
    (draft-ietf-moq-transport-21 §9.13 (TRACK_STATUS))。応答が届かないまま
    request stream が終端すると、状態機械は応答をエラーとして記録する。
    """
    client, _server = _setup()
    events = client.send_track_status([b"ns"], b"t", {})
    request_id = _request_id(events[0])
    client.register_local_request_stream(4, request_id)

    entry = client.track_status_request(request_id)
    assert entry is not None
    assert entry["request_id"] == request_id
    assert entry["namespace"] == [b"ns"]
    assert entry["track_name"] == b"t"
    # 応答が届くまでは pending である
    assert entry["response"] == "pending"
    assert entry["largest_location"] is None

    assert client.track_status_requests() == {request_id: entry}
    assert client.track_status_request(request_id + 100) is None

    # 応答前に request stream が終端するとエラーとして記録される
    client.receive_request_stream_closed(4, False, None)
    closed = client.track_status_request(request_id)
    assert closed is not None
    assert closed["response"] == "error"
    assert closed["largest_location"] is None


def test_track_status_state_accessor_reports_an_ok_response() -> None:
    """TRACK_STATUS の応答が届いた場合に ok と LARGEST_OBJECT が読めることを確認する。

    状態機械は TRACK_STATUS の受信側を扱わないため、relay が返す REQUEST_OK を
    このテストでは再現できない。そこで購読の REQUEST_UPDATE_OK として同じ codec に
    REQUEST_OK を生成させ、TRACK_STATUS を登録した request stream へ流し込む。
    REQUEST_OK は wire 上に Request ID を持たず、受信側はストリームに紐付けた
    Request ID で応答先を決めるため、同じバイト列が TRACK_STATUS の応答として解釈される
    (draft-ietf-moq-transport-21 §9.3 (REQUEST_OK))。
    """
    client, server = _setup()
    status_events = client.send_track_status([b"ns"], b"t", {})
    status_request_id = _request_id(status_events[0])
    client.register_local_request_stream(4, status_request_id)

    # 別の request stream で購読を確立し、REQUEST_UPDATE を送る
    subscribe_events = client.send_subscribe([b"ns"], b"t", {})
    subscription_request_id = _request_id(subscribe_events[0])
    client.register_local_request_stream(8, subscription_request_id)
    server.receive_request_stream(8, _message_data(subscribe_events[0]), "peer")
    accepted = server.send_subscribe_ok(subscription_request_id, 1, {}, {})
    client.receive_request_stream(8, _message_data(accepted[0]), "local")
    update_events = client.send_request_update(
        subscription_request_id, {moqt.PARAM_SUBSCRIBER_PRIORITY: 100}
    )
    server.receive_request_stream(8, _message_data(update_events[0]), "peer")

    # REQUEST_UPDATE_OK は LARGEST_OBJECT を運べる
    # (draft-ietf-moq-transport-21 §9.20.18 (LARGEST OBJECT Parameter))
    ok = server.send_request_ok(
        subscription_request_id,
        {moqt.PARAM_LARGEST_OBJECT: (3, 4)},
        {},
    )

    # 同じ REQUEST_OK を TRACK_STATUS の応答として流し込む
    client.receive_request_stream(4, _message_data(ok[0]), "local")

    entry = client.track_status_request(status_request_id)
    assert entry is not None
    assert entry["response"] == "ok"
    assert entry["largest_location"] == (3, 4)


def test_terminated_subscription_is_forgotten_only_after_cleanup_is_ready() -> None:
    """
    終了した subscription を cleanup 可能になってから回収することを確認する。

    購読が終了しても同じ Request ID への参照が残っている可能性があるため、回収は
    `subscription_cleanup_ready` が真を返したときだけ行う。終了前に `forget_subscription`
    を呼んでも状態機械は変化しない
    (draft-ietf-moq-transport-21 §3.1.1 (Subscription State Management))。
    """
    client, server = _setup()
    request_id = _subscribe_round_trip(client, server, 4)

    # 確立中の subscription は回収できない
    assert client.subscription_cleanup_ready(request_id) is False
    assert client.forget_subscription(request_id) is False
    assert client.subscription(request_id) is not None
    # 保持していない Request ID は照会も回収もできない
    assert client.subscription_cleanup_ready(9999) is None
    assert client.forget_subscription(9999) is False

    # 購読を終了すると回収できるようになる
    client.stop_sending(request_id)
    assert client.subscription_cleanup_ready(request_id) is True
    assert client.forget_subscription(request_id) is True
    assert client.subscription(request_id) is None
    # 回収後の照会と二重の回収は安全である
    assert client.subscription_cleanup_ready(request_id) is None
    assert client.forget_subscription(request_id) is False


def test_terminated_fetch_is_forgotten_only_after_cleanup_is_ready() -> None:
    """
    終了した fetch を cleanup 可能になってから回収することを確認する。

    fetch は subscriber 側の cancel で `Terminated` になり、受信中のデータストリームが
    無くなった時点で回収できる。終了前に `forget_fetch` を呼んでも状態機械は変化しない
    (draft-ietf-moq-transport-21 §3.2.1 (Fetch State Management))。
    """
    client, server = _setup()
    request_id = _fetch_round_trip(client, server, 4)

    # 確立中の fetch は回収できない
    assert client.fetch_cleanup_ready(request_id) is False
    assert client.forget_fetch(request_id) is False
    assert client.fetch(request_id) is not None
    assert client.fetch_cleanup_ready(9999) is None
    assert client.forget_fetch(9999) is False

    # cancel すると回収できるようになる
    client.send_fetch_stop_sending(request_id)
    assert client.fetch_cleanup_ready(request_id) is True
    assert client.forget_fetch(request_id) is True
    assert client.fetch(request_id) is None
    # 回収後の照会と二重の回収は安全である
    assert client.fetch_cleanup_ready(request_id) is None
    assert client.forget_fetch(request_id) is False


def test_track_status_is_forgotten_only_after_the_response() -> None:
    """
    応答済みの TRACK_STATUS だけを回収できることを確認する。

    状態機械は TRACK_STATUS の受信側を扱わないため、relay が返す REQUEST_OK を
    購読の REQUEST_UPDATE_OK として生成し、TRACK_STATUS の request stream へ流し込む。
    応答が載る bidi request stream の終端を通知した後は request stream の対応が消えるため
    回収できない (draft-ietf-moq-transport-21 §9.13 (TRACK_STATUS))。
    """
    client, server = _setup()
    status_events = client.send_track_status([b"ns"], b"t", {})
    status_request_id = _request_id(status_events[0])
    client.register_local_request_stream(4, status_request_id)

    # 応答前の TRACK_STATUS は回収できない
    assert client.forget_track_status(status_request_id) is False
    assert client.track_status_request(status_request_id) is not None
    # 保持していない Request ID も回収できない
    assert client.forget_track_status(9999) is False

    # 購読の REQUEST_UPDATE_OK と同じ REQUEST_OK を応答として流し込む
    subscribe_events = client.send_subscribe([b"ns"], b"t", {})
    subscription_request_id = _request_id(subscribe_events[0])
    client.register_local_request_stream(8, subscription_request_id)
    server.receive_request_stream(8, _message_data(subscribe_events[0]), "peer")
    accepted = server.send_subscribe_ok(subscription_request_id, 1, {}, {})
    client.receive_request_stream(8, _message_data(accepted[0]), "local")
    update_events = client.send_request_update(subscription_request_id, {})
    server.receive_request_stream(8, _message_data(update_events[0]), "peer")
    ok = server.send_request_ok(subscription_request_id, {}, {})
    client.receive_request_stream(4, _message_data(ok[0]), "local")

    # 応答後は回収できる
    assert client.forget_track_status(status_request_id) is True
    assert client.track_status_request(status_request_id) is None
    # 回収後の二重の回収は安全である
    assert client.forget_track_status(status_request_id) is False


def test_track_status_is_forgotten_after_the_stream_ended_without_a_response() -> None:
    """
    応答前に request stream が終端した TRACK_STATUS を回収できることを確認する。

    応答が届かないまま終端した場合は REQUEST_ERROR として記録されるため、状態機械は
    回収できる。回収後は Request ID の照会も二重の回収も安全である
    (draft-ietf-moq-transport-21 §9.13 (TRACK_STATUS))。
    """
    client, _server = _setup()
    events = client.send_track_status([b"ns"], b"t", {})
    request_id = _request_id(events[0])
    client.register_local_request_stream(4, request_id)

    # 終端通知の後はエラー応答として記録され、回収できる
    client.receive_request_stream_closed(4, False, None)
    assert client.forget_track_status(request_id) is True
    assert client.track_status_request(request_id) is None
    assert client.forget_track_status(request_id) is False


def test_goaway_drain_accessors_report_blocking_requests() -> None:
    """GOAWAY の drain を妨げている request を照会できることを確認する。

    購読が cleanup 可能になるまで drain は完了せず、妨げている Request ID が
    snapshot に入る (draft-ietf-moq-transport-21 §6.6.1 (Graceful Session Migration))。
    """
    client, server = _setup()
    subscription_request_id = _subscribe_round_trip(client, server, 4)
    fetch_request_id = _fetch_round_trip(client, server, 8)
    status_events = client.send_track_status([b"ns"], b"t", {})
    status_request_id = _request_id(status_events[0])
    client.register_local_request_stream(12, status_request_id)

    assert client.goaway_drain_ready() is False
    snapshot = client.goaway_drain_snapshot()
    assert snapshot["blocking_subscription_request_ids"] == [subscription_request_id]
    assert snapshot["blocking_fetch_request_ids"] == [fetch_request_id]
    assert snapshot["blocking_track_status_request_ids"] == [status_request_id]

    # 応答前に request stream が終端した TRACK_STATUS は drain を妨げない
    client.receive_request_stream_closed(12, False, None)
    assert client.goaway_drain_snapshot()["blocking_track_status_request_ids"] == []

    # 購読を終了して破棄すると、その購読は drain を妨げなくなる
    client.stop_sending(subscription_request_id)
    assert client.forget_subscription(subscription_request_id) is True
    assert client.goaway_drain_snapshot()["blocking_subscription_request_ids"] == []

    assert client.goaway_drain_ready() is False
    # fetch を cancel して破棄すると drain が完了する
    client.send_fetch_stop_sending(fetch_request_id)
    assert client.forget_fetch(fetch_request_id) is True
    assert client.goaway_drain_ready() is True
    assert client.goaway_drain_snapshot() == {
        "blocking_subscription_request_ids": [],
        "blocking_fetch_request_ids": [],
        "blocking_track_status_request_ids": [],
    }


def test_open_outgoing_fill_stream_count_reports_open_streams() -> None:
    """open 中の送信 fill fetch stream 数を照会できることを確認する。

    fill fetch stream は publisher が開き、1 つの subscription に複数本が同時に
    開くことがある。stream を終端すると索引から外れる
    (draft-ietf-moq-transport-21 §3.4 (Fill Semantics))。
    """
    client, server = _setup()
    request_id = _subscribe_round_trip(client, server, 4)
    # fill fetch stream を開いていない状態では 0 本である
    assert server.open_outgoing_fill_stream_count(request_id) == 0
    assert server.open_outgoing_fill_stream_count(request_id + 100) == 0
    # subscriber 側は fill fetch stream を開かない
    assert client.open_outgoing_fill_stream_count(request_id) == 0

    # publisher が fill fetch stream を開くと本数が増える
    server.send_fill_fetch_header(12, request_id)
    assert server.open_outgoing_fill_stream_count(request_id) == 1

    # 終端した stream は open 中の本数に数えない
    server.send_fetch_data_stream_closed(12)
    assert server.open_outgoing_fill_stream_count(request_id) == 0


def test_session_state_accessors_do_not_change_the_session() -> None:
    """照会を繰り返してもセッションの状態が変化しないことを確認する。

    getter は状態機械の読み出しだけを行い、イベントを生成しない。照会の後も
    新しい request を往復できることで、状態機械が壊れていないことを確かめる。
    """
    client, server = _setup()
    request_id = _subscribe_round_trip(client, server, 4)

    def snapshot() -> tuple[object, ...]:
        """照会系 API が返す値をすべて集める。"""
        return (
            client.state(),
            client.established,
            client.last_error,
            client.peer_max_auth_token_cache_size,
            client.peer_alias_retention_ms,
            client.control_message_timeout_ms,
            client.data_stream_timeout_ms,
            client.goaway_drain_ready(),
            client.goaway_drain_snapshot(),
            client.subscriptions(),
            client.fetches(),
            client.track_status_requests(),
            client.open_outgoing_fill_stream_count(request_id),
            client.subscription(request_id),
        )

    # 2 回目以降の照会でも同じ内容が返る
    assert snapshot() == snapshot()

    # 照会の後も状態機械は request を往復できる
    second_request_id = _subscribe_round_trip(client, server, 8, track_alias=2)
    assert second_request_id != request_id
    assert set(client.subscriptions()) == {request_id, second_request_id}
    assert client.state() == "established"


# ─── セッションの既定値 ─────────────────────────────────────


def test_session_defaults_are_exported() -> None:
    """セッションの既定値が `moqt.moqt` から参照できることを確認する。

    (draft-ietf-moq-transport-21 §10.5 (DEFAULT PUBLISHER GROUP ORDER) /
     §3.1.2 (Track Alias) / §9.2 (GOAWAY) / §9.9 (PUBLISH_DONE))
    """
    assert moqt.DEFAULT_PUBLISHER_GROUP_ORDER_ASCENDING == 0x1
    assert moqt.MAX_NEW_SESSION_URI_LENGTH == 8192
    assert moqt.MAX_OUT_OF_ORDER_REQUEST_IDS == 1024
    assert moqt.DEFAULT_PEER_ALIAS_RETENTION_MS > 0
    assert moqt.PUBLISH_DONE_STREAM_COUNT_UNKNOWN == 0xFFFF_FFFF_FFFF_FFFF


def test_goaway_accepts_a_new_session_uri_at_the_length_limit() -> None:
    """`MAX_NEW_SESSION_URI_LENGTH` ちょうどの URI を GOAWAY が受け付けることを確認する。

    (draft-ietf-moq-transport-21 §9.2 (GOAWAY))
    """
    client, server = _setup()
    uri = b"a" * moqt.MAX_NEW_SESSION_URI_LENGTH

    events = server.send_goaway(uri, 5000)

    assert [event.kind for event in events] == ["send_control"]
    kinds = [event.kind for event in client.receive_control(_event_data(events[0]))]
    assert "goaway" in kinds


def test_goaway_rejects_a_new_session_uri_beyond_the_length_limit() -> None:
    """`MAX_NEW_SESSION_URI_LENGTH` を超える URI を GOAWAY が拒否することを確認する。

    (draft-ietf-moq-transport-21 §9.2 (GOAWAY))
    """
    _client, server = _setup()
    uri = b"a" * (moqt.MAX_NEW_SESSION_URI_LENGTH + 1)

    with pytest.raises(RuntimeError, match="new_session_uri exceeds"):
        server.send_goaway(uri, 5000)


# ─── Subgroup ID のエンコードモード ─────────────────────────


def _received_subgroup_stream(
    first_object_id: int,
    payload: bytes,
    *,
    subgroup_id_mode: int,
    subgroup_id: int | None = None,
) -> bytes:
    """受信側へ流し込む subgroup ストリームのバイト列を組み立てる。

    Type Flags は PROPERTIES bit (0x01)、SUBGROUP_ID_MODE (bits 1-2、mask 0x06)、
    END_OF_GROUP bit (0x08)、bit 4 (0x10、必須)、DEFAULT_PRIORITY bit (0x20)、
    FIRST_OBJECT bit (0x40) から成る
    (draft-ietf-moq-transport-21 §11.3.1 (Subgroup Header))。
    """
    # bit 4 は常に 1 であり、Publisher Priority を省略するため DEFAULT_PRIORITY bit を立てる
    type_byte = 0x10 | 0x20 | subgroup_id_mode
    data = bytearray()
    data += encode_varint(type_byte)
    data += encode_varint(1)
    data += encode_varint(7)
    if subgroup_id is not None:
        data += encode_varint(subgroup_id)
    # 最初の Object の Object ID Delta は Object ID そのものである
    data += encode_varint(first_object_id)
    data += encode_varint(len(payload))
    data += payload
    return bytes(data)


def test_received_subgroup_resolves_the_subgroup_id_from_the_first_object() -> None:
    """
    Subgroup ID を最初の Object ID として決めるモードの受信を確認する。

    ヘッダに Subgroup ID フィールドが無いため、ヘッダ受信直後は Subgroup ID が決まらず、
    最初の Object を受信した時点で確定する
    (draft-ietf-moq-transport-21 §11.3.1 (Subgroup Header))。
    """
    client, server = _setup()
    _subscribe_round_trip(client, server, 4)

    # SUBGROUP_ID_MODE = 0b01 (最初の Object ID が Subgroup ID)
    stream = _received_subgroup_stream(9, b"resolved", subgroup_id_mode=0x02)

    # ヘッダと Object を 1 度に渡すと、最初の Object の ID が Subgroup ID として載る
    objects, events = client.receive_data_stream(2, stream, 0x10)
    assert len(objects) == 1
    accepted = [event for event in events if event.kind == "object"]
    assert len(accepted) == 1
    assert accepted[0].object_id == 9
    assert accepted[0].subgroup_id == 9
    assert accepted[0].data == b"resolved"


def test_received_subgroup_header_alone_does_not_resolve_the_subgroup_id() -> None:
    """
    Object を受信する前は Subgroup ID が決まらないことを確認する。

    SUBGROUP_ID_MODE が 0b01 のヘッダには Subgroup ID フィールドが無いため、最初の
    Object を受信するまで Subgroup ID は確定しない
    (draft-ietf-moq-transport-21 §11.3.1 (Subgroup Header))。
    """
    client, server = _setup()
    _subscribe_round_trip(client, server, 4)

    # ヘッダだけを渡すと Object は 1 件も届かない
    header = encode_varint(0x32) + encode_varint(1) + encode_varint(7)
    objects, events = client.receive_data_stream(2, header, 0x10)
    assert objects == []
    assert [event for event in events if event.kind == "object"] == []

    # ヘッダに続けて Object を渡すと Subgroup ID が確定する
    payload = b"after-header"
    object_data = encode_varint(9) + encode_varint(len(payload)) + payload
    objects, events = client.receive_data_stream(2, object_data, None)
    assert len(objects) == 1
    accepted = [event for event in events if event.kind == "object"]
    assert len(accepted) == 1
    assert accepted[0].object_id == 9
    assert accepted[0].subgroup_id == 9


def test_received_subgroup_reports_an_explicit_subgroup_id_from_the_header() -> None:
    """
    Subgroup ID を明示するモードではヘッダの値がそのまま載ることを確認する。

    SUBGROUP_ID_MODE が 0b10 のヘッダは Subgroup ID フィールドを持ち、Object の受信を
    待たずに確定する (draft-ietf-moq-transport-21 §11.3.1 (Subgroup Header))。
    """
    client, server = _setup()
    _subscribe_round_trip(client, server, 4)

    # SUBGROUP_ID_MODE = 0b10 (Subgroup ID フィールドが存在する)
    stream = _received_subgroup_stream(0, b"explicit", subgroup_id_mode=0x04, subgroup_id=3)
    objects, events = client.receive_data_stream(2, stream, 0x10)

    assert len(objects) == 1
    accepted = [event for event in events if event.kind == "object"]
    assert len(accepted) == 1
    assert accepted[0].subgroup_id == 3


def test_send_subgroup_header_uses_the_first_object_id_mode() -> None:
    """
     Subgroup ID を最初の Object ID として決めるモードで送信できることを確認する。

     このモードでは Subgroup ID フィールドを送らないため、ヘッダは Type Flags、
     Track Alias、Group ID だけになる。状態機械は最初の Object の送信で Subgroup ID を
    確定する (draft-ietf-moq-transport-21 §11.3.1 (Subgroup Header))。
    """
    # client が PUBLISH で配信し、server が購読する経路で publisher 側の状態を作る
    client, server = _setup()
    events = client.send_publish([b"ns"], b"t", 1, {}, {})
    request_id = _request_id(events[0])
    client.register_local_request_stream(4, request_id)
    server.receive_request_stream(4, _message_data(events[0]), "peer")
    reply = server.send_request_ok(request_id, {}, {})
    client.receive_request_stream(4, _message_data(reply[0]), "local")

    # SUBGROUP_ID_MODE = 0b01 でヘッダを登録する
    sent = client.send_subgroup_header(
        8,
        request_id,
        1,
        7,
        None,
        moqt.SUBGROUP_ID_MODE_FIRST_OBJECT_ID,
        None,
        False,
        False,
        False,
    )
    assert [event.kind for event in sent] == []

    # 最初の Object を送ると Subgroup ID が確定する
    reply_events = client.send_subgroup_object(8, 9, None)
    assert [event.kind for event in reply_events[1]] == []


def test_send_subgroup_header_rejects_a_subgroup_id_in_the_first_object_id_mode() -> None:
    """
    Subgroup ID を渡しながら最初の Object ID モードを選べないことを確認する。

    このモードのヘッダに Subgroup ID フィールドは無いため、値の指定は wire と状態機械の
    食い違いになる (draft-ietf-moq-transport-21 §11.3.1 (Subgroup Header))。
    """
    client, server = _setup()
    events = client.send_publish([b"ns"], b"t", 1, {}, {})
    request_id = _request_id(events[0])
    client.register_local_request_stream(4, request_id)
    server.receive_request_stream(4, _message_data(events[0]), "peer")
    reply = server.send_request_ok(request_id, {}, {})
    client.receive_request_stream(4, _message_data(reply[0]), "local")

    with pytest.raises(ValueError, match="subgroup_id must be omitted"):
        client.send_subgroup_header(
            8,
            request_id,
            1,
            7,
            3,
            moqt.SUBGROUP_ID_MODE_FIRST_OBJECT_ID,
            None,
            False,
            False,
            False,
        )

    # Subgroup ID を省略しながら明示モードを選ぶこともできない
    with pytest.raises(ValueError, match="subgroup_id is required"):
        client.send_subgroup_header(
            8,
            request_id,
            1,
            7,
            None,
            moqt.SUBGROUP_ID_MODE_EXPLICIT,
            None,
            False,
            False,
            False,
        )

    # 未知のモードは受け付けない
    with pytest.raises(ValueError, match="unknown subgroup_id_mode"):
        client.send_subgroup_header(
            8,
            request_id,
            1,
            7,
            None,
            "reserved",
            None,
            False,
            False,
            False,
        )
