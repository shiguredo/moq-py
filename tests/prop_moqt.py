"""`moqt.moqt` の sans I/O セッション状態機械に対する Property-Based Testing。"""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from moqt.moqt import (
    GREASE_BASE,
    GREASE_INTERVAL,
    GREASE_MAX,
    MANDATORY_TRACK_PROPERTY_MAX,
    MANDATORY_TRACK_PROPERTY_MIN,
    PARAM_AUTHORIZATION_TOKEN,
    PARAM_EXPIRES,
    PARAM_FILL_PARAMETERS,
    PARAM_FILL_TIMEOUT,
    PARAM_FORWARD,
    PARAM_GROUP_ORDER,
    PARAM_LARGEST_OBJECT,
    PARAM_LOCATION_FILTER,
    PARAM_NEW_GROUP_REQUEST,
    PARAM_OBJECT_DELIVERY_TIMEOUT,
    PARAM_SUBGROUP_DELIVERY_TIMEOUT,
    PARAM_SUBSCRIBER_PRIORITY,
    PARAM_TRACK_NAMESPACE_PREFIX,
    PROP_DEFAULT_PUBLISHER_GROUP_ORDER,
    PROP_DEFAULT_PUBLISHER_PRIORITY,
    PROP_DYNAMIC_GROUPS,
    PROP_IMMUTABLE_PROPERTIES,
    PUBLISHER_PRIORITY_DEFAULT,
    LocationFilter,
    MessageParameters,
    ObjectProperties,
    Session,
    TrackProperties,
    decode_message,
    encode_varint,
    generate,
    is_grease,
)

# Object Status の Normal (draft-ietf-moq-transport-21 §11.1.2 (Object Status))。
OBJECT_STATUS_NORMAL = 0x0

# 購読で使う Track Alias と Group ID。
TRACK_ALIAS = 1
GROUP_ID = 1

# subgroup ストリームを載せる片方向ストリームの ID。
SUBGROUP_STREAM_ID = 2

# subgroup データストリームの stream type。
SUBGROUP_STREAM_TYPE = 0x10

# 購読の要求と応答を載せる双方向ストリームの ID。
REQUEST_STREAM_ID = 0

# プロパティの型番号と varint 値として生成する値の上限。
#
# varint は 8 バイト表現で 62 bit まで運べる
# (draft-ietf-moq-transport-21 §1.4 (Varint Encoding))。
MAX_PROPERTY_TYPE = 2**62 - 1
MAX_PROPERTY_VALUE = 2**62 - 1

# バイト列型プロパティの長さの上限。
#
# draft の値長上限は 2^16-1 バイトである
# (draft-ietf-moq-transport-21 §8.3 (Key-Value-Pair Structure))。ここでは
# テストの実行時間を抑えるため十分に小さい値にする。
MAX_PROPERTY_BYTES = 64

# 生成するプロパティ列の最大件数。
MAX_PROPERTY_ENTRIES = 8

# 値域が仕様で固定されている Track Property の型番号
# (draft-ietf-moq-transport-21 §10.4 (DEFAULT PUBLISHER PRIORITY) /
# §10.5 (DEFAULT PUBLISHER GROUP ORDER) / §10.6 (DYNAMIC GROUPS))。
VALUE_CONSTRAINED_TRACK_TYPES = (
    PROP_DEFAULT_PUBLISHER_PRIORITY,
    PROP_DEFAULT_PUBLISHER_GROUP_ORDER,
    PROP_DYNAMIC_GROUPS,
)


def _is_mandatory_track_type(prop_type: int) -> bool:
    """必須 Track Property の範囲 (0x4000-0x7FFF) の型番号かどうかを返す。

    この範囲の型番号は Track scope では正当だが、この実装が認識していない値を受信すると
    購読が成立しない (draft-ietf-moq-transport-21 §3.6 (Mandatory Track Properties))。
    Object scope では malformed になる。
    """
    return MANDATORY_TRACK_PROPERTY_MIN <= prop_type <= MANDATORY_TRACK_PROPERTY_MAX


def _is_generatable_track_type(prop_type: int) -> bool:
    """任意の値で生成してよい Track Property の型番号かどうかを返す。

    IMMUTABLE_PROPERTIES は中身の KVP 列が別途正しい必要があり、値域が固定された
    3 つの型番号は任意の varint を受け付けない。必須 Track Property の範囲は
    未知の値を送ると購読が成立しない。これらを生成対象から外す。
    """
    return (
        prop_type != PROP_IMMUTABLE_PROPERTIES
        and prop_type not in VALUE_CONSTRAINED_TRACK_TYPES
        and not _is_mandatory_track_type(prop_type)
    )


def _is_generatable_object_type(prop_type: int) -> bool:
    """任意の値で生成してよい Object Property の型番号かどうかを返す。"""
    return prop_type != PROP_IMMUTABLE_PROPERTIES and not _is_mandatory_track_type(prop_type)


# 生成対象の Object Property / Track Property の型番号。
TRACK_PROPERTY_TYPES = st.integers(min_value=0, max_value=MAX_PROPERTY_TYPE).filter(
    _is_generatable_track_type
)
OBJECT_PROPERTY_TYPES = st.integers(min_value=0, max_value=MAX_PROPERTY_TYPE).filter(
    _is_generatable_object_type
)


@st.composite
def _track_properties(draw: st.DrawFn) -> list[tuple[int, int | bytes]]:
    """型番号が重複しない任意の Track Property 列を生成する。

    偶数型は varint、奇数型はバイト列である
    (draft-ietf-moq-transport-21 §8.3 (Key-Value-Pair Structure))。
    """
    prop_types = draw(
        st.lists(
            TRACK_PROPERTY_TYPES,
            min_size=1,
            max_size=MAX_PROPERTY_ENTRIES,
            unique=True,
        )
    )
    properties: list[tuple[int, int | bytes]] = []
    for prop_type in prop_types:
        # 偶数型は varint、奇数型は長さ付きバイト列として生成する
        if prop_type % 2 == 0:
            value: int | bytes = draw(st.integers(min_value=0, max_value=MAX_PROPERTY_VALUE))
        else:
            value = draw(st.binary(max_size=MAX_PROPERTY_BYTES))
        properties.append((prop_type, value))
    return properties


@st.composite
def _object_properties(draw: st.DrawFn) -> list[tuple[int, int | bytes]]:
    """型番号が重複しない任意の Object Property 列を生成する。

    偶数型は varint、奇数型はバイト列である
    (draft-ietf-moq-transport-21 §8.3 (Key-Value-Pair Structure))。
    """
    prop_types = draw(
        st.lists(
            OBJECT_PROPERTY_TYPES,
            min_size=1,
            max_size=MAX_PROPERTY_ENTRIES,
            unique=True,
        )
    )
    properties: list[tuple[int, int | bytes]] = []
    for prop_type in prop_types:
        # 偶数型は varint、奇数型は長さ付きバイト列として生成する
        if prop_type % 2 == 0:
            value: int | bytes = draw(st.integers(min_value=0, max_value=MAX_PROPERTY_VALUE))
        else:
            value = draw(st.binary(max_size=MAX_PROPERTY_BYTES))
        properties.append((prop_type, value))
    return properties


def _track_properties_from_list(
    properties: list[tuple[int, int | bytes]],
) -> TrackProperties:
    """Track Property 列から `TrackProperties` を組み立てる。"""
    result = TrackProperties()
    for prop_type, value in properties:
        result.add(prop_type, value)
    return result


def _object_properties_from_list(
    properties: list[tuple[int, int | bytes]],
) -> ObjectProperties:
    """Object Property 列から `ObjectProperties` を組み立てる。"""
    result = ObjectProperties()
    for prop_type, value in properties:
        result.add(prop_type, value)
    return result


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


def _establish() -> tuple[Session, Session, int]:
    """SETUP と SUBSCRIBE を終えた client / server と Request ID を返す。"""
    client = Session.client("moqt-py-test-client")
    server = Session.server("moqt-py-test-server")
    client_setup = client.start()
    server_setup = server.start()
    server.receive_control(client_setup)
    client.receive_control(server_setup)

    request = client.send_subscribe([b"ns"], b"track", {})[0]
    assert request.request_id is not None
    client.register_local_request_stream(REQUEST_STREAM_ID, request.request_id)
    assert request.message_data is not None
    server.receive_request_stream(REQUEST_STREAM_ID, request.message_data, "peer")
    ok = server.send_subscribe_ok(request.request_id, TRACK_ALIAS, {}, {})[0]
    assert ok.message_data is not None
    client.receive_request_stream(REQUEST_STREAM_ID, ok.message_data, "local")
    return client, server, request.request_id


def _subgroup_stream(
    object_ids: list[int], payloads: list[bytes], *, with_properties: bool
) -> bytes:
    """subgroup ストリームのバイト列を組み立てる。

    Subgroup ID を明示し、Object ID は差分で表現する
    (draft-ietf-moq-transport-21 §11.3.1 (Subgroup Header))。
    """
    # bit 4 は常に 1。SUBGROUP_ID_MODE = 0b10 (bits 1-2) は Subgroup ID の明示、
    # bit 0 は Properties の存在を表す。
    type_byte = 0x10 | 0x04
    if with_properties:
        type_byte |= 0x01
    data = bytearray()
    data += encode_varint(type_byte)
    data += encode_varint(TRACK_ALIAS)
    data += encode_varint(GROUP_ID)
    data += encode_varint(0)
    data.append(PUBLISHER_PRIORITY_DEFAULT)

    previous: int | None = None
    for object_id, payload in zip(object_ids, payloads, strict=True):
        data += encode_varint(object_id if previous is None else object_id - previous - 1)
        if with_properties:
            # プロパティを持たないオブジェクトは Properties Length = 0 を明示する
            data += encode_varint(0)
        data += encode_varint(len(payload))
        if not payload:
            # ペイロード長 0 のオブジェクトは Object Status を明示する
            data += encode_varint(OBJECT_STATUS_NORMAL)
        data += payload
        previous = object_id
    return bytes(data)


def _receive_objects(
    client: Session,
    stream: bytes,
    sizes: list[int],
) -> list[tuple[int, bytes]]:
    """subgroup ストリームを任意の断片へ分割して投入し、受理したオブジェクトを返す。"""
    received: list[tuple[int, bytes]] = []
    for index, chunk in enumerate(_split(stream, sizes)):
        # stream type は最初の断片だけが運ぶ
        _, events = client.receive_data_stream(
            SUBGROUP_STREAM_ID,
            chunk,
            SUBGROUP_STREAM_TYPE if index == 0 else None,
        )
        for event in events:
            if event.kind != "object" or event.acceptance != "accepted":
                continue
            assert event.object_id is not None
            assert event.data is not None
            received.append((event.object_id, event.data))
    return received


@given(
    client_sizes=st.lists(st.integers(min_value=1, max_value=16), min_size=1, max_size=16),
    server_sizes=st.lists(st.integers(min_value=1, max_value=16), min_size=1, max_size=16),
)
def prop_setup_establishes_for_arbitrary_stream_fragmentation(
    client_sizes: list[int],
    server_sizes: list[int],
) -> None:
    """SETUP が任意の stream fragment 境界でも client/server 双方で成立する。"""
    client = Session.client("moqt-py-test-client")
    server = Session.server("moqt-py-test-server")
    client_setup = client.start()
    server_setup = server.start()

    # WebTransport の受信 fragment 境界は MoQT メッセージ境界と一致しない。
    for chunk in _split(client_setup, client_sizes):
        server.receive_control(chunk)
    for chunk in _split(server_setup, server_sizes):
        client.receive_control(chunk)

    assert client.established
    assert server.established


@given(
    objects=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=2**20),
            st.binary(max_size=200),
        ),
        min_size=1,
        max_size=4,
    ),
    sizes=st.lists(st.integers(min_value=1, max_value=64), min_size=1, max_size=32),
    with_properties=st.booleans(),
)
def prop_subgroup_objects_survive_arbitrary_fragmentation(
    objects: list[tuple[int, bytes]],
    sizes: list[int],
    *,
    with_properties: bool,
) -> None:
    """
    subgroup ストリームの断片化境界がオブジェクトの配送に影響しないことを確認する。

    Object ID は昇順に解決し、ペイロードが空のオブジェクトも扱う。WebTransport の
    受信 fragment 境界はオブジェクト境界と一致しないため、どのような分割でも
    同じオブジェクトが同じ順序で届く必要がある。
    """
    client, _server, _request_id = _establish()
    object_ids = sorted({object_id for object_id, _ in objects})
    payloads = [payload for _, payload in objects][: len(object_ids)]
    stream = _subgroup_stream(object_ids, payloads, with_properties=with_properties)

    received = _receive_objects(client, stream, sizes)

    assert received == list(zip(object_ids, payloads, strict=True))


@given(
    object_ids=st.lists(
        st.integers(min_value=0, max_value=2**20),
        min_size=1,
        max_size=4,
        unique=True,
    ).map(sorted),
    payloads=st.lists(st.binary(max_size=64), min_size=4, max_size=4),
    sizes=st.lists(st.integers(min_value=1, max_value=8), min_size=1, max_size=48),
)
def prop_subgroup_objects_survive_byte_by_byte_fragmentation(
    object_ids: list[int],
    payloads: list[bytes],
    sizes: list[int],
) -> None:
    """
    1 バイトずつの断片でも subgroup オブジェクトが復元されることを確認する。

    ヘッダの varint が 1 バイトずつ届く場合、ヘッダが揃うまでオブジェクトを
    デコードしてはならない。
    """
    client, _server, _request_id = _establish()
    payloads = payloads[: len(object_ids)]
    stream = _subgroup_stream(object_ids, payloads, with_properties=False)

    received = _receive_objects(client, stream, [1])

    assert received == list(zip(object_ids, payloads, strict=True))


@given(properties=_object_properties())
def prop_object_properties_round_trip(
    properties: list[tuple[int, int | bytes]],
) -> None:
    """
    任意の Object Property 列が encode / decode で元の内容へ戻ることを確認する。

    decode は先頭の Properties Length が示す範囲だけを消費し、消費バイト数と
    エンコード長が一致する。ワイヤ上は型番号の昇順に正規化されるため、列挙の順も
    型番号の昇順になる。型番号は重複しないので、値の取り違えが起きれば列が一致しなくなる。
    """
    original = _object_properties_from_list(properties)
    encoded = original.encode()

    # Properties Length (vi64) が先頭に付く
    # (draft-ietf-moq-transport-21 §8.3 (Key-Value-Pair Structure))
    decoded, consumed = ObjectProperties.decode(encoded)

    assert consumed == len(encoded)
    assert decoded.encode() == encoded
    # 列挙はワイヤ上の順 (型番号の昇順) になる
    assert decoded.items() == sorted(properties)
    assert list(decoded) == sorted(properties)
    assert len(decoded) == len(properties)


@given(properties=_track_properties())
def prop_track_properties_round_trip(
    properties: list[tuple[int, int | bytes]],
) -> None:
    """
    任意の Track Property 列が encode / decode で元の内容へ戻ることを確認する。

    Track Properties には長さプレフィックスが無く、セッションが運ぶ KVP 列そのものが
    encode 結果になる (draft-ietf-moq-transport-21 §8.4 (Track and Object Properties))。
    列挙の順はワイヤ上と同じ型番号の昇順になる。
    """
    original = _track_properties_from_list(properties)
    encoded = original.encode()
    decoded = TrackProperties.decode(encoded)

    assert len(encoded) > 0
    assert decoded.encode() == encoded
    # 列挙はワイヤ上の順 (型番号の昇順) になる
    assert decoded.items() == sorted(properties)
    assert list(decoded) == sorted(properties)
    assert decoded.to_dict() == dict(properties)
    assert len(decoded) == len(properties)


@given(properties=_track_properties())
def prop_track_properties_encode_matches_session_output(
    properties: list[tuple[int, int | bytes]],
) -> None:
    """
    Track Properties の encode 結果がセッションの送出する PUBLISH と一致することを確認する。

    properties ブロックは制御メッセージの末尾にそのまま埋め込まれる
    (draft-ietf-moq-transport-21 §9.10 (PUBLISH))。したがって同じ型番号と値の組を
    辞書で渡した場合と `TrackProperties` で組み立てた場合とで、ワイヤ上の
    properties ブロックは同じバイト列になる。
    """
    server = Session.server("moqt-py-test-server")
    client = Session.client("moqt-py-test-client")
    client_setup = client.start()
    server_setup = server.start()
    server.receive_control(client_setup)
    client.receive_control(server_setup)

    encoded = _track_properties_from_list(properties).encode()

    # セッションへは辞書で渡し、同じ組を TrackProperties でも組み立てる
    events = server.send_publish([b"ns"], b"track", 1, {}, dict(properties))
    assert events[0].message_data is not None
    message, _consumed = decode_message(events[0].message_data)

    # properties は本文の末尾に置かれるため、encode 結果はメッセージの末尾と一致する
    assert events[0].message_data.endswith(encoded)
    assert message.body["track_properties"] == dict(properties)

    # 受信側が得た辞書から組み立て直しても同じバイト列へ戻る
    received = TrackProperties()
    for prop_type, value in message.body["track_properties"].items():
        received.add(prop_type, value)
    assert received.encode() == encoded


# Message Parameter の型番号と値の上限。
#
# vi64 は 62 bit まで運べる (draft-ietf-moq-transport-21 §1.4 (Varint Encoding))。
MAX_MESSAGE_PARAMETER_VALUE = 2**62 - 1

# 値が vi64 のパラメータ型 (draft-ietf-moq-transport-21 §9.20 (Control Message Parameters))。
VARINT_PARAMETER_TYPES = (
    PARAM_OBJECT_DELIVERY_TIMEOUT,
    PARAM_SUBGROUP_DELIVERY_TIMEOUT,
    PARAM_FILL_TIMEOUT,
    PARAM_EXPIRES,
    PARAM_NEW_GROUP_REQUEST,
)

# LOCATION_FILTER の種別 (draft-ietf-moq-transport-21 §9.20.10 (LOCATION FILTER Parameter))。
LOCATION_FILTER_KINDS = (
    "relative_group",
    "next_object",
    "absolute_start",
    "absolute_range",
    "absolute_range_with_end",
)


@st.composite
def _location_filters(draw: st.DrawFn) -> LocationFilter:
    """任意の LOCATION_FILTER を生成する。"""
    kind = draw(st.sampled_from(LOCATION_FILTER_KINDS))
    fields: dict[str, int] = {}
    if kind != "next_object":
        fields["start_group"] = draw(
            st.integers(min_value=0, max_value=MAX_MESSAGE_PARAMETER_VALUE)
        )
    if kind in ("absolute_start", "absolute_range", "absolute_range_with_end"):
        fields["start_object"] = draw(
            st.integers(min_value=0, max_value=MAX_MESSAGE_PARAMETER_VALUE)
        )
    if kind in ("absolute_range", "absolute_range_with_end"):
        # End Group は Start Group からの差分であり、和が vi64 に収まる必要がある
        fields["end_group_delta"] = draw(
            st.integers(min_value=0, max_value=MAX_MESSAGE_PARAMETER_VALUE)
        )
    if kind == "absolute_range_with_end":
        fields["end_object"] = draw(st.integers(min_value=0, max_value=MAX_MESSAGE_PARAMETER_VALUE))
    location_filter = LocationFilter(kind, **fields)
    # Start が両方 0 の absolute_start は wire 上で next_object と区別できないため、
    # 往復が一致する組み合わせだけを生成する
    if kind == "absolute_start" and fields["start_group"] == 0 and fields["start_object"] == 0:
        return LocationFilter("next_object")
    return location_filter


@st.composite
def _message_parameters(draw: st.DrawFn) -> dict[int, object]:
    """型ごとの Python 表現で任意の Message Parameter の辞書を生成する。

    (draft-ietf-moq-transport-21 §9.20 (Control Message Parameters))
    """
    parameters: dict[int, object] = {}
    for param_type in draw(
        st.lists(
            st.sampled_from(VARINT_PARAMETER_TYPES),
            unique=True,
            max_size=len(VARINT_PARAMETER_TYPES),
        )
    ):
        parameters[param_type] = draw(
            st.integers(min_value=0, max_value=MAX_MESSAGE_PARAMETER_VALUE)
        )
    if draw(st.booleans()):
        # FORWARD は 0 または 1 である
        parameters[PARAM_FORWARD] = draw(st.integers(min_value=0, max_value=1))
    if draw(st.booleans()):
        # GROUP_ORDER は 1 (Ascending) または 2 (Descending) である
        parameters[PARAM_GROUP_ORDER] = draw(st.integers(min_value=1, max_value=2))
    if draw(st.booleans()):
        parameters[PARAM_SUBSCRIBER_PRIORITY] = draw(st.integers(min_value=0, max_value=255))
    if draw(st.booleans()):
        parameters[PARAM_LARGEST_OBJECT] = (
            draw(st.integers(min_value=0, max_value=MAX_MESSAGE_PARAMETER_VALUE)),
            draw(st.integers(min_value=0, max_value=MAX_MESSAGE_PARAMETER_VALUE)),
        )
    if draw(st.booleans()):
        parameters[PARAM_LOCATION_FILTER] = draw(_location_filters())
    if draw(st.booleans()):
        # namespace のフィールドは空にできず、合計 4096 バイト以下である
        parameters[PARAM_TRACK_NAMESPACE_PREFIX] = draw(
            st.lists(st.binary(min_size=1, max_size=8), max_size=4)
        )
    if draw(st.booleans()):
        # FILL_PARAMETERS の内側は FILL_TIMEOUT だけを生成する
        parameters[PARAM_FILL_PARAMETERS] = {
            PARAM_FILL_TIMEOUT: draw(
                st.integers(min_value=0, max_value=MAX_MESSAGE_PARAMETER_VALUE)
            )
        }
    if draw(st.booleans()):
        # 同じ (Token Type, Token Value) の組は重複できない
        tokens = draw(
            st.lists(
                st.tuples(
                    st.integers(min_value=0, max_value=MAX_MESSAGE_PARAMETER_VALUE),
                    st.binary(max_size=8),
                ),
                max_size=3,
                unique=True,
            )
        )
        if tokens:
            parameters[PARAM_AUTHORIZATION_TOKEN] = tokens
    return parameters


@given(parameters=_message_parameters())
def prop_message_parameters_round_trip(parameters: dict[int, object]) -> None:
    """
    Message Parameter がエンコード済みバイト列の辞書と往復することを確認する。

    `to_dict()` は `Event.parameters` / `Message.parameters` と同じ形式の辞書を返し、
    その辞書から構築し直した集合は元と一致する
    (draft-ietf-moq-transport-21 §9.20 (Control Message Parameters))。
    """
    built = MessageParameters(parameters)

    encoded = built.to_dict()

    assert MessageParameters(encoded) == built
    assert MessageParameters(encoded).to_dict() == encoded


@given(location_filter=_location_filters())
def prop_location_filter_round_trip(location_filter: LocationFilter) -> None:
    """
    LOCATION_FILTER が wire 形式のバイト列と往復することを確認する。

    フィールド数が値の意味を決め、AbsoluteStart の {0, 0} だけは NextObject へ
    正規化される (draft-ietf-moq-transport-21 §9.20.10 (LOCATION FILTER Parameter))。
    """
    encoded = location_filter.encode()

    decoded = LocationFilter.decode(encoded)

    assert decoded.encode() == encoded
    assert LocationFilter.decode(decoded.encode()) == decoded


# GREASE 値の連番 (`0x7F * N + 0x9D` の N) として取り得る最大値。
#
# `GREASE_MAX` ちょうどが値の並びに乗るため、この値が最大の N になる。
MAX_GREASE_SEQUENCE = (GREASE_MAX - GREASE_BASE) // GREASE_INTERVAL


@given(sequence=st.integers(min_value=0, max_value=MAX_GREASE_SEQUENCE))
def prop_grease_round_trip(sequence: int) -> None:
    """
    範囲内の連番から生成した GREASE 値を `is_grease` が受理することを確認する。

    値の並びは `0x7F * N + 0x9D` で上限は `GREASE_MAX` である
    (draft-ietf-moq-transport-21 §13 (Grease))。乱数源を渡して連番を固定するため、
    生成される値も固定される。
    """
    # 乱数源は `stop` を受け取る呼び出し可能オブジェクトである
    value = generate(lambda _stop: sequence)

    assert value == GREASE_INTERVAL * sequence + GREASE_BASE
    assert value <= GREASE_MAX
    assert is_grease(value) is True


@given(sequence=st.integers(min_value=MAX_GREASE_SEQUENCE + 1, max_value=2**64 - 1))
def prop_grease_generate_rejects_out_of_range_sequences(sequence: int) -> None:
    """
    上限を超える連番を乱数源が返したときに `generate` が失敗することを確認する。

    上限を超える連番からは `GREASE_MAX` を超える値しか作れないため、黙って
    上限へ丸めずエラーにする。
    """
    with pytest.raises(ValueError, match="exceeds"):
        generate(lambda _stop: sequence)


@given(
    value=st.integers(min_value=0, max_value=2**64 - 1).filter(
        lambda value: value % GREASE_INTERVAL != GREASE_BASE % GREASE_INTERVAL
    )
)
def prop_grease_is_grease_rejects_other_values(value: int) -> None:
    """
    値の並びに合致しない値を `is_grease` が拒否することを確認する。

    `(value - GREASE_BASE)` が `GREASE_INTERVAL` で割り切れない値、または
    `GREASE_BASE` より小さい値は GREASE ではない。
    """
    assert is_grease(value) is False
