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

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

logger = logging.getLogger(__name__)

# 購読が未登録の Track Alias 宛てに保持するオブジェクトの上限。
#
# 状態機械が SUBSCRIBE_OK を処理してから Client が購読を登録するまでの間だけ
# 保持すればよいため、通常は数件に収まる。上限に達するのは購読が成立しないまま
# オブジェクトが届き続けている場合だけである。
MAX_PENDING_OBJECTS_PER_ALIAS = 1024


@dataclass(slots=True)
class MoqtObject:
    """受信した MoQT オブジェクト。"""

    stream_id: int
    """受信したデータストリームの ID。"""

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


@dataclass(slots=True)
class Announcement:
    """確立した namespace 購読。"""

    request_id: int
    """SUBSCRIBE_NAMESPACE の Request ID。"""

    prefix: tuple[bytes, ...]
    """購読した prefix。"""

    _namespaces: asyncio.Queue[tuple[bytes, ...] | None] = field(default_factory=asyncio.Queue)

    async def namespaces(self) -> AsyncIterator[tuple[bytes, ...]]:
        """通知された namespace を順に返す。"""
        while True:
            item = await self._namespaces.get()
            if item is None:
                return
            yield item

    def _push(self, suffix: tuple[bytes, ...]) -> None:
        self._namespaces.put_nowait(suffix)

    def _finish(self) -> None:
        self._namespaces.put_nowait(None)


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
    ) -> None:
        self._transport = h3.Client(
            url=url,
            verify_peer=verify_peer,
            origin=origin,
            ca_file=ca_file,
        )
        self._implementation = implementation
        self._runtime: Runtime | None = None
        self._established_event = asyncio.Event()
        self._connect_error: BaseException | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._tick_task: asyncio.Task[None] | None = None
        self._subscriptions: dict[int, Subscription] = {}
        self._subscriptions_by_alias: dict[int, Subscription] = {}
        self._pending_objects: dict[int, list[MoqtObject]] = {}
        self._announcements: dict[int, Announcement] = {}
        self._fetches: dict[int, Fetch] = {}

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
        """Track を購読する。"""
        runtime = self._require_runtime()
        request_id, _event = await runtime.subscribe(namespace, track_name, parameters)
        # 応答の SUBSCRIBE_OK はパラメータのみを運ぶため、Track Alias は
        # 状態機械から取得する
        track_alias = runtime.subscription_track_alias(request_id)
        subscription = Subscription(
            request_id=request_id,
            track_alias=track_alias,
            namespace=tuple(namespace),
            track_name=track_name,
            _runtime=runtime,
        )
        self._subscriptions[request_id] = subscription
        self._subscriptions_by_alias[track_alias] = subscription
        # SUBSCRIBE_OK の処理より先に届いていたオブジェクトを購読へ渡す
        for item in self._pending_objects.pop(track_alias, []):
            subscription._push(item)
        return subscription

    async def subscribe_namespace(
        self,
        prefix: Sequence[bytes],
        parameters: dict[int, object] | None = None,
    ) -> Announcement:
        """Namespace を購読する。"""
        runtime = self._require_runtime()
        request_id, _event = await runtime.subscribe_namespace(prefix, parameters)
        announcement = Announcement(request_id=request_id, prefix=tuple(prefix))
        self._announcements[request_id] = announcement
        return announcement

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
        fetch.end_location = end_location if isinstance(end_location, tuple) else (0, 0)
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
        """Track の状態を問い合わせる (TRACK_STATUS)。"""
        runtime = self._require_runtime()
        request_id, event = await runtime.track_status(namespace, track_name, parameters)
        return TrackStatus(
            request_id=request_id,
            namespace=tuple(namespace),
            track_name=track_name,
            parameters=dict(event.parameters or {}),
        )

    async def subscribe_tracks(
        self,
        prefix: Sequence[bytes],
        parameters: dict[int, object] | None = None,
    ) -> Announcement:
        """prefix 配下の Track の通知を購読する (SUBSCRIBE_TRACKS)。"""
        runtime = self._require_runtime()
        request_id, _event = await runtime.subscribe_tracks(prefix, parameters)
        announcement = Announcement(request_id=request_id, prefix=tuple(prefix))
        self._announcements[request_id] = announcement
        return announcement

    async def goaway(self, timeout: int = 0) -> None:
        """GOAWAY を送信してセッションの終了を予告する。"""
        runtime = self._require_runtime()
        await runtime.send_goaway(timeout)

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
            on_namespace=self._on_namespace,
            on_namespace_done=self._on_namespace_done,
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

    async def _on_object(self, stream_id: int, event: NativeEvent, payload: bytes) -> None:
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
        subscription = self._subscriptions.pop(event.request_id, None)
        if subscription is not None:
            self._subscriptions_by_alias.pop(subscription.track_alias, None)
            self._pending_objects.pop(subscription.track_alias, None)
            subscription._finish()
        fetch = self._fetches.pop(event.request_id, None)
        if fetch is not None:
            fetch._finish()

    async def _on_publish_done(self, event: NativeEvent) -> None:
        subscription = self._subscriptions.get(event.request_id or -1)
        if subscription is not None:
            subscription._finish()

    async def _on_namespace(self, event: NativeEvent) -> None:
        announcement = self._announcements.get(event.request_id or -1)
        if announcement is not None:
            announcement._push(_body_namespace(event.message, "track_namespace_suffix"))

    async def _on_namespace_done(self, event: NativeEvent) -> None:
        announcement = self._announcements.get(event.request_id or -1)
        if announcement is not None:
            announcement._finish()

    async def _on_task_error(self, error: BaseException) -> None:
        self._fail_connect(error)

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


def _body_namespace(body: MessageBody | None, key: str) -> tuple[bytes, ...]:
    """メッセージ本体から Track Namespace を取り出す。"""
    if body is None:
        return ()
    value = body.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(field for field in value if isinstance(field, bytes))


__all__ = [
    "Announcement",
    "Client",
    "Fetch",
    "MoqtObject",
    "Subscription",
    "TrackStatus",
]
