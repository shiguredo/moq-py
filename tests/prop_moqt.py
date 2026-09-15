"""`moq.moqt` の sans I/O セッション状態機械に対する Property-Based Testing。"""

from hypothesis import given
from hypothesis import strategies as st
from moq.moqt import PUBLISHER_PRIORITY_DEFAULT, Session, encode_varint

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
    client = Session.client("moq-py-test-client")
    server = Session.server("moq-py-test-server")
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
    client = Session.client("moq-py-test-client")
    server = Session.server("moq-py-test-server")
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
