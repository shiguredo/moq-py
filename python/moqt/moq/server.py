"""WebTransport over HTTP/3 を利用する MoQT server。"""

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING

from webtransport import h3

from moqt import moqt
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
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# 接続を識別する context。`RuntimeEvents.bind` がコールバックの第 1 引数に渡す。
ConnectionContext = tuple[tuple[str, int], int]


@dataclass(frozen=True, slots=True)
class ServerSession:
    """確立した MoQT server session。"""

    session_id: int
    """WebTransport session の ID。"""

    address: tuple[str, int]
    """peer のアドレス。"""

    runtime: Runtime
    """この session のランタイム。"""

    async def goaway(self, timeout: int = 0) -> None:
        """GOAWAY を送り、セッションの終了を予告する。

        `timeout` は peer が残りの request を終えるまで待つ猶予時間 (ms) である。
        GOAWAY の送信後、peer は新しい request を開始しない
        (draft-ietf-moq-transport-21 §9.2 (GOAWAY))。
        """
        await self.runtime.send_goaway(timeout)


@dataclass(slots=True)
class SubscriptionRequest:
    """peer から届いた SUBSCRIBE。"""

    request_id: int
    """SUBSCRIBE の Request ID。"""

    namespace: tuple[bytes, ...]
    """Track Namespace。"""

    track_name: bytes
    """Track 名。"""

    parameters: dict[int, object]
    """購読パラメータ。"""

    runtime: Runtime
    """応答に使うランタイム。"""

    async def subscribe_ok(
        self,
        track_alias: int,
        parameters: dict[int, object] | None = None,
        track_properties: dict[int, object] | None = None,
    ) -> Publication:
        """SUBSCRIBE_OK を返して配信を開始する。"""
        await self.runtime.send_subscribe_ok(
            self.request_id, track_alias, parameters, track_properties
        )
        return Publication(
            request_id=self.request_id,
            track_alias=track_alias,
            namespace=self.namespace,
            track_name=self.track_name,
            runtime=self.runtime,
        )

    async def reject(self, error_code: int, reason: str) -> None:
        """REQUEST_ERROR を返して購読を拒否する。"""
        await self.runtime.send_request_error(self.request_id, error_code, reason)


@dataclass(slots=True)
class FetchRequest:
    """peer から届いた FETCH。"""

    request_id: int
    """FETCH の Request ID。"""

    namespace: tuple[bytes, ...]
    """Track Namespace。"""

    track_name: bytes
    """Track 名。"""

    parameters: dict[int, object]
    """取得条件のパラメータ。"""

    runtime: Runtime
    """応答に使うランタイム。"""

    async def respond(
        self,
        end_location: tuple[int, int],
        *,
        end_of_track: bool = False,
        parameters: dict[int, object] | None = None,
        track_properties: dict[int, object] | None = None,
    ) -> FetchResponse:
        """FETCH_OK を返して応答ストリームを開く。"""
        await self.runtime.send_fetch_ok(
            self.request_id,
            end_location,
            end_of_track=end_of_track,
            parameters=parameters,
            track_properties=track_properties,
        )
        stream_id = await self.runtime.open_fetch_stream(self.request_id)
        return FetchResponse(
            request_id=self.request_id,
            stream_id=stream_id,
            runtime=self.runtime,
        )

    async def reject(self, error_code: int, reason: str) -> None:
        """REQUEST_ERROR を返して取得を拒否する。"""
        await self.runtime.send_request_error(self.request_id, error_code, reason)


@dataclass(slots=True)
class FetchResponse:
    """配信中の fetch 応答。"""

    request_id: int
    """FETCH の Request ID。"""

    stream_id: int
    """応答に使う fetch stream の ID。"""

    runtime: Runtime
    """送信に使うランタイム。"""

    async def send_object(
        self,
        group_id: int,
        object_id: int,
        payload: bytes,
        *,
        publisher_priority: int = 128,
        subgroup_id: int = 0,
    ) -> None:
        """fetch stream へオブジェクトを書き込む。"""
        await self.runtime.send_fetch_stream_object(
            self.stream_id,
            group_id,
            object_id,
            payload,
            publisher_priority=publisher_priority,
            subgroup_id=subgroup_id,
        )

    async def close(self) -> None:
        """fetch stream を終了する。"""
        await self.runtime.close_fetch_stream(self.stream_id)


@dataclass(slots=True)
class PublisherRequest:
    """peer から届いた PUBLISH。"""

    request_id: int
    """PUBLISH の Request ID。"""

    namespace: tuple[bytes, ...]
    """Track Namespace。"""

    track_name: bytes
    """Track 名。"""

    track_alias: int
    """peer が通知した Track Alias。"""

    parameters: dict[int, object]
    """PUBLISH のパラメータ。"""

    runtime: Runtime
    """応答に使うランタイム。"""

    async def accept(
        self,
        parameters: dict[int, object] | None = None,
        track_properties: dict[int, object] | None = None,
    ) -> Publication:
        """REQUEST_OK を返して配信を受け入れる。

        PUBLISH の応答は REQUEST_OK であり、SUBSCRIBE_OK とは異なり Track Alias を
        運ばない。peer が通知した Track Alias をそのまま使う
        (draft-ietf-moq-transport-21 §9.3 (REQUEST_OK))。
        """
        await self.runtime.send_request_ok(self.request_id, parameters, track_properties)
        return Publication(
            request_id=self.request_id,
            track_alias=self.track_alias,
            namespace=self.namespace,
            track_name=self.track_name,
            runtime=self.runtime,
        )

    async def reject(self, error_code: int, reason: str) -> None:
        """REQUEST_ERROR を返して配信を拒否する。"""
        await self.runtime.send_request_error(self.request_id, error_code, reason)


@dataclass(slots=True)
class _Connection:
    """1 本の WebTransport session に対応する MoQT の内部状態。"""

    runtime: Runtime
    address: tuple[str, int]
    session_id: int


class Server:
    """WebTransport 接続上で MoQT を扱う server。"""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        certfile: str,
        keyfile: str,
        allowed_origins: list[str] | None = None,
        implementation: str = "moqt-py",
        control_message_timeout: float | None = None,
        data_stream_timeout: float | None = None,
    ) -> None:
        """server を作成する。

        `control_message_timeout` と `data_stream_timeout` は peer の停止を検出する
        期限 (秒) である。省略した場合は期限を設けない。設定すると期限切れで
        セッションが終了する
        (draft-ietf-moq-transport-21 §12.2 (Session Termination Codes))。
        """
        self._transport = h3.Server(
            host=host,
            port=port,
            certfile=certfile,
            keyfile=keyfile,
            allowed_origins=allowed_origins,
        )
        self._implementation = implementation
        self._control_message_timeout = control_message_timeout
        self._data_stream_timeout = data_stream_timeout
        self._connections: dict[tuple[tuple[str, int], int], _Connection] = {}
        self._on_session_established: Callable[[ServerSession], Awaitable[None]] | None = None
        self._on_subscribe: Callable[[SubscriptionRequest], Awaitable[None]] | None = None
        self._on_fetch: Callable[[FetchRequest], Awaitable[None]] | None = None
        self._publish_callback: Callable[[PublisherRequest], Awaitable[None]] | None = None
        self._request_update_callback: (
            Callable[[Runtime, int, dict[int, object]], Awaitable[None]] | None
        ) = None
        self._tick_task: asyncio.Task[None] | None = None

        self._transport.on_session_ready(self._on_session_ready)
        self._transport.on_session_closed(self._on_session_closed)
        self._transport.on_stream_data(self._on_stream_data)
        self._transport.on_stream_reset(self._on_stream_reset)
        self._transport.on_datagram(self._on_datagram)

    # ─── 公開 API ───────────────────────────────────────────

    @property
    def actual_port(self) -> int:
        """実際にバインドしている UDP ポート番号。"""
        return self._transport.actual_port

    def on_session_established(
        self,
        callback: Callable[[ServerSession], Awaitable[None]],
    ) -> None:
        """MoQT SETUP 完了時に呼び出す非同期 callback を設定する。"""
        self._on_session_established = callback

    def on_subscribe(
        self,
        callback: Callable[[SubscriptionRequest], Awaitable[None]],
    ) -> None:
        """peer から SUBSCRIBE が届いたときに呼び出す非同期 callback を設定する。"""
        self._on_subscribe = callback

    def on_fetch(
        self,
        callback: Callable[[FetchRequest], Awaitable[None]],
    ) -> None:
        """peer から FETCH が届いたときに呼び出す非同期 callback を設定する。"""
        self._on_fetch = callback

    def on_publish(
        self,
        callback: Callable[[PublisherRequest], Awaitable[None]],
    ) -> None:
        """peer から PUBLISH が届いたときに呼び出す非同期 callback を設定する。

        コールバックは `PublisherRequest.accept()` で受け入れるか、`reject()` で
        拒否する。未登録の場合は PUBLISH を REQUEST_NOT_SUPPORTED で拒否する。
        """
        self._publish_callback = callback

    def on_request_update(
        self,
        callback: Callable[[Runtime, int, dict[int, object]], Awaitable[None]],
    ) -> None:
        """peer から REQUEST_UPDATE が届いたときに呼び出す非同期 callback を設定する。

        引数はランタイム、request の Request ID、受信パラメータである。コールバックが
        例外を送出すると REQUEST_ERROR で拒否し、それ以外は REQUEST_OK で受け入れる。
        応答はランタイムが送る。未登録の場合は受け入れる。

        request の所有者はランタイムが識別済みである。PUBLISH の応答を返したい場合は
        コールバックで `runtime.send_request_ok(request_id)` を呼ぶ。
        """
        self._request_update_callback = callback

    async def start(self) -> None:
        """WebTransport server を開始する。"""
        await self._transport.start()
        self._tick_task = asyncio.create_task(self._tick_loop())

    async def run(self) -> None:
        """停止されるまで WebTransport server の受信ループを実行する。"""
        await self._transport.run()

    async def stop(self) -> None:
        """全接続と WebTransport server を停止する。"""
        if self._tick_task is not None:
            self._tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._tick_task
            self._tick_task = None
        for connection in list(self._connections.values()):
            with contextlib.suppress(Exception):
                await connection.runtime.close()
        self._connections.clear()
        await self._transport.stop()

    # ─── 内部 ───────────────────────────────────────────────

    def _transport_ops(self, address: tuple[str, int], session_id: int) -> TransportOps:
        """1 本の session に対応するトランスポート操作を組み立てる。"""
        transport = self._transport

        async def open_bidi_stream() -> int:
            # WebTransport は server 起点の双方向ストリームを規定していない
            raise MoqtError(
                "the server cannot open a bidirectional stream; "
                "server-initiated requests are not available over WebTransport"
            )

        return TransportOps(
            open_uni_stream=lambda: transport.open_stream(address, session_id, True),
            open_bidi_stream=open_bidi_stream,
            send_stream_data=lambda stream_id, data, fin: transport.send_stream_data(
                address, stream_id, data, fin
            ),
            reset_stream=lambda stream_id, error_code: transport.reset_stream(
                address, stream_id, error_code
            ),
            stop_sending=self._make_stop_sending(address, session_id),
            send_datagram=lambda data: transport.send_datagram(address, session_id, data),
            close=lambda _code, _reason: transport.close_stream(address, session_id, 0),
        )

    def _make_stop_sending(
        self, address: tuple[str, int], session_id: int
    ) -> Callable[[int, int], Awaitable[None]]:
        """受信ストリームの中断を返す。"""

        async def stop_sending(stream_id: int, error_code: int) -> None:
            # webtransport-py の server は STOP_SENDING を公開していないため、
            # ストリームを reset して受信を終わらせる
            await self._transport.reset_stream(address, stream_id, error_code)

        return stop_sending

    def _runtime_events(self, context: ConnectionContext) -> RuntimeEvents:
        """ランタイムのコールバックを組み立てる。

        server は接続ごとにランタイムを作るため、コールバックは接続を
        識別する context を閉じ込めた形で渡す。
        """
        return RuntimeEvents(
            on_established=lambda: self._on_established(context),
            on_request=lambda event: self._on_request(context, event),
            on_request_update=lambda event: self._on_request_update(context, event),
        )

    async def _on_request_update(self, context: ConnectionContext, event: NativeEvent) -> None:
        """peer からの REQUEST_UPDATE をアプリへ通知する。

        応答 (REQUEST_OK) はランタイムが送る。アプリが例外を送出した場合だけ
        REQUEST_ERROR で拒否される。コールバックが未登録の場合は受け入れる。
        """
        callback = self._request_update_callback
        if callback is None:
            return
        connection = self._connection(context)
        if connection is None:
            return
        await callback(connection.runtime, event.request_id or 0, event.parameters or {})

    async def _on_established(self, context: ConnectionContext) -> None:
        """SETUP 完了をアプリケーションへ通知する。"""
        address, session_id = context
        connection = self._connection(context)
        if connection is None:
            return
        if self._on_session_established is not None:
            await self._on_session_established(
                ServerSession(
                    session_id=session_id,
                    address=address,
                    runtime=connection.runtime,
                )
            )

    def _connection(self, context: ConnectionContext) -> _Connection | None:
        """コールバックの context から接続を引く。"""
        address, session_id = context
        return self._connections.get((address, session_id))

    async def _on_request(self, context: ConnectionContext, event: NativeEvent) -> None:
        """peer からの request を処理する。"""
        connection = self._connection(context)
        if connection is None:
            return
        runtime = connection.runtime
        if event.kind == "fetch":
            if self._on_fetch is None:
                await runtime.send_request_error(
                    event.request_id or 0,
                    0x3,
                    "FETCH is not handled by this server",
                )
                return
            body = event.message
            request = FetchRequest(
                request_id=event.request_id or 0,
                namespace=_body_namespace(body, "track_namespace"),
                track_name=_body_bytes(body, "track_name"),
                parameters=_body_parameters(body),
                runtime=runtime,
            )
            await self._on_fetch(request)
            return
        if event.kind == "publish":
            if self._publish_callback is None:
                await runtime.send_request_error(
                    event.request_id or 0,
                    moqt.REQUEST_NOT_SUPPORTED,
                    "PUBLISH is not handled by this server",
                )
                return
            body = event.message
            request = PublisherRequest(
                request_id=event.request_id or 0,
                namespace=_body_namespace(body, "track_namespace"),
                track_name=_body_bytes(body, "track_name"),
                track_alias=_body_int(body, "track_alias"),
                parameters=_body_parameters(body),
                runtime=runtime,
            )
            await self._publish_callback(request)
            return
        if event.kind not in {"subscribe", "track_status"}:
            # 未対応の request は REQUEST_NOT_SUPPORTED で拒否する
            await runtime.send_request_error(
                event.request_id or 0,
                moqt.REQUEST_NOT_SUPPORTED,
                f"{event.kind} is not supported",
            )
            return
        if event.kind == "track_status":
            # TRACK_STATUS は relay が返す応答であり、endpoint は購読の一部として
            # 応答できない。名前空間の探索を伴わないため DOES_NOT_EXIST を返す
            # (moqt-rs の `recv_request` も TRACK_STATUS を受理しない)。
            await runtime.send_request_error(
                event.request_id or 0,
                moqt.REQUEST_DOES_NOT_EXIST,
                "TRACK_STATUS is not answered by an endpoint",
            )
            return
        if self._on_subscribe is None:
            await runtime.send_request_error(
                event.request_id or 0,
                0x3,
                "SUBSCRIBE is not handled by this server",
            )
            return
        body = event.message
        request = SubscriptionRequest(
            request_id=event.request_id or 0,
            namespace=_body_namespace(body, "track_namespace"),
            track_name=_body_bytes(body, "track_name"),
            parameters=_body_parameters(body),
            runtime=runtime,
        )
        await self._on_subscribe(request)

    async def _on_session_ready(self, session_id: int, address: tuple[str, int]) -> None:
        """WebTransport session ごとに server role の MoQT Session を開始する。"""
        key = (address, session_id)
        if key in self._connections:
            raise RuntimeError(f"duplicate WebTransport session: {session_id} from {address}")

        context: ConnectionContext = (address, session_id)
        runtime = Runtime(
            client=False,
            implementation=self._implementation,
            ops=self._transport_ops(address, session_id),
            events=self._runtime_events(context),
            control_message_timeout=self._control_message_timeout,
            data_stream_timeout=self._data_stream_timeout,
        )
        self._connections[key] = _Connection(
            runtime=runtime, address=address, session_id=session_id
        )
        await runtime.start()

    async def _on_session_closed(self, session_id: int, address: tuple[str, int]) -> None:
        """閉じた WebTransport session の MoQT 状態を破棄する。"""
        self._connections.pop((address, session_id), None)

    async def _on_stream_data(
        self,
        session_id: int,
        stream_id: int,
        data: bytes,
        address: tuple[str, int],
    ) -> None:
        """受信データをストリーム種別に振り分ける。"""
        connection = self._connections.get((address, session_id))
        if connection is None:
            raise RuntimeError(f"unknown WebTransport session: {session_id} from {address}")
        await connection.runtime.receive_stream(stream_id, data)

    async def _on_stream_reset(
        self,
        session_id: int,
        stream_id: int,
        error_code: int | None,
        address: tuple[str, int],
    ) -> None:
        """ストリームの終端を MoQT 状態機械へ通知する。"""
        connection = self._connections.get((address, session_id))
        if connection is not None:
            await connection.runtime.receive_stream_closed(stream_id, error_code)

    async def _on_datagram(
        self,
        session_id: int,
        data: bytes,
        address: tuple[str, int],
    ) -> None:
        """受信したデータグラムを MoQT 状態機械へ渡す。"""
        connection = self._connections.get((address, session_id))
        if connection is not None:
            await connection.runtime.receive_datagram(data)

    async def _tick_loop(self) -> None:
        """セッションのタイムアウト判定を定期的に実行する。"""
        while True:
            await asyncio.sleep(TICK_INTERVAL)
            for connection in list(self._connections.values()):
                if connection.runtime.closed:
                    continue
                with contextlib.suppress(Exception):
                    await connection.runtime.tick()

    async def __aenter__(self) -> Server:
        await self.start()
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.stop()


def _body_namespace(body: MessageBody | None, key: str) -> tuple[bytes, ...]:
    """メッセージ本体から Track Namespace を取り出す。"""
    if body is None:
        return ()
    value = body.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(field for field in value if isinstance(field, bytes))


def _body_bytes(body: MessageBody | None, key: str) -> bytes:
    """メッセージ本体からバイト列を取り出す。"""
    if body is None:
        return b""
    value = body.get(key)
    return value if isinstance(value, bytes) else b""


def _body_int(body: MessageBody | None, key: str) -> int:
    """メッセージ本体から整数を取り出す。"""
    if body is None:
        return 0
    value = body.get(key)
    return value if isinstance(value, int) else 0


def _body_parameters(body: MessageBody | None) -> dict[int, object]:
    """メッセージ本体からパラメータを取り出す。"""
    if body is None:
        return {}
    value = body.get("parameters")
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if isinstance(key, int)}


__all__ = [
    "FetchRequest",
    "FetchResponse",
    "Publication",
    "PublisherRequest",
    "Server",
    "ServerSession",
    "SubscriptionRequest",
]
