"""WebTransport over HTTP/3 を利用する MoQT client。"""

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from types import TracebackType
from typing import TYPE_CHECKING

from webtransport import h3

from moqt.moq._runtime import (
    TICK_INTERVAL,
    MessageBody,
    MoqtError,
    NativeEvent,
    Runtime,
    RuntimeEvents,
    TransportOps,
)
from moqt.moq.publisher import Publication

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

logger = logging.getLogger(__name__)

# 購読が未登録の Track Alias 宛てに保持するオブジェクトの上限。
#
# 状態機械が SUBSCRIBE_OK を処理してから Client が購読を登録するまでの間だけ
# 保持すればよいため、通常は数件に収まる。上限に達するのは購読が成立しないまま
# オブジェクトが届き続けている場合だけである。
MAX_PENDING_OBJECTS_PER_ALIAS = 1024


@dataclass(frozen=True, slots=True)
class PeerGoaway:
    """peer から受信した GOAWAY。"""

    new_session_uri: bytes
    """移行先セッションの URI。空の場合は移行先が通知されていない。"""

    timeout: int
    """peer が待つ猶予時間 (ms)。0 の場合は即時の終了を求める。"""


@dataclass(slots=True)
class MoqtObject:
    """受信した MoQT オブジェクト。"""

    stream_id: int | None
    """受信したデータストリームの ID。

    データグラムで届いたオブジェクトは `None` になる。
    """

    group_id: int
    """Group ID。"""

    object_id: int
    """Object ID。"""

    payload: bytes
    """オブジェクトのペイロード。"""

    status: int | None = None
    """Object Status。

    ペイロード長 0 のオブジェクトだけが持ち、非 0 長では `None` になる。
    (draft-ietf-moq-transport-21 §11.1.2 (Object Status))
    """

    properties: bytes | None = None
    """Object Properties の生バイト (`Properties Length | Key-Value-Pairs`)。

    `moqt.moqt.ObjectProperties.decode` で解釈する。データグラムと subgroup の
    どちらでも同じ形になる。
    (draft-ietf-moq-transport-21 §16.8 (Properties) Table 14)
    """

    publisher_priority: int | None = None
    """データストリームまたはデータグラムが運ぶ Publisher Priority。

    `None` は DEFAULT_PRIORITY bit が立ち、購読を確立した制御メッセージで指定された
    優先度を継承することを示す。データグラムは明示的な優先度を持つ場合だけ値が入り、
    DEFAULT_PRIORITY bit が立っている場合は `None` になる。
    (draft-ietf-moq-transport-21 §11.2.1 (Object Datagram) /
    §11.3.1 (Subgroup Header))
    """

    subgroup_id: int | None = None
    """オブジェクトを含む subgroup の Subgroup ID。

    ヘッダが Subgroup ID を最初の Object ID として決めるモードでは `None` になる。
    データグラムでは常に `None` になる。
    """


@dataclass(slots=True)
class Subscription:
    """確立した subscription。"""

    request_id: int
    """SUBSCRIBE の Request ID。"""

    track_alias: int
    """SUBSCRIBE_OK で通知された Track Alias。"""

    namespace: tuple[bytes, ...]
    """Track Namespace。"""

    track_name: bytes
    """Track 名。"""

    parameters: dict[int, object]
    """SUBSCRIBE_OK が運んだパラメータ。

    キーはパラメータ型、値はエンコード済みバイト列である。AUTHORIZATION_TOKEN は
    リストになる。EXPIRES / LARGEST_OBJECT / GROUP_ORDER /
    DEFAULT_PUBLISHER_PRIORITY など publisher が購読条件を確定するために返す値が
    入る (draft-ietf-moq-transport-21 §9.20 (Control Message Parameters))。
    """

    track_properties: dict[int, object]
    """SUBSCRIBE_OK が運んだ Track Properties。

    キーは Track Property 型、値は偶数型なら `int`、奇数型なら `bytes` である
    (draft-ietf-moq-transport-21 §8.4 (Track and Object Properties))。
    """

    _objects: asyncio.Queue[MoqtObject | None] = field(default_factory=asyncio.Queue)
    _runtime: Runtime | None = None

    async def objects(self) -> AsyncIterator[MoqtObject]:
        """受信したオブジェクトを順に返す。

        subscription が終了すると反復も終わる。
        """
        while True:
            item = await self._objects.get()
            if item is None:
                return
            yield item

    async def close(self) -> None:
        """subscription を終了する。"""
        if self._runtime is not None:
            await self._runtime.stop_sending(self.request_id)

    async def request_update(self, parameters: dict[int, object] | None = None) -> None:
        """REQUEST_UPDATE を送り、REQUEST_OK の受信を待つ。

        購読の条件を更新する。REQUEST_UPDATE は同じ request stream に書ける
        (draft-ietf-moq-transport-21 §9.5 (REQUEST_UPDATE))。
        """
        runtime = self._runtime
        if runtime is None:
            raise MoqtError("subscription has no runtime")
        await runtime.send_request_update(self.request_id, parameters)

    def _push(self, item: MoqtObject) -> None:
        """受信したオブジェクトをキューへ積む。"""
        self._objects.put_nowait(item)

    def _finish(self) -> None:
        """subscription の終了を通知する。"""
        self._objects.put_nowait(None)


@dataclass(slots=True)
class Fetch:
    """確立した fetch。"""

    request_id: int
    """FETCH の Request ID。"""

    namespace: tuple[bytes, ...]
    """Track Namespace。"""

    track_name: bytes
    """Track 名。"""

    end_of_track: bool
    """Track の終端まで取得したか。"""

    end_location: tuple[int, int]
    """取得範囲の終端 Location。"""

    _objects: asyncio.Queue[MoqtObject | None] = field(default_factory=asyncio.Queue)
    _ranges: asyncio.Queue[tuple[str, int, int] | None] = field(default_factory=asyncio.Queue)

    async def objects(self) -> AsyncIterator[MoqtObject]:
        """fetch で届いたオブジェクトを順に返す。"""
        while True:
            item = await self._objects.get()
            if item is None:
                return
            yield item

    async def ranges(self) -> AsyncIterator[tuple[str, int, int]]:
        """取得できなかった範囲の終端を順に返す。

        要素は `(種別, group_id, object_id)` である。種別は
        `end_of_non_existent_range` / `end_of_unknown_range` /
        `end_of_timed_out_range` のいずれかである。
        """
        while True:
            item = await self._ranges.get()
            if item is None:
                return
            yield item

    def _push(self, item: MoqtObject) -> None:
        self._objects.put_nowait(item)

    def _push_range(self, kind: str, group_id: int, object_id: int) -> None:
        self._ranges.put_nowait((kind, group_id, object_id))

    def _finish(self) -> None:
        self._objects.put_nowait(None)
        self._ranges.put_nowait(None)


@dataclass(slots=True)
class TrackStatus:
    """TRACK_STATUS の応答。"""

    request_id: int
    """TRACK_STATUS の Request ID。"""

    namespace: tuple[bytes, ...]
    """Track Namespace。"""

    track_name: bytes
    """Track 名。"""

    parameters: dict[int, object]
    """応答パラメータ (LARGEST_OBJECT など)。"""


class Client:
    """WebTransport 接続上で MoQT を扱う client。"""

    def __init__(
        self,
        url: str,
        *,
        verify_peer: bool = True,
        origin: str = "",
        ca_file: str | None = None,
        implementation: str = "moqt-py",
        control_message_timeout: float | None = None,
        data_stream_timeout: float | None = None,
        setup_options: dict[int, object] | None = None,
    ) -> None:
        """client を作成する。

        `control_message_timeout` と `data_stream_timeout` は peer の停止を検出する
        期限 (秒) である。省略した場合は期限を設けない。設定すると期限切れで
        セッションが終了する
        (draft-ietf-moq-transport-21 §12.2 (Session Termination Codes))。

        `setup_options` は SETUP で送る Setup Option である。キーは Setup Option Type、
        値は偶数型なら `int`、奇数型なら `bytes`、AUTHORIZATION_TOKEN なら Token の
        辞書またはそのリストである。MOQT_IMPLEMENTATION は `implementation` 引数が
        担うため指定できない
        (draft-ietf-moq-transport-21 §16.4 (Setup Options))。
        """
        self._transport = h3.Client(
            url=url,
            verify_peer=verify_peer,
            origin=origin,
            ca_file=ca_file,
        )
        self._implementation = implementation
        self._control_message_timeout = control_message_timeout
        self._data_stream_timeout = data_stream_timeout
        self._setup_options = dict(setup_options) if setup_options is not None else None
        self._runtime: Runtime | None = None
        self._established_event = asyncio.Event()
        self._connect_error: BaseException | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._tick_task: asyncio.Task[None] | None = None
        self._subscriptions: dict[int, Subscription] = {}
        self._subscriptions_by_alias: dict[int, Subscription] = {}
        self._pending_objects: dict[int, list[MoqtObject]] = {}
        self._fetches: dict[int, Fetch] = {}

        # アプリが登録するコールバック
        self._publish_state_notify_callback: (
            Callable[[dict[int, object]], Awaitable[None]] | None
        ) = None
        self._request_update_callback: (
            Callable[[int, dict[int, object]], Awaitable[None]] | None
        ) = None
        self._goaway_callback: Callable[[PeerGoaway], Awaitable[None]] | None = None
        self._peer_goaway: PeerGoaway | None = None

        # 受信データはすべてランタイムへ渡す
        self._transport.on_stream_data(self._on_stream_data)
        self._transport.on_stream_reset(self._on_stream_reset)
        self._transport.on_datagram(self._on_datagram)
        self._transport.on_session_closed(self._on_session_closed)
        self._transport.on_session_ready(self._on_session_ready)

    # ─── 接続 ───────────────────────────────────────────────

    @property
    def established(self) -> bool:
        """MoQT SETUP 交換が完了しているかを返す。"""
        runtime = self._runtime
        return runtime is not None and runtime.established

    @property
    def peer_setup_options(self) -> dict[int, object]:
        """peer が SETUP で宣言した Setup Option。

        キーは Setup Option Type、値は偶数型なら `int`、奇数型なら `bytes` である。
        AUTHORIZATION_TOKEN は Token の辞書のリストになる。未接続の場合は空の辞書を
        返す (draft-ietf-moq-transport-21 §16.4 (Setup Options))。
        """
        runtime = self._runtime
        return {} if runtime is None else runtime.peer_setup_options

    @property
    def peer_goaway(self) -> PeerGoaway | None:
        """peer から受信した GOAWAY。未受信の場合は `None`。"""
        return self._peer_goaway

    @property
    def peer_max_auth_token_cache_size(self) -> int:
        """peer が SETUP で宣言した MAX_AUTH_TOKEN_CACHE_SIZE を返す。

        宣言が無い場合は 0 である。AUTHORIZATION_TOKEN の Token Alias を登録する
        アプリは、この値と登録量を突き合わせて peer の上限に収まるか判断する
        (draft-ietf-moq-transport-21 §9.1.3 (MAX_AUTH_TOKEN_CACHE_SIZE))。
        """
        return self._require_runtime().peer_max_auth_token_cache_size

    @property
    def peer_alias_retention_ms(self) -> int:
        """キャンセル済み peer publisher alias の保持期間 (ms) を返す。

        draft-ietf-moq-transport-21 §3.1.2 (Track Alias) の SHOULD に対応する
        保持期間である。
        """
        return self._require_runtime().peer_alias_retention_ms

    def set_peer_alias_retention_ms(self, retention_ms: int) -> None:
        """キャンセル済み peer publisher alias の保持期間 (ms) を設定する。

        0 を設定すると保持は実質無効になる。既に登録済みの保持期限は変わらない。
        """
        self._require_runtime().set_peer_alias_retention_ms(retention_ms)

    @property
    def goaway_drain_ready(self) -> bool:
        """GOAWAY の drain が完了しているかを返す。

        GOAWAY を送った後、購読や fetch の終了を待ってからセッションを閉じる
        判断に使う
        (draft-ietf-moq-transport-21 §6.6.1 (Graceful Session Migration))。
        """
        return self._require_runtime().goaway_drain_ready

    def goaway_drain_snapshot(self) -> dict[str, list[int]]:
        """GOAWAY の drain を妨げている Request ID を返す。

        キーは `blocking_subscription_request_ids` / `blocking_fetch_request_ids` /
        `blocking_track_status_request_ids` である
        (draft-ietf-moq-transport-21 §6.6.1 (Graceful Session Migration) /
        §9.2 (GOAWAY))。
        """
        return self._require_runtime().goaway_drain_snapshot()

    def subscription_state(self, request_id: int) -> dict[str, object] | None:
        """指定 Request ID の subscription の状態を返す。

        保持していない Request ID の場合は `None` である。全件は
        `subscriptions()` で取得する。値は状態機械のスナップショットであり、
        参照しても状態は変化しない。
        """
        return self._require_runtime().subscription_state(request_id)

    def subscriptions(self) -> dict[int, dict[str, object]]:
        """自側が保持する全 subscription の状態を Request ID をキーにして返す。

        値は状態機械のスナップショットであり、参照しても状態は変化しない。
        """
        return self._require_runtime().subscriptions()

    def fetch_state(self, request_id: int) -> dict[str, object] | None:
        """指定 Request ID の fetch の状態を返す。

        保持していない Request ID の場合は `None` である。`fetch` は FETCH を
        開始する API であるため、状態の照会はこの名前で行う。全件は
        `fetches()` で取得する。値は状態機械のスナップショットであり、
        参照しても状態は変化しない。
        """
        return self._require_runtime().fetch_state(request_id)

    def fetches(self) -> dict[int, dict[str, object]]:
        """自側が保持する全 fetch の状態を Request ID をキーにして返す。

        値は状態機械のスナップショットであり、参照しても状態は変化しない。
        """
        return self._require_runtime().fetches()

    def track_status_state(self, request_id: int) -> dict[str, object] | None:
        """指定 Request ID の TRACK_STATUS の状態を返す。

        保持していない Request ID の場合は `None` である。`track_status` は
        TRACK_STATUS を送る API であるため、状態の照会はこの名前で行う。全件は
        `track_status_requests()` で取得する。値は状態機械のスナップショットであり、
        参照しても状態は変化しない。
        """
        return self._require_runtime().track_status_state(request_id)

    def track_status_requests(self) -> dict[int, dict[str, object]]:
        """自側が保持する全 TRACK_STATUS の状態を Request ID をキーにして返す。

        値は状態機械のスナップショットであり、参照しても状態は変化しない。
        """
        return self._require_runtime().track_status_requests()

    def on_publish_state_notify(
        self,
        callback: Callable[[dict[int, object]], Awaitable[None]],
    ) -> None:
        """PUBLISH_STATE_NOTIFY を受信したときに呼ぶコールバックを登録する。

        状態機械が購読の状態へ反映済みであり、応答は不要である
        (draft-ietf-moq-transport-21 §9.10 (PUBLISH_STATE_NOTIFY))。
        """
        self._publish_state_notify_callback = callback

    def on_request_update(
        self,
        callback: Callable[[int, dict[int, object]], Awaitable[None]],
    ) -> None:
        """REQUEST_UPDATE を受信したときに呼ぶコールバックを登録する。

        コールバックが例外を送出すると REQUEST_ERROR で拒否し、それ以外は
        REQUEST_OK で受け入れる。応答はランタイムが送る。
        """
        self._request_update_callback = callback

    def on_goaway(self, callback: Callable[[PeerGoaway], Awaitable[None]]) -> None:
        """GOAWAY を受信したときに呼ぶコールバックを登録する。

        GOAWAY の受信後は状態機械が新規 request の送信を拒否する。移行先が
        通知された場合は、アプリが新しいセッションへ接続し直す。
        """
        self._goaway_callback = callback

    async def connect(self, timeout: float = 10.0) -> None:
        """WebTransport へ接続し、MoQT SETUP 交換の完了を待つ。"""
        if self._run_task is not None:
            raise RuntimeError("client has already been started")

        # 接続に失敗した場合は webtransport-py が具体的な例外を送出する
        await self._transport.connect(timeout=timeout)

        self._runtime = Runtime(
            client=True,
            implementation=self._implementation,
            ops=self._transport_ops(),
            events=self._runtime_events(),
            on_task_error=self._on_task_error,
            control_message_timeout=self._control_message_timeout,
            data_stream_timeout=self._data_stream_timeout,
            setup_options=self._setup_options,
        )
        self._run_task = asyncio.create_task(self._transport.run())
        self._run_task.add_done_callback(self._on_run_done)
        self._tick_task = asyncio.create_task(self._tick_loop())

        try:
            await self._runtime.start()
            await asyncio.wait_for(self._established_event.wait(), timeout=timeout)
        except TimeoutError:
            await self.close()
            raise TimeoutError(f"MoQT SETUP did not complete within {timeout} seconds") from None

        if self._connect_error is not None:
            error = self._connect_error
            await self.close()
            raise error

    async def close(self) -> None:
        """MoQT client と WebTransport 接続を閉じる。"""
        if self._tick_task is not None:
            self._tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._tick_task
            self._tick_task = None
        runtime = self._runtime
        if runtime is not None:
            with contextlib.suppress(Exception):
                await runtime.close()
            self._runtime = None
        self._pending_objects.clear()
        await self._transport.close()
        if self._run_task is not None:
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
            finally:
                self._run_task = None

    # ─── 公開 API ───────────────────────────────────────────

    async def subscribe(
        self,
        namespace: Sequence[bytes],
        track_name: bytes,
        parameters: dict[int, object] | None = None,
    ) -> Subscription:
        """Track を購読する。

        返る `Subscription` は SUBSCRIBE_OK が運んだパラメータと Track Properties を
        保持する。EXPIRES は購読の有効期限、LARGEST_OBJECT は publisher が持つ最新の
        Location であり、アプリはこれらを見て購読の更新や取得範囲を判断できる。
        """
        runtime = self._require_runtime()
        request_id, event = await runtime.subscribe(namespace, track_name, parameters)
        # 応答の SUBSCRIBE_OK はパラメータのみを運ぶため、Track Alias は
        # 状態機械から取得する
        track_alias = runtime.subscription_track_alias(request_id)
        subscription = Subscription(
            request_id=request_id,
            track_alias=track_alias,
            namespace=tuple(namespace),
            track_name=track_name,
            parameters=dict(event.parameters or {}),
            track_properties=dict(event.track_properties or {}),
            _runtime=runtime,
        )
        self._subscriptions[request_id] = subscription
        self._subscriptions_by_alias[track_alias] = subscription
        # SUBSCRIBE_OK の処理より先に届いていたオブジェクトを購読へ渡す
        for item in self._pending_objects.pop(track_alias, []):
            subscription._push(item)
        return subscription

    async def publish(
        self,
        namespace: Sequence[bytes],
        track_name: bytes,
        track_alias: int,
        parameters: dict[int, object] | None = None,
        track_properties: dict[int, object] | None = None,
    ) -> Publication:
        """Track の配信を開始する (PUBLISH)。

        `track_properties` は `moqt.moqt.TrackProperties.to_dict()` の形で渡す。
        応答 (REQUEST_OK) を受信してから `Publication` を返す。送信した
        オブジェクトは `Publication.send_object` と `send_datagram` で送る。

        返る `Publication` は REQUEST_OK が運んだパラメータと Track Properties を
        保持する。EXPIRES は配信の有効期限である
        (draft-ietf-moq-transport-21 §9.3 (REQUEST_OK))。
        """
        runtime = self._require_runtime()
        request_id, event = await runtime.publish(
            namespace, track_name, track_alias, parameters, track_properties
        )
        return Publication(
            request_id=request_id,
            track_alias=track_alias,
            namespace=tuple(namespace),
            track_name=track_name,
            runtime=runtime,
            parameters=dict(event.parameters or {}),
            track_properties=dict(event.track_properties or {}),
        )

    async def fetch(
        self,
        namespace: Sequence[bytes],
        track_name: bytes,
        parameters: dict[int, object] | None = None,
    ) -> Fetch:
        """Track のオブジェクトを取得する (FETCH)。

        取得範囲は `LOCATION_FILTER` パラメータで指定する。FETCH_OK の受信後、
        fetch stream で届くオブジェクトを `Fetch.objects()` で取り出せる。
        """
        runtime = self._require_runtime()
        # FETCH_OK より先に fetch stream のオブジェクトが届くことがあるため、
        # 応答を待つ前に Fetch を登録する
        request_id, event = await runtime.fetch(
            namespace,
            track_name,
            parameters,
            on_request_id=lambda value: self._register_pending_fetch(value, namespace, track_name),
        )
        fetch = self._fetches.get(request_id)
        if fetch is None:
            fetch = self._new_fetch(request_id, namespace, track_name)
            self._fetches[request_id] = fetch
        body = event.message or {}
        end_location = body.get("end_location")
        fetch.end_of_track = bool(body.get("end_of_track"))
        # end_location は (group_id, object_id) のタプルである。型が違う場合は既定値を使う
        if isinstance(end_location, tuple) and len(end_location) == 2:
            group_id, object_id = end_location
            if isinstance(group_id, int) and isinstance(object_id, int):
                fetch.end_location = (group_id, object_id)
        return fetch

    def _new_fetch(
        self,
        request_id: int,
        namespace: Sequence[bytes],
        track_name: bytes,
    ) -> Fetch:
        """Fetch を作成して登録する。"""
        fetch = Fetch(
            request_id=request_id,
            namespace=tuple(namespace),
            track_name=track_name,
            end_of_track=False,
            end_location=(0, 0),
        )
        self._fetches[request_id] = fetch
        return fetch

    def _register_pending_fetch(
        self,
        request_id: int,
        namespace: Sequence[bytes],
        track_name: bytes,
    ) -> None:
        """FETCH の Request ID が確定した時点で Fetch を登録する。"""
        if request_id not in self._fetches:
            self._new_fetch(request_id, namespace, track_name)

    async def track_status(
        self,
        namespace: Sequence[bytes],
        track_name: bytes,
        parameters: dict[int, object] | None = None,
    ) -> TrackStatus:
        """Track の状態を問い合わせる (TRACK_STATUS)。

        draft-ietf-moq-transport-21 §9.13 (TRACK_STATUS) は publisher が
        TRACK_STATUS_OK または REQUEST_ERROR で応答するとする。moqt-py が利用する
        状態機械は TRACK_STATUS を endpoint が受信する request として受理しないため、
        moqt-py の server は応答を返さずにセッションを終了する。応答が無いまま
        待ち続けないよう `control_message_timeout` を設定すること。
        """
        runtime = self._require_runtime()
        request_id, event = await runtime.track_status(namespace, track_name, parameters)
        return TrackStatus(
            request_id=request_id,
            namespace=tuple(namespace),
            track_name=track_name,
            parameters=dict(event.parameters or {}),
        )

    async def goaway(self, timeout: int = 0, new_session_uri: bytes = b"") -> None:
        """GOAWAY を送信してセッションの終了を予告する。

        `new_session_uri` は移行先のセッション URI である。URI を通知できるのは
        Server だけであり、Client は空の URI しか送れない。
        `MAX_NEW_SESSION_URI_LENGTH` を超える値は送信せずに `MoqtError` になる
        (draft-ietf-moq-transport-21 §9.2 (GOAWAY))。
        """
        runtime = self._require_runtime()
        await runtime.send_goaway(timeout, new_session_uri)

    # ─── 内部 ───────────────────────────────────────────────

    def _require_runtime(self) -> Runtime:
        """接続済みのランタイムを返す。"""
        runtime = self._runtime
        if runtime is None or not runtime.established:
            raise MoqtError("client is not connected")
        return runtime

    def _transport_ops(self) -> TransportOps:
        """トランスポート操作を組み立てる。"""
        transport = self._transport
        return TransportOps(
            open_uni_stream=lambda: transport.open_stream(unidirectional=True),
            open_bidi_stream=lambda: transport.open_stream(unidirectional=False),
            send_stream_data=lambda stream_id, data, fin: transport.send_stream_data(
                stream_id, data, fin
            ),
            reset_stream=lambda stream_id, error_code: transport.reset_stream(
                stream_id, error_code
            ),
            stop_sending=self._stop_sending,
            send_datagram=transport.send_datagram,
            close=lambda _code, _reason: transport.close(),
        )

    async def _stop_sending(self, stream_id: int, error_code: int) -> None:
        """受信ストリームへ STOP_SENDING を送る。"""
        # webtransport-py の client は STOP_SENDING を公開していないため、
        # ストリームを reset して受信を終わらせる
        await self._transport.reset_stream(stream_id, error_code)

    def _runtime_events(self) -> RuntimeEvents:
        """ランタイムのコールバックを組み立てる。"""
        return RuntimeEvents(
            on_established=self._on_established,
            on_close=self._on_close,
            on_object=self._on_object,
            on_fetch_end=self._on_fetch_end,
            on_request_terminated=self._on_request_terminated,
            on_publish_done=self._on_publish_done,
            on_publish_state_notify=self._on_publish_state_notify,
            on_request_update=self._on_request_update,
            on_goaway=self._on_goaway,
        )

    async def _on_established(self) -> None:
        self._established_event.set()

    async def _on_close(self, code: int, reason: str) -> None:
        self._fail_connect(MoqtError(f"session closed: code={code} reason={reason}"))

    async def _on_fetch_end(self, kind: str, event: NativeEvent) -> None:
        """fetch の範囲終端を fetch へ渡す。"""
        for fetch in self._fetches.values():
            fetch._push_range(kind, event.group_id or 0, event.object_id or 0)
        return None

    async def _on_object(self, stream_id: int | None, event: NativeEvent, payload: bytes) -> None:
        """受信したオブジェクトを subscription へ渡す。

        data stream は Request ID ではなく Track Alias で購読を特定する。
        Group ID はデータストリームのヘッダが運ぶため、ここではストリームごとに
        記録した値を使う。
        """
        track_alias = event.track_alias
        item = MoqtObject(
            stream_id=stream_id,
            group_id=event.group_id or 0,
            object_id=event.object_id or 0,
            payload=payload,
            status=event.status,
            properties=event.properties,
            publisher_priority=event.publisher_priority,
            subgroup_id=event.subgroup_id,
        )
        if track_alias is None:
            # fetch stream のオブジェクトは Track Alias を持たない
            for fetch in self._fetches.values():
                fetch._push(item)
            return
        subscription = self._subscriptions_by_alias.get(track_alias)
        if subscription is None:
            # 状態機械が SUBSCRIBE_OK を処理してから Client が購読を登録するまでの間に
            # 届いたオブジェクトである。購読が決まるまで保持する
            self._buffer_object(track_alias, item)
            return
        subscription._push(item)

    def _buffer_object(self, track_alias: int, item: MoqtObject) -> None:
        """購読が未登録の Track Alias 宛てのオブジェクトを保持する。

        保持する数には上限を設ける。上限に達するのは、購読が成立しないまま
        オブジェクトが届き続けている場合だけである。
        """
        pending = self._pending_objects.setdefault(track_alias, [])
        if len(pending) >= MAX_PENDING_OBJECTS_PER_ALIAS:
            logger.warning(
                "MoQT dropped an object for track alias %d: "
                "no subscription is registered and %d objects are already buffered",
                track_alias,
                MAX_PENDING_OBJECTS_PER_ALIAS,
            )
            pending.pop(0)
        pending.append(item)

    async def _on_request_terminated(self, event: NativeEvent) -> None:
        request_id = event.request_id
        if request_id is None:
            return
        subscription = self._subscriptions.pop(request_id, None)
        if subscription is not None:
            self._subscriptions_by_alias.pop(subscription.track_alias, None)
            self._pending_objects.pop(subscription.track_alias, None)
            subscription._finish()
        fetch = self._fetches.pop(request_id, None)
        if fetch is not None:
            fetch._finish()

    async def _on_publish_done(self, event: NativeEvent) -> None:
        subscription = self._subscriptions.get(event.request_id or -1)
        if subscription is not None:
            subscription._finish()

    async def _on_publish_state_notify(self, event: NativeEvent) -> None:
        """peer からの PUBLISH_STATE_NOTIFY をアプリへ通知する。"""
        callback = self._publish_state_notify_callback
        if callback is not None:
            await callback(event.parameters or {})

    async def _on_request_update(self, event: NativeEvent) -> None:
        """peer からの REQUEST_UPDATE をアプリへ通知する。

        応答 (REQUEST_OK) はランタイムが送る。アプリが例外を送出した場合だけ
        REQUEST_ERROR で拒否される。
        """
        callback = self._request_update_callback
        if callback is not None:
            await callback(event.request_id or 0, event.parameters or {})

    async def _on_goaway(self, event: NativeEvent) -> None:
        """peer からの GOAWAY をアプリへ通知する。

        状態機械は GOAWAY 受信後の新規 request 送信を拒否する
        (moqt-rs の `SendRequestError::PeerGoawayReceived`)。移行先が通知された
        場合はアプリが新しいセッションへ接続し直す。
        """
        body = event.message or {}
        new_session_uri = body.get("new_session_uri")
        timeout = body.get("timeout")
        peer_goaway = PeerGoaway(
            new_session_uri=new_session_uri if isinstance(new_session_uri, bytes) else b"",
            timeout=timeout if isinstance(timeout, int) else 0,
        )
        self._peer_goaway = peer_goaway
        callback = self._goaway_callback
        if callback is not None:
            await callback(peer_goaway)

    async def _on_task_error(self, error: BaseException) -> None:
        """アプリのコールバックの失敗を記録する。

        セッション自体は壊れていないため、接続は閉じない。
        """
        logger.warning("MoQT callback failed: %s", error)

    async def _tick_loop(self) -> None:
        """セッションのタイムアウト判定を定期的に実行する。"""
        while True:
            await asyncio.sleep(TICK_INTERVAL)
            runtime = self._runtime
            if runtime is None or runtime.closed:
                return
            with contextlib.suppress(Exception):
                await runtime.tick()

    async def _on_stream_data(self, stream_id: int, data: bytes) -> None:
        runtime = self._runtime
        if runtime is None:
            return
        try:
            await runtime.receive_stream(stream_id, data)
        except Exception as error:
            self._fail_connect(error)

    async def _on_stream_reset(self, stream_id: int, error_code: int | None) -> None:
        runtime = self._runtime
        if runtime is None:
            return
        with contextlib.suppress(Exception):
            await runtime.receive_stream_closed(stream_id, error_code)

    async def _on_datagram(self, data: bytes) -> None:
        runtime = self._runtime
        if runtime is None:
            return
        try:
            await runtime.receive_datagram(data)
        except Exception as error:
            self._fail_connect(error)

    async def _on_session_ready(self, session_id: int) -> None:
        """WebTransport session の確立を待つ。"""
        logger.debug("WebTransport session ready: %s", session_id)

    async def _on_session_closed(self, session_id: int) -> None:
        """SETUP 完了前の WebTransport session close を接続失敗として扱う。"""
        if not self.established:
            self._fail_connect(
                ConnectionError(
                    f"WebTransport session {session_id} closed before MoQT SETUP completed"
                )
            )

    def _fail_connect(self, error: BaseException) -> None:
        """最初の接続エラーを保存して待機中の connect を起こす。"""
        if self._connect_error is None:
            self._connect_error = error
        self._established_event.set()

    def _on_run_done(self, task: asyncio.Task[None]) -> None:
        """WebTransport の受信ループ異常を SETUP 待機側へ伝える。"""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._fail_connect(error)

    async def __aenter__(self) -> Client:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()


def _body_int(body: MessageBody | None, key: str) -> int:
    """メッセージ本体から整数を取り出す。"""
    if body is None:
        return 0
    value = body.get(key)
    return value if isinstance(value, int) else 0


__all__ = [
    "Client",
    "Fetch",
    "MoqtObject",
    "Publication",
    "Subscription",
    "TrackStatus",
]
