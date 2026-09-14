"""webtransport-py と moqt-rs を接続する実通信テスト。"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import pytest
from moqt import Client, Server
from moqt.client import Fetch, MoqtObject, Subscription
from moqt.server import (
    FetchRequest,
    Publication,
    ServerSession,
    SubscriptionRequest,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

# テストで使う Track
NAMESPACE = [b"moqt-py", b"test"]
TRACK_NAME = b"video"
TRACK_ALIAS = 1


@pytest.fixture
async def moqt_pair(
    test_certificates: tuple[str, str],
) -> AsyncIterator[tuple[Client, Server, ServerSession]]:
    """localhost の実 WebTransport 接続で MoQT の client / server を接続する。"""
    certfile, keyfile = test_certificates
    server = Server(
        host="127.0.0.1",
        port=0,
        certfile=certfile,
        keyfile=keyfile,
    )
    server_established = asyncio.Event()
    established_session: ServerSession | None = None

    async def on_session_established(session: ServerSession) -> None:
        nonlocal established_session
        established_session = session
        server_established.set()

    server.on_session_established(on_session_established)
    await server.start()
    server_task = asyncio.create_task(server.run())
    client = Client(
        url=f"https://127.0.0.1:{server.actual_port}/webtransport",
        verify_peer=False,
    )

    try:
        await client.connect(timeout=5.0)
        await asyncio.wait_for(server_established.wait(), timeout=5.0)
        assert established_session is not None
        yield client, server, established_session
    finally:
        await client.close()
        await server.stop()
        server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server_task


@pytest.mark.asyncio
async def test_client_and_server_exchange_setup_over_webtransport(
    moqt_pair: tuple[Client, Server, ServerSession],
) -> None:
    """
    localhost の実 WebTransport 接続上で MoQT SETUP が成立することを確認する。

    SETUP 交換の完了、確立した session の識別情報、接続元アドレスを検証する。
    """
    client, _server, session = moqt_pair

    assert client.established
    assert session.session_id >= 0
    assert session.address[0] == "127.0.0.1"


@pytest.mark.asyncio
async def test_subscribe_and_receive_objects_over_subgroup(
    moqt_pair: tuple[Client, Server, ServerSession],
) -> None:
    """
    SUBSCRIBE / SUBSCRIBE_OK と subgroup ストリームのオブジェクト配送を確認する。

    client が購読し、server が同じ group のオブジェクトを 2 件送る。受信側で
    Group ID と Object ID が送信側と一致することを検証する。
    """
    client, server, _session = moqt_pair
    subscribe_requests: list[SubscriptionRequest] = []
    published: list[Publication] = []

    async def on_subscribe(request: SubscriptionRequest) -> None:
        subscribe_requests.append(request)
        published.append(await request.subscribe_ok(TRACK_ALIAS))

    server.on_subscribe(on_subscribe)

    subscription = await client.subscribe(NAMESPACE, TRACK_NAME)
    assert subscription.track_alias == TRACK_ALIAS
    await _wait_until(lambda: bool(published))
    assert len(subscribe_requests) == 1
    assert subscribe_requests[0].namespace == tuple(NAMESPACE)
    assert subscribe_requests[0].track_name == TRACK_NAME

    publication = published[0]
    await publication.send_object(3, 0, b"first")
    await publication.send_object(3, 1, b"second")

    received = await _take_objects(subscription, 2)

    assert [item.group_id for item in received] == [3, 3]
    assert [item.object_id for item in received] == [0, 1]
    assert [item.payload for item in received] == [b"first", b"second"]


@pytest.mark.asyncio
async def test_subscribe_and_receive_objects_over_datagram(
    moqt_pair: tuple[Client, Server, ServerSession],
) -> None:
    """
    オブジェクトデータグラムの配送を確認する。

    subgroup ストリームではなくデータグラムで送ったオブジェクトが、
    同じ Group ID と Object ID で受信できることを検証する。
    """
    client, server, _session = moqt_pair
    published: list[Publication] = []

    async def on_subscribe(request: SubscriptionRequest) -> None:
        published.append(await request.subscribe_ok(TRACK_ALIAS))

    server.on_subscribe(on_subscribe)

    subscription = await client.subscribe(NAMESPACE, TRACK_NAME)
    await _wait_until(lambda: bool(published))

    await published[0].send_datagram(7, 0, b"datagram payload")

    received = await _take_objects(subscription, 1)

    assert len(received) == 1
    assert received[0].group_id == 7
    assert received[0].object_id == 0
    assert received[0].payload == b"datagram payload"


# client から FETCH を送る経路は、応答後にライブラリがセッションを閉じる
# 不具合が残っている (issues/0001-bug-fetch-response-closes-session.md)。
@pytest.mark.xfail(reason="FETCH の応答処理が未完成である", strict=False)
@pytest.mark.asyncio
async def test_fetch_receives_objects(
    moqt_pair: tuple[Client, Server, ServerSession],
) -> None:
    """
    FETCH で要求した過去のオブジェクトが fetch stream で届くことを確認する。

    client が FETCH を送り、server が FETCH_OK と fetch stream で
    2 件のオブジェクトを返す。Group ID / Object ID / ペイロードが
    送信側と一致することを検証する。
    """
    client, server, _session = moqt_pair
    responded = asyncio.Event()

    async def on_fetch(request: FetchRequest) -> None:
        response = await request.respond((9, 9), end_of_track=False)
        await response.send_object(5, 10, b"fetched-1")
        await response.send_object(5, 11, b"fetched-2")
        await response.close()
        responded.set()

    server.on_fetch(on_fetch)

    fetch = await client.fetch(NAMESPACE, TRACK_NAME)
    assert fetch.end_of_track is False
    assert fetch.end_location == (9, 9)
    await asyncio.wait_for(responded.wait(), timeout=5.0)

    received = await _take_fetch_objects(fetch, 2)
    assert [item.group_id for item in received] == [5, 5]
    assert [item.object_id for item in received] == [10, 11]
    assert [item.payload for item in received] == [b"fetched-1", b"fetched-2"]


async def _take_fetch_objects(fetch: Fetch, count: int) -> list[MoqtObject]:
    """fetch から指定件数のオブジェクトを取り出す。"""
    received: list[MoqtObject] = []
    iterator = fetch.objects()
    for _ in range(count):
        received.append(await asyncio.wait_for(anext(iterator), timeout=5.0))
    return received


async def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """述語が真になるまで待つ。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise TimeoutError("condition was not satisfied in time")


async def _take_objects(subscription: Subscription, count: int) -> list[MoqtObject]:
    """subscription から指定件数のオブジェクトを取り出す。"""
    received: list[MoqtObject] = []
    iterator = subscription.objects()
    for _ in range(count):
        received.append(await asyncio.wait_for(anext(iterator), timeout=5.0))
    return received
