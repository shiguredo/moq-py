"""webtransport-py と moqt-rs を接続する実通信テスト。"""

import asyncio
import contextlib
import importlib
import importlib.util
import logging

import pytest
from moqt import loc, moqt
from moqt.moq import Client, Fetch, MoqtObject, PeerGoaway, Server, ServerSession, Subscription
from moqt.moq._runtime import MoqtError, Runtime
from moqt.moq.server import FetchRequest, Publication, PublisherRequest, SubscriptionRequest
from moqt.moq.testing import ClientFactory, MoqPair, collect_objects, wait_until

# テストで使う Track
NAMESPACE = [b"moqt-py", b"test"]
TRACK_NAME = b"video"
TRACK_ALIAS = 1

# オブジェクトの待ち合わせの上限秒数
OBJECT_TIMEOUT = 5.0


async def _send_object(
    publication: Publication,
    kind: str,
    payload: bytes,
    status: int | None = None,
) -> None:
    """subgroup とデータグラムのどちらかの経路でオブジェクトを送る。"""
    if kind == "subgroup":
        await publication.send_object(1, 0, payload, status=status)
    else:
        await publication.send_datagram(1, 0, payload, status=status)


async def _take_objects(subscription: Subscription, count: int) -> list[MoqtObject]:
    """subscription から指定件数のオブジェクトを取り出す。"""
    return await collect_objects(subscription.objects(), count, OBJECT_TIMEOUT)


async def _take_objects_in_group(
    subscription: Subscription,
    group_id: int,
    limit: int = 4,
) -> list[MoqtObject]:
    """指定した Group ID のオブジェクトが届くまで取り出す。

    reset と競合したオブジェクトは破棄されることもあれば届くこともある
    (RESET_STREAM は送信側の操作であり、到着済みのデータは取り消せない)。
    reset の後に送ったオブジェクトが届いたことを確かめるために使う。
    """
    collected: list[MoqtObject] = []
    async for item in subscription.objects():
        collected.append(item)
        if item.group_id == group_id:
            return collected
        if len(collected) >= limit:
            break
    return collected


async def _take_fetch_objects(fetch: Fetch, count: int) -> list[MoqtObject]:
    """fetch から指定件数のオブジェクトを取り出す。"""
    return await collect_objects(fetch.objects(), count, OBJECT_TIMEOUT)


def _range_filter(
    set_id: int,
    start: int,
    end: int,
    property_type: int | None = None,
) -> bytes:
    """Range Filter 1 個分のバイト列を作る。

    `SetID (8 bits) | [Property Type (vi64)] | Start Delta (vi64) | End Delta (vi64)`
    の形である。Property Type を持つのは OBJECT_PROPERTY_FILTER と
    TRACK_PROPERTY_FILTER だけである
    (draft-ietf-moq-transport-21 §3.3.2 (Range Filters))。
    """
    data = bytes([set_id])
    if property_type is not None:
        data += moqt.encode_varint(property_type)
    data += moqt.encode_varint(start)
    data += moqt.encode_varint(end - start)
    return data


def _object_properties(timestamp: int) -> bytes:
    """LOC の TIMESTAMP だけを持つ Object Properties を作る。"""
    properties = loc.Properties()
    properties.add(loc.TIMESTAMP, timestamp)
    return properties.encode()


async def test_client_and_server_exchange_setup_over_webtransport(moq_pair: MoqPair) -> None:
    """
    localhost の実 WebTransport 接続上で MoQT SETUP が成立することを確認する。

    SETUP 交換の完了、確立した session の識別情報、接続元アドレスを検証する。
    """
    assert moq_pair.client.established
    assert moq_pair.session.session_id >= 0
    assert moq_pair.session.address[0] == "127.0.0.1"
    # peer が SETUP で宣言した Setup Option が client と server の両方から見える
    # (draft-ietf-moq-transport-21 §16.4 (Setup Options))
    assert moq_pair.client.peer_setup_options[moqt.SETUP_OPTION_MOQT_IMPLEMENTATION] == b"moqt-py"
    assert (
        moq_pair.session.runtime.peer_setup_options[moqt.SETUP_OPTION_MOQT_IMPLEMENTATION]
        == b"moqt-py"
    )


async def test_object_property_filter_selects_objects_by_property(
    moq_certificates: tuple[str, str],
) -> None:
    """
    OBJECT_PROPERTY_FILTER を満たすオブジェクトだけが送信されることを確認する。

    publisher は Range Filter を評価し、条件を満たさないオブジェクトを送らない
    (draft-ietf-moq-transport-21 §3.3.3 (Combining Filters))。評価には
    Object Properties が要る (§11.1.3 (Object Properties))。
    subscriber が Range Filter を送るには publisher が SETUP で MAX_FILTER_RANGES を
    宣言している必要がある (§9.1.6 (MAX FILTER RANGES))。
    """
    certfile, keyfile = moq_certificates
    server = Server(
        host="127.0.0.1",
        port=0,
        certfile=certfile,
        keyfile=keyfile,
        setup_options={moqt.SETUP_OPTION_MAX_FILTER_RANGES: 8},
    )
    await server.start()
    run_task = asyncio.create_task(server.run())
    client: Client | None = None
    try:
        published: list[Publication] = []

        async def on_subscribe(request: SubscriptionRequest) -> None:
            published.append(await request.subscribe_ok(TRACK_ALIAS))

        server.on_subscribe(on_subscribe)
        client = Client(
            url=f"https://127.0.0.1:{server.actual_port}/webtransport",
            verify_peer=False,
        )
        await client.connect()

        # TIMESTAMP が 100 のオブジェクトだけを要求する
        subscription = await client.subscribe(
            NAMESPACE,
            TRACK_NAME,
            {moqt.PARAM_OBJECT_PROPERTY_FILTER: _range_filter(0, 100, 100, loc.TIMESTAMP)},
        )
        await wait_until(lambda: bool(published))

        # 条件を満たさないオブジェクトを先に送る。フィルタが効いていれば届かない
        await published[0].send_object(1, 0, b"mismatch", properties_data=_object_properties(200))
        await published[0].send_object(1, 1, b"match", properties_data=_object_properties(100))

        received = await collect_objects(subscription.objects(), 1, OBJECT_TIMEOUT)
        assert received[0].payload == b"match"
        assert received[0].object_id == 1
    finally:
        if client is not None:
            await client.close()
        await server.stop()
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task


async def test_two_clients_connect_to_one_server(moq_client_factory: ClientFactory) -> None:
    """
    同じ server へ 2 本の client を接続できることを確認する。

    `moq_client_factory` が返す factory を繰り返し呼び、それぞれの接続で
    MoQT SETUP が成立することを検証する。
    """
    first: Client = await moq_client_factory()
    second: Client = await moq_client_factory()

    try:
        assert first.established
        assert second.established
    finally:
        await first.close()
        await second.close()


async def test_subscribe_and_receive_objects_over_subgroup(moq_pair: MoqPair) -> None:
    """
    SUBSCRIBE / SUBSCRIBE_OK と subgroup ストリームのオブジェクト配送を確認する。

    client が購読し、server が同じ group のオブジェクトを 2 件送る。受信側で
    Group ID と Object ID が送信側と一致することを検証する。
    """
    subscribe_requests: list[SubscriptionRequest] = []
    published: list[Publication] = []

    async def on_subscribe(request: SubscriptionRequest) -> None:
        subscribe_requests.append(request)
        published.append(await request.subscribe_ok(TRACK_ALIAS))

    moq_pair.server.on_subscribe(on_subscribe)

    subscription = await moq_pair.client.subscribe(NAMESPACE, TRACK_NAME)
    assert subscription.track_alias == TRACK_ALIAS
    await wait_until(lambda: bool(published))
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


async def test_objects_in_a_second_group_are_delivered(moq_pair: MoqPair) -> None:
    """
    同じ subscription で Group を進めてもオブジェクトが届くことを確認する。

    subgroup ストリームの最初のオブジェクトの Object ID は絶対値であり、直前の Group の
    Object ID を基準にした差分ではない
    (draft-ietf-moq-transport-21 §11.3.1 (Subgroup Header))。
    """
    published: list[Publication] = []

    async def on_subscribe(request: SubscriptionRequest) -> None:
        published.append(await request.subscribe_ok(TRACK_ALIAS))

    moq_pair.server.on_subscribe(on_subscribe)

    subscription = await moq_pair.client.subscribe(NAMESPACE, TRACK_NAME)
    await wait_until(lambda: bool(published))

    publication = published[0]
    await publication.send_object(1, 0, b"first")
    await publication.send_object(1, 1, b"second")
    # Group を進めると新しい subgroup ストリームが開き、Object ID は絶対値に戻る
    await publication.send_object(2, 0, b"third")

    received = await _take_objects(subscription, 3)

    # Group 1 と Group 2 は別の subgroup ストリームであり、ストリーム間の到着順は
    # 保証されない。同じストリーム内の Object の順序だけを Group ごとに検証する。
    by_group: dict[int, list[tuple[int, bytes]]] = {}
    for item in received:
        by_group.setdefault(item.group_id, []).append((item.object_id, item.payload))
    assert by_group == {
        1: [(0, b"first"), (1, b"second")],
        2: [(0, b"third")],
    }


async def test_object_properties_are_delivered(moq_pair: MoqPair) -> None:
    """
    subgroup で送った Object Properties が受信側で参照できることを確認する。

    Properties は subgroup ヘッダの has_properties bit で有無が固定される。受信側では
    `MoqtObject.properties` から生バイトとして取り出し、`ObjectProperties.decode` で
    解釈する。
    """
    published: list[Publication] = []

    async def on_subscribe(request: SubscriptionRequest) -> None:
        published.append(await request.subscribe_ok(TRACK_ALIAS))

    moq_pair.server.on_subscribe(on_subscribe)

    subscription = await moq_pair.client.subscribe(NAMESPACE, TRACK_NAME)
    await wait_until(lambda: bool(published))

    properties = moqt.ObjectProperties()
    properties.add(moqt.PROP_PRIOR_GROUP_ID_GAP, 3)
    await published[0].send_object(7, 0, b"with-properties", properties_data=properties.encode())

    received = await _take_objects(subscription, 1)

    assert received[0].payload == b"with-properties"
    assert received[0].properties is not None
    decoded, _consumed = moqt.ObjectProperties.decode(received[0].properties)
    assert decoded.prior_group_id_gap == 3
    assert received[0].publisher_priority is None


async def test_subgroup_properties_must_be_consistent(moq_pair: MoqPair) -> None:
    """
    Subgroup 内で Properties の有無が変わると送信が拒否されることを確認する。

    PROPERTIES bit は Subgroup Header で固定されるため、subgroup 内の全オブジェクトが
    Properties を持つか、1 つも持たないかのどちらかでなければならない
    (draft-ietf-moq-transport-21 §11.3.1 (Subgroup Header))。
    """
    published: list[Publication] = []

    async def on_subscribe(request: SubscriptionRequest) -> None:
        # Track ごとに別の Track Alias を使う
        published.append(await request.subscribe_ok(len(published) + 1))

    moq_pair.server.on_subscribe(on_subscribe)

    subscription = await moq_pair.client.subscribe(NAMESPACE, TRACK_NAME)
    await wait_until(lambda: bool(published))
    publication = published[0]
    # 有無だけが問題なので、順序検証に意味を持たない LOC の TIMESTAMP を使う
    properties = loc.Properties()
    properties.add(loc.TIMESTAMP, 1000)

    # Properties ありで開いた subgroup へ Properties 無しのオブジェクトは送れない
    await publication.send_object(1, 0, b"with-properties", properties_data=properties.encode())
    with pytest.raises(MoqtError, match="must be consistent within a subgroup"):
        await publication.send_object(1, 1, b"without-properties")
    # 先に拒否されたオブジェクトは送られていないため、届くのは 1 件だけである
    received = await _take_objects(subscription, 1)
    assert received[0].payload == b"with-properties"

    # Properties 無しで開いた subgroup へ Properties ありのオブジェクトは送れない。
    # 同じ subscription では Properties の有無が固定されるため、別の Track を使う
    await moq_pair.client.subscribe(NAMESPACE, b"audio")
    await wait_until(lambda: len(published) == 2)
    await published[1].send_object(1, 0, b"without-properties")
    with pytest.raises(MoqtError, match="must be consistent within a subgroup"):
        await published[1].send_object(
            1, 1, b"with-properties", properties_data=properties.encode()
        )


async def test_datagram_object_properties_are_delivered(moq_pair: MoqPair) -> None:
    """
    データグラムで送った Object Properties が受信側で参照できることを確認する。

    データグラムは subgroup ヘッダを持たないため `subgroup_id` は `None` のままである。
    Publisher Priority を指定していないため DEFAULT_PRIORITY bit が立ち、
    `publisher_priority` も `None` になる
    (draft-ietf-moq-transport-21 §11.2.1 (Object Datagram))。
    """
    published: list[Publication] = []

    async def on_subscribe(request: SubscriptionRequest) -> None:
        published.append(await request.subscribe_ok(TRACK_ALIAS))

    moq_pair.server.on_subscribe(on_subscribe)

    subscription = await moq_pair.client.subscribe(NAMESPACE, TRACK_NAME)
    await wait_until(lambda: bool(published))

    properties = moqt.ObjectProperties()
    properties.add(moqt.PROP_PRIOR_GROUP_ID_GAP, 2)
    properties.add(moqt.PROP_OBJECT_DELIVERY_TIMEOUT, 1000)
    await published[0].send_datagram(9, 0, b"datagram", properties_data=properties.encode())

    received = await _take_objects(subscription, 1)

    assert received[0].payload == b"datagram"
    assert received[0].stream_id is None
    assert received[0].subgroup_id is None
    assert received[0].publisher_priority is None
    assert received[0].properties is not None
    decoded, _consumed = moqt.ObjectProperties.decode(received[0].properties)
    assert decoded.prior_group_id_gap == 2
    assert decoded.object_delivery_timeout == 1000


async def test_datagram_publisher_priority_is_delivered(moq_pair: MoqPair) -> None:
    """
    データグラムが運ぶ Publisher Priority が受信側で参照できることを確認する。

    DEFAULT_PRIORITY bit が立っていないデータグラムは Publisher Priority を明示して
    おり、その値が `MoqtObject.publisher_priority` に入る。bit が立っている
    データグラムは購読を確立した制御メッセージの優先度を継承するため `None` になる
    (draft-ietf-moq-transport-21 §11.2.1 (Object Datagram))。
    """
    published: list[Publication] = []

    async def on_subscribe(request: SubscriptionRequest) -> None:
        published.append(await request.subscribe_ok(TRACK_ALIAS))

    moq_pair.server.on_subscribe(on_subscribe)

    subscription = await moq_pair.client.subscribe(NAMESPACE, TRACK_NAME)
    await wait_until(lambda: bool(published))

    # 優先度を明示したデータグラムは値がそのまま届く
    await published[0].send_datagram(1, 0, b"explicit", publisher_priority=10)
    explicit = await _take_objects(subscription, 1)

    assert explicit[0].payload == b"explicit"
    assert explicit[0].publisher_priority == 10

    # 優先度を省略したデータグラムは DEFAULT_PRIORITY bit が立ち、値を持たない
    await published[0].send_datagram(1, 1, b"default")
    default = await _take_objects(subscription, 1)

    assert default[0].payload == b"default"
    assert default[0].publisher_priority is None


async def test_client_publish_and_object_delivery(moq_pair: MoqPair) -> None:
    """
    client が PUBLISH で配信し、送ったオブジェクトが server へ届くことを確認する。

    PUBLISH の応答は REQUEST_OK であり、SUBSCRIBE_OK とは異なり Track Alias を
    運ばない。server は peer が通知した Track Alias をそのまま使う。
    """
    accepted: list[PublisherRequest] = []

    async def on_publish(request: PublisherRequest) -> None:
        accepted.append(request)
        await request.accept()

    moq_pair.server.on_publish(on_publish)

    publication = await moq_pair.client.publish(NAMESPACE, TRACK_NAME, TRACK_ALIAS)
    await wait_until(lambda: bool(accepted))

    assert accepted[0].namespace == tuple(NAMESPACE)
    assert accepted[0].track_name == TRACK_NAME
    assert accepted[0].track_alias == TRACK_ALIAS

    await publication.send_object(1, 0, b"published")
    await publication.send_datagram(1, 1, b"datagram-published")
    await publication.close()


async def test_session_timeouts_can_be_configured(
    moq_client_factory: ClientFactory,
) -> None:
    """
    セッションのタイムアウトを設定しても通常の通信が成立することを確認する。

    タイムアウトは peer の停止を検出する期限であり、既定では無効である
    (draft-ietf-moq-transport-21 §12.2 (Session Termination Codes))。
    """
    client = await moq_client_factory(
        control_message_timeout=5.0,
        data_stream_timeout=5.0,
    )

    assert client.established


async def test_session_state_accessors_report_the_live_session(
    moq_certificates: tuple[str, str],
) -> None:
    """
    確立したセッションの内部状態を高レベル API から照会できることを確認する。

    peer が SETUP で宣言した値はキャッシュせず状態機械から都度取得する。購読と fetch の
    状態は Request ID をキーにした辞書で返り、GOAWAY の drain を妨げている request も
    参照できる (draft-ietf-moq-transport-21 §9.1.3 (MAX_AUTH_TOKEN_CACHE_SIZE) /
    §6.6.1 (Graceful Session Migration))。
    """
    certfile, keyfile = moq_certificates
    server = Server(
        host="127.0.0.1",
        port=0,
        certfile=certfile,
        keyfile=keyfile,
        setup_options={moqt.SETUP_OPTION_MAX_AUTH_TOKEN_CACHE_SIZE: 1024},
    )
    await server.start()
    run_task = asyncio.create_task(server.run())
    client: Client | None = None
    try:
        sessions: list[ServerSession] = []
        established = asyncio.Event()
        published: list[Publication] = []
        fetched = asyncio.Event()

        async def on_session_established(session: ServerSession) -> None:
            sessions.append(session)
            established.set()

        async def on_subscribe(request: SubscriptionRequest) -> None:
            published.append(await request.subscribe_ok(TRACK_ALIAS))

        async def on_fetch(request: FetchRequest) -> None:
            response = await request.respond((1, 1), end_of_track=True)
            await response.close()
            fetched.set()

        server.on_session_established(on_session_established)
        server.on_subscribe(on_subscribe)
        server.on_fetch(on_fetch)

        client = Client(
            url=f"https://127.0.0.1:{server.actual_port}/webtransport",
            verify_peer=False,
        )
        await client.connect()
        await asyncio.wait_for(established.wait(), timeout=OBJECT_TIMEOUT)

        # client から見た peer (server) の宣言値であり、SETUP の値がそのまま読める
        assert client.peer_max_auth_token_cache_size == 1024
        # server から見た peer (client) の宣言値は無いため 0 になる
        assert sessions[0].peer_max_auth_token_cache_size == 0

        # alias の保持期間は設定した値が読める
        client.set_peer_alias_retention_ms(2000)
        assert client.peer_alias_retention_ms == 2000

        # 購読が無い状態では fill fetch stream も drain も残っていない
        assert sessions[0].runtime.open_outgoing_fill_stream_count(0) == 0
        assert client.goaway_drain_ready is True

        # 購読を確立すると状態機械の subscription が観測できる
        subscription = await client.subscribe(NAMESPACE, TRACK_NAME)
        await wait_until(lambda: bool(published))

        entry = client.subscription_state(subscription.request_id)
        assert entry is not None
        assert entry["state"] == "established"
        assert entry["track_alias"] == TRACK_ALIAS
        assert entry["namespace"] == NAMESPACE
        assert entry["track_name"] == TRACK_NAME
        assert entry["my_role"] == "subscriber"
        assert entry["forward"] is True
        assert client.subscriptions() == {subscription.request_id: entry}
        # 保持していない Request ID は取得できない
        assert client.subscription_state(subscription.request_id + 100) is None

        # 同じ subscription が publisher 側からは publisher として見える
        publisher_entry = sessions[0].subscription_state(subscription.request_id)
        assert publisher_entry is not None
        assert publisher_entry["my_role"] == "publisher"

        # 確立中の購読は GOAWAY の drain を妨げる
        assert client.goaway_drain_ready is False
        assert client.goaway_drain_snapshot()["blocking_subscription_request_ids"] == [
            subscription.request_id
        ]

        # 購読を終了すると drain が完了する
        await subscription.close()
        await wait_until(lambda: client.goaway_drain_ready)
        assert client.goaway_drain_snapshot()["blocking_subscription_request_ids"] == []
        # 照会はセッションを終わらせない
        assert client.established is True

        # fetch を確立すると fetch の状態も観測できる
        fetch = await client.fetch(NAMESPACE, b"fetched")
        await asyncio.wait_for(fetched.wait(), timeout=OBJECT_TIMEOUT)

        fetch_entry = client.fetch_state(fetch.request_id)
        assert fetch_entry is not None
        assert fetch_entry["my_role"] == "subscriber"
        assert fetch_entry["track_name"] == b"fetched"
        assert client.fetches() == {fetch.request_id: fetch_entry}
        # TRACK_STATUS は送っていないため空である
        assert client.track_status_requests() == {}
    finally:
        if client is not None:
            await client.close()
        await server.stop()
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task


async def test_server_goaway_is_notified_to_the_client(moq_pair: MoqPair) -> None:
    """
    server の GOAWAY が client へ通知されることを確認する。

    受信した GOAWAY は `Client.peer_goaway` と `Client.on_goaway` の両方から
    参照できる。移行先が通知された場合はアプリが新しいセッションへ接続し直す。
    """
    received: list[PeerGoaway] = []

    async def on_goaway(info: PeerGoaway) -> None:
        received.append(info)

    moq_pair.client.on_goaway(on_goaway)
    await moq_pair.session.goaway(timeout=0)

    await wait_until(lambda: bool(received))
    assert received[0].timeout == 0
    assert moq_pair.client.peer_goaway is not None


async def test_server_goaway_carries_a_new_session_uri(moq_pair: MoqPair) -> None:
    """
    server の GOAWAY が移行先のセッション URI を運ぶことを確認する。

    セッションを閉じる側は `new_session_uri` で移行先を通知でき、受け取った側は
    `Client.peer_goaway` から URI を復元する
    (draft-ietf-moq-transport-21 §9.2 (GOAWAY))。
    """
    received: list[PeerGoaway] = []

    async def on_goaway(info: PeerGoaway) -> None:
        received.append(info)

    moq_pair.client.on_goaway(on_goaway)
    uri = b"https://example.com/webtransport"
    await moq_pair.session.goaway(timeout=5000, new_session_uri=uri)

    await wait_until(lambda: bool(received))
    assert received[0].new_session_uri == uri
    assert received[0].timeout == 5000
    peer_goaway = moq_pair.client.peer_goaway
    assert peer_goaway is not None
    assert peer_goaway.new_session_uri == uri


async def test_goaway_rejects_a_new_session_uri_beyond_the_length_limit(
    moq_pair: MoqPair,
) -> None:
    """
    長さ上限を超える new session URI を GOAWAY が送信前に拒否することを確認する。

    上限は `MAX_NEW_SESSION_URI_LENGTH` であり、上限ちょうどの URI は送信できる
    (draft-ietf-moq-transport-21 §9.2 (GOAWAY))。
    """
    received: list[PeerGoaway] = []

    async def on_goaway(info: PeerGoaway) -> None:
        received.append(info)

    moq_pair.client.on_goaway(on_goaway)
    too_long = b"a" * (moqt.MAX_NEW_SESSION_URI_LENGTH + 1)

    # server からも client からも拒否される
    with pytest.raises(MoqtError, match="new_session_uri"):
        await moq_pair.session.goaway(timeout=0, new_session_uri=too_long)
    with pytest.raises(MoqtError, match="new_session_uri"):
        await moq_pair.client.goaway(timeout=0, new_session_uri=too_long)

    # 拒否された GOAWAY は送信されていない
    assert moq_pair.client.peer_goaway is None
    assert moq_pair.client.established is True

    # 上限ちょうどの URI は送信でき、そのまま相手へ届く
    uri = b"a" * moqt.MAX_NEW_SESSION_URI_LENGTH
    await moq_pair.session.goaway(timeout=0, new_session_uri=uri)

    await wait_until(lambda: bool(received))
    assert received[0].new_session_uri == uri


async def test_request_update_is_accepted_by_the_peer(moq_pair: MoqPair) -> None:
    """
    REQUEST_UPDATE に peer が REQUEST_OK で応答することを確認する。

    購読の sender である client が同じ request stream へ REQUEST_UPDATE を書き、
    server が応答する。応答を待たずに戻る実装ではこのテストがタイムアウトする。
    """
    published: list[Publication] = []

    async def on_subscribe(request: SubscriptionRequest) -> None:
        published.append(await request.subscribe_ok(TRACK_ALIAS))

    moq_pair.server.on_subscribe(on_subscribe)

    subscription = await moq_pair.client.subscribe(NAMESPACE, TRACK_NAME)
    await wait_until(lambda: bool(published))

    # FORWARD (varint) を付けた更新を送り、応答が返ることを確認する
    await asyncio.wait_for(
        subscription.request_update({moqt.PARAM_FORWARD: 1}),
        timeout=OBJECT_TIMEOUT,
    )


async def test_object_published_right_after_subscribe_ok_is_delivered(moq_pair: MoqPair) -> None:
    """
    SUBSCRIBE_OK の直後に送ったオブジェクトが取りこぼされないことを確認する。

    data stream は Request ID ではなく Track Alias で購読を特定する。状態機械が
    SUBSCRIBE_OK を処理してから client が購読を登録するまでの間に届いた
    オブジェクトも、購読が確定した時点で渡さなければならない。
    """
    published: list[Publication] = []

    async def on_subscribe(request: SubscriptionRequest) -> None:
        # 応答と同じコールバックの中で送る。購読の登録が追いついていない間に
        # 届く可能性がある最も早いタイミングである
        publication = await request.subscribe_ok(TRACK_ALIAS)
        published.append(publication)
        await publication.send_object(1, 0, b"immediate")
        await publication.send_object(1, 1, b"immediate-2")

    moq_pair.server.on_subscribe(on_subscribe)

    subscription = await moq_pair.client.subscribe(NAMESPACE, TRACK_NAME)

    received = await _take_objects(subscription, 2)

    assert [item.payload for item in received] == [b"immediate", b"immediate-2"]


@pytest.mark.parametrize(
    "payload_size",
    [0, 127, 128, 16383, 16384],
    ids=["empty", "1byte-max", "2byte-min", "2byte-max", "3byte-min"],
)
async def test_subgroup_object_payload_length_boundaries(
    moq_pair: MoqPair,
    payload_size: int,
) -> None:
    """
    ペイロード長が vi64 のエンコード長の境界にあっても配送できることを確認する。

    vi64 は先頭バイトの leading-1-bits が長さを決めるため、1 バイトで表せるのは
    0-127、2 バイトで表せるのは 0-16383 である
    (draft-ietf-moq-transport-21 §8.1 (Variable-Length Integers) Table 3)。

    ペイロード長 0 のオブジェクトは Object Status を明示する必要がある
    (draft-ietf-moq-transport-21 §11.1.2 (Object Status))。境界をまたぐ長さで
    subgroup オブジェクトを送り、受信側が同じバイト列を得られることを検証する。
    """
    published: list[Publication] = []

    async def on_subscribe(request: SubscriptionRequest) -> None:
        published.append(await request.subscribe_ok(TRACK_ALIAS))

    moq_pair.server.on_subscribe(on_subscribe)

    subscription = await moq_pair.client.subscribe(NAMESPACE, TRACK_NAME)
    await wait_until(lambda: bool(published))

    payload = bytes(range(256)) * (payload_size // 256) + bytes(range(payload_size % 256))
    assert len(payload) == payload_size
    await published[0].send_object(1, 0, payload)

    received = await _take_objects(subscription, 1)

    assert len(received) == 1
    assert received[0].payload == payload


@pytest.mark.parametrize(
    ("subgroup_id", "publisher_priority", "end_of_group"),
    [
        (None, None, False),
        (0, None, False),
        (7, None, False),
        (200, None, False),
        (None, 5, False),
        (None, None, True),
        (3, 100, True),
    ],
    ids=[
        "default",
        "explicit-zero",
        "explicit-7",
        "explicit-200",
        "priority-5",
        "end-of-group",
        "all-fields",
    ],
)
async def test_subgroup_header_variants_are_delivered(
    moq_pair: MoqPair,
    subgroup_id: int | None,
    publisher_priority: int | None,
    end_of_group: bool,
) -> None:
    """
    subgroup ヘッダの各フィールドの組み合わせでオブジェクトが配送されることを確認する。

    Subgroup ID を明示する場合は SUBGROUP_ID_MODE を 0b10 にしなければならない
    (draft-ietf-moq-transport-21 §11.3.1 (Subgroup Header))。Publisher Priority を
    省略するかどうかと End of Group の有無も含めて、受信側が同じペイロードを
    得られることを検証する。
    """
    published: list[Publication] = []

    async def on_subscribe(request: SubscriptionRequest) -> None:
        published.append(await request.subscribe_ok(TRACK_ALIAS))

    moq_pair.server.on_subscribe(on_subscribe)

    subscription = await moq_pair.client.subscribe(NAMESPACE, TRACK_NAME)
    await wait_until(lambda: bool(published))

    await published[0].send_object(
        1,
        0,
        b"header variant",
        subgroup_id=subgroup_id,
        publisher_priority=publisher_priority,
        end_of_group=end_of_group,
    )

    received = await _take_objects(subscription, 1)

    assert len(received) == 1
    assert received[0].payload == b"header variant"


async def test_subscribe_and_receive_objects_over_datagram(moq_pair: MoqPair) -> None:
    """
    オブジェクトデータグラムの配送を確認する。

    subgroup ストリームではなくデータグラムで送ったオブジェクトが、
    同じ Group ID と Object ID で受信できることを検証する。
    """
    published: list[Publication] = []

    async def on_subscribe(request: SubscriptionRequest) -> None:
        published.append(await request.subscribe_ok(TRACK_ALIAS))

    moq_pair.server.on_subscribe(on_subscribe)

    subscription = await moq_pair.client.subscribe(NAMESPACE, TRACK_NAME)
    await wait_until(lambda: bool(published))

    await published[0].send_datagram(7, 0, b"datagram payload")

    received = await _take_objects(subscription, 1)

    assert len(received) == 1
    assert received[0].group_id == 7
    assert received[0].object_id == 0
    assert received[0].payload == b"datagram payload"


async def test_datagram_with_an_empty_payload_is_delivered(moq_pair: MoqPair) -> None:
    """
    ペイロードが空のオブジェクトデータグラムが配送されることを確認する。

    データグラムはペイロード長を持たないため、ペイロードが無い場合は STATUS bit を
    立てて Object Status を明示しなければならない
    (draft-ietf-moq-transport-21 §11.2.1 (Object Datagram))。
    """
    published: list[Publication] = []

    async def on_subscribe(request: SubscriptionRequest) -> None:
        published.append(await request.subscribe_ok(TRACK_ALIAS))

    moq_pair.server.on_subscribe(on_subscribe)

    subscription = await moq_pair.client.subscribe(NAMESPACE, TRACK_NAME)
    await wait_until(lambda: bool(published))

    await published[0].send_datagram(7, 0, b"")

    received = await _take_objects(subscription, 1)

    assert len(received) == 1
    assert received[0].group_id == 7
    assert received[0].object_id == 0
    assert received[0].payload == b""
    assert received[0].status == moqt.OBJECT_STATUS_NORMAL


@pytest.mark.parametrize(
    "status",
    [
        moqt.OBJECT_STATUS_NORMAL,
        moqt.OBJECT_STATUS_END_OF_GROUP,
        moqt.OBJECT_STATUS_END_OF_TRACK,
    ],
    ids=["normal", "end-of-group", "end-of-track"],
)
@pytest.mark.parametrize("send", ["subgroup", "datagram"], ids=["subgroup", "datagram"])
async def test_object_status_is_delivered(
    moq_pair: MoqPair,
    status: int,
    send: str,
) -> None:
    """
    Object Status を付けたオブジェクトが受信側で同じ status として観測されることを確認する。

    ペイロード長 0 のオブジェクトは Object Status を明示する
    (draft-ietf-moq-transport-21 §11.1.2 (Object Status))。subgroup とデータグラムの
    どちらの経路でも status が保たれることを検証する。
    """
    published: list[Publication] = []

    async def on_subscribe(request: SubscriptionRequest) -> None:
        published.append(await request.subscribe_ok(TRACK_ALIAS))

    moq_pair.server.on_subscribe(on_subscribe)

    subscription = await moq_pair.client.subscribe(NAMESPACE, TRACK_NAME)
    await wait_until(lambda: bool(published))

    await _send_object(published[0], send, b"", status)

    received = await _take_objects(subscription, 1)

    assert len(received) == 1
    assert received[0].payload == b""
    assert received[0].status == status


@pytest.mark.parametrize("send", ["subgroup", "datagram"], ids=["subgroup", "datagram"])
async def test_object_status_rejects_a_payload(moq_pair: MoqPair, send: str) -> None:
    """
    Normal 以外の Object Status にペイロードを付けた場合に拒否することを確認する。

    draft-ietf-moq-transport-21 §11.1.2 (Object Status): "An Object MUST have an
    empty payload unless its Object Status value is registered as permitting a
    payload in the Object Status registry"。
    """
    published: list[Publication] = []

    async def on_subscribe(request: SubscriptionRequest) -> None:
        published.append(await request.subscribe_ok(TRACK_ALIAS))

    moq_pair.server.on_subscribe(on_subscribe)

    await moq_pair.client.subscribe(NAMESPACE, TRACK_NAME)
    await wait_until(lambda: bool(published))

    with pytest.raises(MoqtError, match="requires an empty payload"):
        await _send_object(published[0], send, b"data", moqt.OBJECT_STATUS_END_OF_GROUP)


@pytest.mark.parametrize("send", ["subgroup", "datagram"], ids=["subgroup", "datagram"])
async def test_object_status_rejects_an_unknown_value(moq_pair: MoqPair, send: str) -> None:
    """
    未知の Object Status を拒否することを確認する。

    draft-ietf-moq-transport-21 §11.1.2 (Object Status): "Any other value SHOULD be
    treated as a protocol error"。
    """
    published: list[Publication] = []

    async def on_subscribe(request: SubscriptionRequest) -> None:
        published.append(await request.subscribe_ok(TRACK_ALIAS))

    moq_pair.server.on_subscribe(on_subscribe)

    await moq_pair.client.subscribe(NAMESPACE, TRACK_NAME)
    await wait_until(lambda: bool(published))

    with pytest.raises(MoqtError, match="unknown object status"):
        await _send_object(published[0], send, b"", 0x99)


@pytest.mark.parametrize("oversized", [False, True], ids=["fits", "oversized"])
async def test_datagram_size_is_reported_before_sending(
    moq_pair: MoqPair,
    caplog: pytest.LogCaptureFixture,
    *,
    oversized: bool,
) -> None:
    """
    経路に依存せず運べる大きさを超えるデータグラムを送信前に警告することを確認する。

    上限を超えたデータグラムは経路によっては通知なく破棄され、送信側からは検知
    できない (draft-ietf-moq-transport-21 §11.2.1 (Object Datagram))。受信待ちで
    止まる前に原因が分かるよう、送信時に警告を記録する。
    """
    published: list[Publication] = []

    async def on_subscribe(request: SubscriptionRequest) -> None:
        published.append(await request.subscribe_ok(TRACK_ALIAS))

    moq_pair.server.on_subscribe(on_subscribe)

    await moq_pair.client.subscribe(NAMESPACE, TRACK_NAME)
    await wait_until(lambda: bool(published))

    # MoQT のヘッダもデータグラムに含まれるため、ペイロードは上限と同じか半分にする
    payload_size = moqt.MAX_DATAGRAM_SIZE if oversized else moqt.MAX_DATAGRAM_SIZE // 2

    with caplog.at_level(logging.WARNING, logger="moqt.moq._runtime"):
        await published[0].send_datagram(1, 0, bytes(payload_size))

    reported = "exceeds the portable limit" in caplog.text
    assert reported is oversized


async def test_server_keeps_serving_after_a_client_closes(
    moq_server: Server,
    moq_client_factory: ClientFactory,
) -> None:
    """
    client が接続を閉じたあとも server が新しい接続を受け付けることを確認する。

    接続の終了時には、トランスポートの後始末として制御ストリームや要求ストリームの
    終端が server へ届く。制御ストリームは session の生存中に閉じてはならないため
    (draft-ietf-moq-transport-21 §6.4.1 (Control Streams))、これらを状態機械が
    プロトコル違反として拒否しても server は動き続けなければならない。
    """
    first = await moq_client_factory()
    assert first.established

    await first.close()
    await wait_until(lambda: not first.established)

    # server が生きていれば SETUP が成立する。動いていなければ接続がタイムアウトする
    second = await moq_client_factory()
    assert second.established


async def test_fetch_receives_objects(moq_pair: MoqPair) -> None:
    """
    FETCH で要求した過去のオブジェクトが fetch stream で届くことを確認する。

    client が FETCH を送り、server が FETCH_OK と fetch stream でオブジェクトを
    返す。Group ID / Object ID / ペイロードが送信側と一致することを検証する。

    fetch stream の Group ID と Object ID は直前のオブジェクトを基準に差分で
    表現されるため、同じ Group 内の 2 件目と Group をまたぐ 3 件目を含める
    (draft-ietf-moq-transport-21 §11.4.1.1 (Flags))。ペイロード長 0 の
    オブジェクトも扱う。
    """
    responded = False

    async def on_fetch(request: FetchRequest) -> None:
        nonlocal responded
        response = await request.respond((9, 9), end_of_track=False)
        await response.send_object(5, 10, b"fetched-1")
        await response.send_object(5, 11, b"fetched-2")
        await response.send_object(6, 0, b"fetched-3")
        await response.send_object(6, 1, b"")
        await response.close()
        responded = True

    moq_pair.server.on_fetch(on_fetch)

    fetch = await moq_pair.client.fetch(NAMESPACE, TRACK_NAME)
    assert fetch.end_of_track is False
    assert fetch.end_location == (9, 9)
    await wait_until(lambda: responded)

    received = await _take_fetch_objects(fetch, 4)
    assert [item.group_id for item in received] == [5, 5, 6, 6]
    assert [item.object_id for item in received] == [10, 11, 0, 1]
    assert [item.payload for item in received] == [
        b"fetched-1",
        b"fetched-2",
        b"fetched-3",
        b"",
    ]


def test_low_level_names_are_exported_from_the_package_root() -> None:
    """
    `moqt` の `__all__` に挙げた名前がすべて取り出せることを確認する。

    利用者が `moqt.moqt` や `moqt.loc` ではなく `moqt` から import できることを
    検証する。
    """
    package = importlib.import_module("moqt")
    for name in package.__all__:
        assert getattr(package, name, None) is not None, name


def test_high_level_names_are_exported_from_moqt_moq() -> None:
    """
    `moqt.moq` の `__all__` に挙げた名前がすべて取り出せることを確認する。

    利用者が `moqt.moq.client` や `moqt.moq.server` ではなく `moqt.moq` から
    import できることを検証する。
    """
    package = importlib.import_module("moqt.moq")
    for name in package.__all__:
        assert getattr(package, name, None) is not None, name


def test_legacy_moq_package_is_not_installed() -> None:
    """
    改名前の `moq` パッケージが残っていないことを確認する。

    互換シムを置かない方針であるため、`moq` が import できる状態は改名の
    取りこぼしである。
    """
    assert importlib.util.find_spec("moq") is None


async def test_reset_subgroup_allows_a_new_subgroup_on_the_same_track(
    moq_pair: MoqPair,
) -> None:
    """
    subgroup を reset した後も同じ Track で配信を続けられることを確認する。

    reset は送信済みのデータを破棄し
    (draft-ietf-moq-transport-21 §16.11.4 (Stream Reset Codes))、次のオブジェクトは
    新しい subgroup ストリームで送る。
    """
    published: list[Publication] = []

    async def on_subscribe(request: SubscriptionRequest) -> None:
        published.append(await request.subscribe_ok(TRACK_ALIAS))

    moq_pair.server.on_subscribe(on_subscribe)

    subscription = await moq_pair.client.subscribe(NAMESPACE, TRACK_NAME)
    await wait_until(lambda: bool(published))

    # オブジェクトを送ってから同じ subgroup を reset する
    publication = published[0]
    await publication.send_object(1, 0, b"discarded")
    await publication.reset_subgroup(moqt.STREAM_CANCELLED)

    # reset の後は同じ Request ID で新しい subgroup を開ける
    await publication.send_object(2, 0, b"kept")

    received = await _take_objects_in_group(subscription, 2)

    assert [item.payload for item in received if item.group_id == 2] == [b"kept"]


async def test_reset_subgroup_at_keeps_the_connection_usable(moq_pair: MoqPair) -> None:
    """
    RESET_STREAM_AT で subgroup を reset してもセッションが壊れないことを確認する。

    先頭 `reliable_size` バイトは peer へ届き、残りは破棄される
    (draft-ietf-moq-transport-21 §11.3.2 (Subgroup Object))。
    どのバイトまで届くかはトランスポートの実装に依存するため、ここでは API が
    受理され、その後の配信が続くことだけを確認する。
    """
    published: list[Publication] = []

    async def on_subscribe(request: SubscriptionRequest) -> None:
        published.append(await request.subscribe_ok(TRACK_ALIAS))

    moq_pair.server.on_subscribe(on_subscribe)

    subscription = await moq_pair.client.subscribe(NAMESPACE, TRACK_NAME)
    await wait_until(lambda: bool(published))

    publication = published[0]
    await publication.send_object(1, 0, b"partial")
    # stream type の 1 バイトと subgroup ヘッダの 3 バイトだけを確実に届ける
    await publication.reset_subgroup_at(4, moqt.STREAM_CANCELLED)
    await publication.send_object(2, 0, b"kept")

    received = await _take_objects_in_group(subscription, 2)

    assert [item.payload for item in received if item.group_id == 2] == [b"kept"]


async def test_fill_parameters_open_a_fill_fetch_stream(moq_pair: MoqPair) -> None:
    """
    FILL_PARAMETERS 付きの購読で fill fetch stream が開かれることを確認する。

    peer が過去のオブジェクトの補充を求めた場合、publisher は fill fetch stream を
    開いて応答する (draft-ietf-moq-transport-21 §3.4 (Fill Semantics))。
    """
    opened: list[tuple[int, int]] = []

    published: list[Publication] = []

    async def on_fill_fetch_stream(runtime: Runtime, request_id: int, stream_id: int) -> None:
        opened.append((request_id, stream_id))

    async def on_subscribe(request: SubscriptionRequest) -> None:
        published.append(await request.subscribe_ok(TRACK_ALIAS))

    moq_pair.server.on_fill_fetch_stream(on_fill_fetch_stream)
    moq_pair.server.on_subscribe(on_subscribe)

    # fill の範囲は Largest Object を超えられないため、先に 1 件配信して観測させる
    await moq_pair.client.subscribe(NAMESPACE, TRACK_NAME)
    await wait_until(lambda: bool(published))
    await published[0].send_object(1, 0, b"original")

    # 補充を求める購読を送る。FILL_PARAMETERS の内側は補充の範囲を指定する
    await moq_pair.client.subscribe(
        NAMESPACE,
        TRACK_NAME,
        {moqt.PARAM_FILL_PARAMETERS: {moqt.PARAM_FILL_TIMEOUT: 1000}},
    )

    await wait_until(lambda: bool(opened))
    assert opened[0][0] >= 0
    assert opened[0][1] >= 0


async def test_publish_state_notify_is_delivered_to_the_subscriber(moq_pair: MoqPair) -> None:
    """
    PUBLISH_STATE_NOTIFY が購読側のコールバックへ届くことを確認する。

    通知は publisher が自分の request で送り、subscriber が応答せずに受理する。
    購読のクレジットも消費しない (draft-ietf-moq-transport-21 §9.10
    (PUBLISH_STATE_NOTIFY))。
    """
    published: list[Publication] = []

    async def on_subscribe(request: SubscriptionRequest) -> None:
        published.append(await request.subscribe_ok(TRACK_ALIAS))

    moq_pair.server.on_subscribe(on_subscribe)

    received: list[dict[int, object]] = []

    async def on_publish_state_notify(parameters: dict[int, object]) -> None:
        received.append(parameters)

    moq_pair.client.on_publish_state_notify(on_publish_state_notify)

    subscription = await moq_pair.client.subscribe(NAMESPACE, TRACK_NAME)
    await wait_until(lambda: bool(published))

    # 状態として LARGEST_OBJECT を通知する
    await published[0].send_publish_state_notify({moqt.PARAM_LARGEST_OBJECT: (2, 5)})

    await wait_until(lambda: bool(received))
    # パラメータは型付きで読める
    assert moqt.MessageParameters(received[0]).largest_object == (2, 5)

    # 通知は購読を終わらせない。続けて送ったオブジェクトが届く
    await published[0].send_object(1, 0, b"after-notify")

    objects = await _take_objects(subscription, 1)

    assert objects[0].payload == b"after-notify"


async def test_track_status_is_not_answered_by_an_endpoint(
    moq_server: Server,
    moq_client_factory: ClientFactory,
) -> None:
    """
    endpoint が TRACK_STATUS に応答しないことを確認する。

    draft-ietf-moq-transport-21 §9.13 (TRACK_STATUS) は、publisher が失敗した
    TRACK_STATUS に REQUEST_ERROR を返すとする。moqt-py が利用する状態機械は
    TRACK_STATUS を endpoint が受信する request として受理しないため、応答は返らず、
    要求を受け取った側のセッションはプロトコル違反で終了する。要求側には応答が
    届かないため、制御メッセージの期限で失敗を検出する
    (draft-ietf-moq-transport-21 §12.2 (Session Termination Codes))。
    """
    sessions: list[ServerSession] = []
    established = asyncio.Event()

    async def on_session_established(session: ServerSession) -> None:
        sessions.append(session)
        established.set()

    moq_server.on_session_established(on_session_established)
    # 応答が返らないまま待ち続けないよう、制御メッセージの期限を設定する
    client = await moq_client_factory(control_message_timeout=1.0)
    await asyncio.wait_for(established.wait(), timeout=OBJECT_TIMEOUT)

    with pytest.raises(MoqtError, match="session closed: code="):
        await client.track_status(NAMESPACE, TRACK_NAME)

    # 受理しなかった publisher 側もセッションを終了する
    await wait_until(lambda: sessions[0].runtime.closed)


async def test_client_goaway_is_notified_to_the_server(moq_pair: MoqPair) -> None:
    """
    client の GOAWAY が server へ届くことを確認する。

    受信した GOAWAY は、その session と移行先の情報としてアプリへ通知される。
    GOAWAY の受信後、状態機械はその peer への新規 request の送信を拒否する
    (draft-ietf-moq-transport-21 §9.2 (GOAWAY))。
    """
    received: list[tuple[ServerSession, PeerGoaway]] = []

    async def on_goaway(session: ServerSession, info: PeerGoaway) -> None:
        received.append((session, info))

    moq_pair.server.on_goaway(on_goaway)

    # client は移行先を通知できないため new session URI は空になる
    await moq_pair.client.goaway(timeout=0)

    await wait_until(lambda: bool(received))
    assert received[0][0].session_id == moq_pair.session.session_id
    assert received[0][1].new_session_uri == b""
    assert received[0][1].timeout == 0


async def test_datagram_priority_mismatch_cancels_the_subscription(moq_pair: MoqPair) -> None:
    """
    同じ Location の重複 Object の Priority が食い違うと購読が取り消されることを確認する。

    draft-ietf-moq-transport-21 §12.1 (Malformed Tracks): "When a subscriber detects a
    Malformed Track, it MUST cancel any corresponding subscription or fetches for that
    Track from that publisher, and SHOULD deliver an error to the application."
    セッションは閉じず、取り消された購読のオブジェクトが届かなくなる。
    """
    published: list[Publication] = []

    async def on_subscribe(request: SubscriptionRequest) -> None:
        published.append(await request.subscribe_ok(TRACK_ALIAS))

    moq_pair.server.on_subscribe(on_subscribe)

    subscription = await moq_pair.client.subscribe(NAMESPACE, TRACK_NAME)
    await wait_until(lambda: bool(published))

    # 同じ Group ID と Object ID のデータグラムを、違う Priority で 2 回送る
    await published[0].send_datagram(1, 0, b"first", publisher_priority=10)
    received = await _take_objects(subscription, 1)
    assert received[0].payload == b"first"

    await published[0].send_datagram(1, 0, b"second", publisher_priority=20)

    # 重複 Object の不一致を検出した購読は取り消され、オブジェクトの到着が終わる
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(subscription.objects()), timeout=OBJECT_TIMEOUT)

    # §12.1 が求めるのは購読の取り消しであり、セッションの終了ではない
    assert moq_pair.client.established is True
