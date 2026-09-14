"""namespace 系・TRACK_STATUS・GOAWAY・パディングの native 往復確認。

WebTransport を介さず、Sans I/O facade の入出力だけで確認する。
"""

from moqt._native import _CoreEvent, _CoreSession


def _request_id(event: _CoreEvent) -> int:
    """イベントの Request ID を取り出す。"""
    assert event.request_id is not None
    return event.request_id


def _message_data(event: _CoreEvent) -> bytes:
    """イベントのメッセージバイト列を取り出す。"""
    assert event.message_data is not None
    return event.message_data


def _event_data(event: _CoreEvent) -> bytes:
    """イベントの送信バイト列を取り出す。"""
    assert event.data is not None
    return event.data


def _setup() -> tuple[_CoreSession, _CoreSession]:
    client = _CoreSession.client("c")
    server = _CoreSession.server("s")
    client_setup = client.start()
    server_setup = server.start()
    server.receive_control(client_setup)
    client.receive_control(server_setup)
    return client, server


def _round_trip(
    client: _CoreSession,
    server: _CoreSession,
    stream_id: int,
    events: list[_CoreEvent],
    request_id: int,
) -> list[_CoreEvent]:
    """request を送り、REQUEST_OK を受け取るまでを往復させる。"""
    client.register_local_request_stream(stream_id, request_id)
    server.receive_request_stream(stream_id, _message_data(events[0]), "peer")
    ok = server.send_request_ok(request_id, {}, {})
    return client.receive_request_stream(stream_id, _message_data(ok[0]), "local")


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
