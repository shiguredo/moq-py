"""MoQT セッションと WebTransport ストリームを接続する内部ランタイム。

このモジュールは公開 API ではない。`moqt.moq.client` と `moqt.moq.server` が
共通で使うストリーム振り分けとイベント処理をまとめる。

役割分担は次のとおりである。

- `webtransport.h3` がストリームとデータグラムの I/O を担当する
- `moqt._native` が MoQT のプロトコル状態機械とメッセージのデコードを担当する
- このランタイムが両者を接続し、ストリーム ID と Request ID の対応を保持する

応答メッセージはワイヤに Request ID を含まないため、ストリームと Request ID の
対応を I/O 層が保持する必要がある (draft-ietf-moq-transport-21 §9.4 (REQUEST_ERROR))。
"""

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from moqt import _native, moqt

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Sequence

logger = logging.getLogger(__name__)

# メッセージ本体を表す辞書。キーはメッセージ種別ごとに異なる。
MessageBody = dict[str, object]


class NativeEvent(Protocol):
    """`moqt._native` が返すイベントの構造。

    ネイティブ拡張の型を Python 側で再定義せずに型検査を通すため、
    必要な属性だけを構造として表す。
    """

    @property
    def kind(self) -> str:
        """イベント種別。"""
        ...

    @property
    def data(self) -> bytes | None:
        """制御ストリームへ書き込むバイト列、またはオブジェクトのペイロード。"""
        ...

    @property
    def message_data(self) -> bytes | None:
        """メッセージの生バイト列。"""
        ...

    @property
    def message(self) -> MessageBody | None:
        """メッセージ本体。"""
        ...

    @property
    def parameters(self) -> dict[int, object] | None:
        """メッセージのパラメータ。"""
        ...

    @property
    def request_id(self) -> int | None:
        """対象 request の Request ID。"""
        ...

    @property
    def stream_id(self) -> int | None:
        """対象データストリームの ID。"""
        ...

    @property
    def object_id(self) -> int | None:
        """オブジェクトの Object ID。"""
        ...

    @property
    def group_id(self) -> int | None:
        """オブジェクトの Group ID。"""
        ...

    @property
    def track_alias(self) -> int | None:
        """データストリームの Track Alias。"""
        ...

    @property
    def status(self) -> int | None:
        """オブジェクトの Object Status (ペイロード長 0 の場合のみ)。"""
        ...

    @property
    def properties(self) -> bytes | None:
        """オブジェクトの Properties の生バイト。"""
        ...

    @property
    def publisher_priority(self) -> int | None:
        """データストリームが運ぶ Publisher Priority。"""
        ...

    @property
    def subgroup_id(self) -> int | None:
        """オブジェクトを含む subgroup の Subgroup ID。"""
        ...

    @property
    def reliable_size(self) -> int | None:
        """RESET_STREAM の reliable size。"""
        ...

    @property
    def acceptance(self) -> str | None:
        """オブジェクトの受理結果。"""
        ...

    @property
    def code(self) -> int | None:
        """エラーコード。"""
        ...

    @property
    def reason(self) -> str | None:
        """終了理由。"""
        ...

    @property
    def fin(self) -> bool | None:
        """ストリームを FIN するか。"""
        ...


# ストリーム種別とパラメータの既定値はプロトコル層の定義をそのまま使う
SETUP_STREAM_TYPE = moqt.SETUP_STREAM_TYPE
FETCH_HEADER_TYPE = moqt.FETCH_HEADER_TYPE
PADDING_STREAM_TYPE = moqt.PADDING_STREAM_TYPE
PADDING_DATAGRAM_TYPE = moqt.PADDING_DATAGRAM_TYPE
DEFAULT_SUBSCRIBER_PRIORITY = moqt.DEFAULT_SUBSCRIBER_PRIORITY

# セッションのタイムアウト判定間隔 (秒)
TICK_INTERVAL = 0.1

# ストリームの種別
_STREAM_CONTROL = "control"
_STREAM_REQUEST = "request"
_STREAM_DATA = "data"


class MoqtError(Exception):
    """MoQT のプロトコルエラー。"""


class SessionClosedError(MoqtError):
    """セッションが閉じられた。"""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(f"session closed: code={code} reason={reason}")
        self.code = code
        self.reason = reason


@dataclass(slots=True)
class StreamInfo:
    """peer のストリーム 1 本の状態。"""

    kind: str
    """`control` / `request` / `data` のいずれか。"""

    request_id: int | None = None
    """request stream の場合の Request ID。"""

    stream_type: int | None = None
    """data stream の場合の stream type。"""


@dataclass(slots=True)
class TransportOps:
    """トランスポート操作。

    client と server で引数が異なるため、関数として渡す。
    """

    open_uni_stream: Callable[[], Awaitable[int]]
    open_bidi_stream: Callable[[], Awaitable[int]]
    send_stream_data: Callable[[int, bytes, bool], Awaitable[None]]
    reset_stream: Callable[[int, int], Awaitable[None]]
    stop_sending: Callable[[int, int], Awaitable[None]]
    send_datagram: Callable[[bytes], Awaitable[None]]
    close: Callable[[int, str], Awaitable[None]]


@dataclass(slots=True)
class RuntimeEvents:
    """アプリケーションへ通知するイベントの受け口。"""

    on_established: Callable[[], Awaitable[None]] | None = None
    on_close: Callable[[int, str], Awaitable[None]] | None = None
    on_request: Callable[[NativeEvent], Awaitable[None]] | None = None
    on_request_ok: Callable[[NativeEvent], Awaitable[None]] | None = None
    on_request_error: Callable[[NativeEvent], Awaitable[None]] | None = None
    on_request_terminated: Callable[[NativeEvent], Awaitable[None]] | None = None
    on_request_update: Callable[[NativeEvent], Awaitable[None]] | None = None
    on_publish_done: Callable[[NativeEvent], Awaitable[None]] | None = None
    on_publish_state_notify: Callable[[NativeEvent], Awaitable[None]] | None = None
    on_goaway: Callable[[NativeEvent], Awaitable[None]] | None = None
    on_object: Callable[[int, NativeEvent, bytes], Awaitable[None]] | None = None
    on_fetch_end: Callable[[str, NativeEvent], Awaitable[None]] | None = None

    def bind(self, context: object) -> RuntimeEvents:
        """コールバックを接続に紐付けた新しい `RuntimeEvents` を返す。

        server は 1 つのコールバックで複数の接続を扱うため、どの接続で起きた
        イベントかを識別する必要がある。context はコールバックの第 1 引数として
        渡される。
        """

        def wrap(
            callback: Callable[..., Awaitable[None]] | None,
        ) -> Callable[..., Awaitable[None]] | None:
            if callback is None:
                return None

            async def bound(*args: object) -> None:
                await callback(context, *args)

            return bound

        # ここで包んだコールバックは context を第 1 引数に取るため、
        # 呼び出し側の型とは一致しない。呼び出しは文字列で指定した
        # イベント種別に紐付くため、実行時の引数は正しい。

        return RuntimeEvents(
            on_established=wrap(self.on_established),
            on_close=wrap(self.on_close),
            on_request=wrap(self.on_request),
            on_request_ok=wrap(self.on_request_ok),
            on_request_error=wrap(self.on_request_error),
            on_request_terminated=wrap(self.on_request_terminated),
            on_request_update=wrap(self.on_request_update),
            on_publish_done=wrap(self.on_publish_done),
            on_publish_state_notify=wrap(self.on_publish_state_notify),
            on_goaway=wrap(self.on_goaway),
            on_object=wrap(self.on_object),
            on_fetch_end=wrap(self.on_fetch_end),
        )


@dataclass(slots=True)
class SubgroupWriter:
    """送信中の subgroup ストリーム 1 本の状態。

    Object ID は subgroup ストリーム内で差分として表現されるため、
    直前の Object ID を保持する (draft-ietf-moq-transport-21 §11.3.1)。
    """

    stream_id: int
    group_id: int
    last_object_id: int | None = None
    has_properties: bool = False
    """ヘッダが Properties を持つか。

    Properties の有無はヘッダで固定されるため、途中のオブジェクトで変更できない
    (draft-ietf-moq-transport-21 §11.3.1 (Subgroup Header))。
    """


@dataclass(slots=True)
class FetchWriter:
    """送信中の fetch ストリーム 1 本の状態。

    fetch ストリームの Group ID と Object ID は直前のオブジェクトを基準に
    差分で表現されるため、直前の値を保持する
    (draft-ietf-moq-transport-21 §11.4.1.1 (Flags))。
    """

    stream_id: int
    last_group_id: int | None = None
    last_object_id: int | None = None
    last_subgroup_id: int | None = None
    last_publisher_priority: int | None = None


@dataclass(slots=True)
class _PendingRequest:
    """自側が開始した request の待ち合わせ状態。"""

    future: asyncio.Future[NativeEvent] = field(default_factory=asyncio.Future)


class Runtime:
    """1 本の WebTransport session 上で MoQT セッションを駆動する。"""

    def __init__(
        self,
        *,
        client: bool,
        implementation: str,
        ops: TransportOps,
        events: RuntimeEvents,
        on_task_error: Callable[[BaseException], Awaitable[None]] | None = None,
        control_message_timeout: float | None = None,
        data_stream_timeout: float | None = None,
    ) -> None:
        self._core = (
            _native.Session.client(implementation)
            if client
            else _native.Session.server(implementation)
        )
        # タイムアウトは既定で無効である。設定すると tick が期限を判定し、期限切れの
        # セッションを SESSION_CONTROL_MESSAGE_TIMEOUT / SESSION_DATA_STREAM_TIMEOUT で
        # 終了する (draft-ietf-moq-transport-21 §12.2 (Session Termination Codes))。
        if control_message_timeout is not None:
            self._core.set_control_message_timeout_ms(_to_milliseconds(control_message_timeout))
        if data_stream_timeout is not None:
            self._core.set_data_stream_timeout_ms(_to_milliseconds(data_stream_timeout))
        self._ops = ops
        self._events = events
        self._on_task_error = on_task_error

        # stream_id ごとの状態
        self._streams: dict[int, StreamInfo] = {}
        # 自側制御ストリームの ID
        self._local_control_stream_id: int | None = None
        # 自側が開始した request の待ち合わせ (Request ID 索引)
        self._pending_requests: dict[int, _PendingRequest] = {}
        # 自側が開始した request の request stream ID (Request ID 索引)
        self._request_streams: dict[int, int] = {}
        # 自側が開始したストリーム ID
        self._local_streams: set[int] = set()
        # stream type を通知済みのデータストリーム
        self._data_stream_types: dict[int, int] = {}
        # 自側が開始した subgroup ストリームの送信状態 (Request ID 索引)
        self._subgroups: dict[int, SubgroupWriter] = {}
        # 自側が開いた fetch stream の送信状態 (stream ID 索引)
        self._fetch_streams: dict[int, FetchWriter] = {}
        self._closed = False

    # ─── 状態 ───────────────────────────────────────────────

    def subscription_track_alias(self, request_id: int) -> int:
        """subscription の Track Alias を返す。未確定の場合は 0 を返す。"""
        return self._core.subscription_track_alias(request_id) or 0

    @property
    def established(self) -> bool:
        """SETUP 交換が完了しているかを返す。"""
        return self._core.established

    @property
    def closed(self) -> bool:
        """セッションが閉じられたかを返す。"""
        return self._closed

    # ─── 開始と終了 ─────────────────────────────────────────

    async def start(self) -> None:
        """制御ストリームを開いて SETUP を送信する。"""
        stream_id = await self._ops.open_uni_stream()
        if stream_id < 0:
            raise ConnectionError("failed to open the local MoQT control stream")
        self._local_control_stream_id = stream_id
        self._local_streams.add(stream_id)
        self._streams[stream_id] = StreamInfo(kind=_STREAM_CONTROL)
        await self._ops.send_stream_data(stream_id, self._core.start(), False)

    async def close(self, code: int = 0, reason: str = "") -> None:
        """MoQT セッションを閉じる。"""
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await self._apply_events(self._core.close(code, reason))
        await self._ops.close(code, reason)

    # ─── 受信 ───────────────────────────────────────────────

    async def receive_stream(self, stream_id: int, data: bytes) -> None:
        """WebTransport の受信データをストリーム種別に振り分ける。"""
        info = self._streams.get(stream_id)
        if info is None:
            info = self._classify_stream(stream_id, data)
            if info is None:
                # stream type の varint が途中の場合は次の断片で判定する
                return

        if info.kind == _STREAM_CONTROL:
            await self._apply_events(self._core.receive_control(data))
        elif info.kind == _STREAM_REQUEST:
            role = "local" if stream_id in self._local_streams else "peer"
            await self._apply_events(self._core.receive_request_stream(stream_id, data, role))
        else:
            # 単方向ストリームは先頭に stream type の varint を持つ。生バイト列は
            # そのままネイティブ実装へ渡し、種別だけを最初の断片で通知する
            stream_type = self._data_stream_types.get(stream_id)
            if stream_type is None:
                stream_type = _decode_first_varint(data)
                if stream_type is None:
                    # varint が途中の場合は次の断片で判定する
                    return
                self._data_stream_types[stream_id] = stream_type
            _objects, events = self._core.receive_data_stream(stream_id, data, stream_type)
            await self._apply_events(events)

    async def receive_stream_closed(
        self,
        stream_id: int,
        error_code: int | None = None,
    ) -> None:
        """WebTransport のストリーム終端を状態機械へ通知する。

        トランスポートの終了に伴う終端は、状態機械がプロトコル違反として拒否することが
        ある。例えば制御ストリームは session の生存中に閉じてはならないため
        (draft-ietf-moq-transport-21 §6.4.1 (Control Streams))、WebTransport session の
        終了と前後して届いた FIN は違反として扱われる。これはアプリケーションが
        対処できる失敗ではないので、例外を送出せずセッションの終了として扱う。
        """
        info = self._streams.pop(stream_id, None)
        self._data_stream_types.pop(stream_id, None)
        if info is None:
            return
        reset = error_code is not None
        try:
            if info.kind == _STREAM_CONTROL:
                await self._apply_events(
                    self._core.receive_control_stream_closed(reset, error_code)
                )
            elif info.kind == _STREAM_REQUEST:
                await self._apply_events(
                    self._core.receive_request_stream_closed(stream_id, reset, error_code)
                )
            else:
                await self._apply_events(
                    self._core.receive_data_stream_closed(stream_id, reset, error_code)
                )
        except Exception as error:
            logger.debug("MoQT stream close was rejected: stream=%s error=%s", stream_id, error)
            await self._finish_session(0, str(error))

    async def receive_datagram(self, data: bytes) -> None:
        """WebTransport のデータグラムを状態機械へ渡す。"""
        await self._apply_events(self._core.receive_datagram(data))

    # ─── 定期処理 ───────────────────────────────────────────

    async def tick(self) -> None:
        """タイムアウトを判定する。"""
        if self._closed:
            return
        now_ms = int(asyncio.get_running_loop().time() * 1000)
        await self._apply_events(self._core.tick(now_ms))

    # ─── 要求 ───────────────────────────────────────────────

    async def subscribe(
        self,
        namespace: Sequence[bytes],
        track_name: bytes,
        parameters: dict[int, object] | None = None,
    ) -> tuple[int, NativeEvent]:
        """SUBSCRIBE を送信し、応答を待つ。"""
        merged = dict(parameters or {})
        merged.setdefault(_native.PARAM_SUBSCRIBER_PRIORITY, DEFAULT_SUBSCRIBER_PRIORITY)
        return await self._start_request(
            lambda request_id: self._core.send_subscribe(list(namespace), track_name, merged)
        )

    async def publish(
        self,
        namespace: Sequence[bytes],
        track_name: bytes,
        track_alias: int,
        parameters: dict[int, object] | None = None,
        track_properties: dict[int, object] | None = None,
    ) -> tuple[int, NativeEvent]:
        """PUBLISH を送信し、応答を待つ。"""
        return await self._start_request(
            lambda request_id: self._core.send_publish(
                list(namespace),
                track_name,
                track_alias,
                dict(parameters or {}),
                dict(track_properties or {}),
            )
        )

    async def fetch(
        self,
        namespace: Sequence[bytes],
        track_name: bytes,
        parameters: dict[int, object] | None = None,
        on_request_id: Callable[[int], None] | None = None,
    ) -> tuple[int, NativeEvent]:
        """FETCH を送信し、FETCH_OK を待つ。

        取得範囲は LOCATION_FILTER パラメータで指定する。`on_request_id` は
        Request ID が確定した時点で呼ばれる。FETCH_OK より先に fetch stream の
        オブジェクトが届く場合に備え、応答を待つ前に登録するために使う。
        """
        return await self._start_request(
            lambda request_id: self._core.send_fetch(
                list(namespace), track_name, dict(parameters or {})
            ),
            on_request_id=on_request_id,
        )

    async def track_status(
        self,
        namespace: Sequence[bytes],
        track_name: bytes,
        parameters: dict[int, object] | None = None,
    ) -> tuple[int, NativeEvent]:
        """TRACK_STATUS を送信し、応答を待つ。"""
        return await self._start_request(
            lambda request_id: self._core.send_track_status(
                list(namespace), track_name, dict(parameters or {})
            )
        )

    async def send_request_update(
        self,
        request_id: int,
        parameters: dict[int, object] | None = None,
    ) -> None:
        """REQUEST_UPDATE を送信し、REQUEST_OK の受信を待つ。

        応答は同じ request stream で届くため、待ち合わせには `_start_request` を
        使えない (Request ID は状態機械が採番済みである)。
        """
        pending = _PendingRequest()
        self._pending_requests[request_id] = pending
        try:
            await self._apply_events(
                self._core.send_request_update(request_id, dict(parameters or {}))
            )
            await pending.future
        finally:
            self._pending_requests.pop(request_id, None)

    async def send_publish_state_notify(
        self,
        request_id: int,
        parameters: dict[int, object] | None = None,
    ) -> None:
        """PUBLISH_STATE_NOTIFY を送信する。"""
        await self._apply_events(
            self._core.send_publish_state_notify(request_id, dict(parameters or {}))
        )

    async def open_fetch_stream(self, request_id: int) -> int:
        """fetch 応答用の単方向ストリームを開き、状態機械へ登録する。

        Returns:
            ストリーム ID
        """
        stream_id = await self._ops.open_uni_stream()
        if stream_id < 0:
            raise ConnectionError("failed to open a fetch stream")
        await self._apply_events(self._core.send_fetch_header(stream_id, request_id))
        await self._ops.send_stream_data(stream_id, _encode_fetch_header(request_id), False)
        self._local_streams.add(stream_id)
        self._streams[stream_id] = StreamInfo(kind=_STREAM_DATA)
        self._fetch_streams[stream_id] = FetchWriter(stream_id=stream_id)
        return stream_id

    async def send_fetch_stream_object(
        self,
        stream_id: int,
        group_id: int,
        object_id: int,
        payload: bytes,
        *,
        publisher_priority: int = 128,
        subgroup_id: int = 0,
    ) -> None:
        """開いた fetch stream へオブジェクトを書き込む。"""
        writer = self._fetch_streams.get(stream_id)
        if writer is None:
            raise MoqtError(f"fetch stream {stream_id} is not open")
        data = _encode_fetch_object(
            writer,
            group_id,
            object_id,
            payload,
            publisher_priority,
            subgroup_id,
        )
        await self._apply_events(self._core.send_fetch_object(stream_id))
        await self._ops.send_stream_data(stream_id, data, False)
        writer.last_group_id = group_id
        writer.last_object_id = object_id
        writer.last_subgroup_id = subgroup_id
        writer.last_publisher_priority = publisher_priority

    async def close_fetch_stream(self, stream_id: int) -> None:
        """fetch stream を終了する。"""
        self._fetch_streams.pop(stream_id, None)
        await self._ops.send_stream_data(stream_id, b"", True)
        await self._apply_events(self._core.send_fetch_data_stream_closed(stream_id))

    async def send_fetch_ok(
        self,
        request_id: int,
        end_location: tuple[int, int],
        *,
        end_of_track: bool = False,
        parameters: dict[int, object] | None = None,
        track_properties: dict[int, object] | None = None,
    ) -> None:
        """FETCH_OK を送信する。"""
        await self._apply_events(
            self._core.send_fetch_ok(
                request_id,
                end_of_track,
                end_location,
                dict(parameters or {}),
                dict(track_properties or {}),
            )
        )

    async def _start_request(
        self,
        send: Callable[[int], Iterable[NativeEvent]],
        on_request_id: Callable[[int], None] | None = None,
    ) -> tuple[int, NativeEvent]:
        """自側が開始する request を送信し、応答を待つ。

        Request ID はプロトコル状態機械が採番する。I/O 層は `send_request`
        イベントで通知される ID をストリームへ対応付ける。
        """
        pending = _PendingRequest()
        request_id: int | None = None
        events = send(0)
        for event in events:
            if event.kind == "send_request" and event.request_id is not None:
                request_id = event.request_id
                self._pending_requests[request_id] = pending
                if on_request_id is not None:
                    on_request_id(request_id)
            await self._apply_events([event])
        if request_id is None:
            raise MoqtError("request message did not produce a send_request event")
        try:
            return request_id, await pending.future
        except SessionClosedError:
            self._pending_requests.pop(request_id, None)
            raise
        except BaseException:
            self._pending_requests.pop(request_id, None)
            raise

    async def send_request_ok(
        self,
        request_id: int,
        parameters: dict[int, object] | None = None,
        track_properties: dict[int, object] | None = None,
    ) -> None:
        """REQUEST_OK を送信する。"""
        await self._apply_events(
            self._core.send_request_ok(
                request_id, dict(parameters or {}), dict(track_properties or {})
            )
        )

    async def send_subscribe_ok(
        self,
        request_id: int,
        track_alias: int,
        parameters: dict[int, object] | None = None,
        track_properties: dict[int, object] | None = None,
    ) -> None:
        """SUBSCRIBE_OK を送信する。"""
        await self._apply_events(
            self._core.send_subscribe_ok(
                request_id,
                track_alias,
                dict(parameters or {}),
                dict(track_properties or {}),
            )
        )

    async def send_request_error(
        self,
        request_id: int,
        error_code: int,
        reason: str,
        retry_interval: int = 0,
    ) -> None:
        """REQUEST_ERROR を送信する。"""
        await self._apply_events(
            self._core.send_request_error(request_id, error_code, retry_interval, reason)
        )

    async def send_publish_done(
        self,
        request_id: int,
        status_code: int,
        reason: str = "",
    ) -> None:
        """PUBLISH_DONE を送信する。"""
        await self._apply_events(
            self._core.send_publish_done_for_subscription(request_id, status_code, reason)
        )

    async def send_goaway(self, timeout: int = 0) -> None:
        """GOAWAY を送信する。"""
        await self._apply_events(self._core.send_goaway(b"", timeout))

    async def stop_sending(self, request_id: int) -> None:
        """subscription を終了する (subscriber 側の STOP_SENDING)。"""
        await self._apply_events(self._core.stop_sending(request_id))

    # ─── オブジェクト送信 ───────────────────────────────────

    async def send_subgroup_object(
        self,
        request_id: int,
        track_alias: int,
        group_id: int,
        object_id: int,
        payload: bytes,
        *,
        subgroup_id: int | None = None,
        publisher_priority: int | None = None,
        end_of_group: bool = False,
        status: int | None = None,
        properties_data: bytes | None = None,
    ) -> None:
        """subgroup ストリームでオブジェクトを送信する。

        同じ Request ID と Group ID のストリームが既にあれば再利用する。
        Object ID はストリーム内で差分として表現されるため、直前の値との差を書く。

        `properties_data` を渡す場合、そのストリームの最初のオブジェクトで
        Properties の有無がヘッダに固定される
        (draft-ietf-moq-transport-21 §11.3.1 (Subgroup Header))。ヘッダが Properties を
        持たないストリームへ途中から渡すと `MoqtError` になる。
        """
        # 状態を進める前にバイト列を組み立て、不正な組み合わせでは送信も状態更新もしない
        writer = self._subgroups.get(request_id)
        opens_stream = writer is None or writer.group_id != group_id
        if properties_data is not None and not opens_stream and not writer.has_properties:
            raise MoqtError(
                f"stream for request {request_id} carries objects without properties; "
                "properties must be set on the first object of a subgroup"
            )
        delta = (
            object_id
            if writer is None or writer.last_object_id is None
            else object_id - writer.last_object_id - 1
        )
        data = _encode_subgroup_object(delta, payload, status, properties_data)

        if opens_stream:
            if writer is not None:
                await self._finish_subgroup_writer(request_id, writer)
            writer = await self._open_subgroup(
                request_id,
                track_alias,
                group_id,
                subgroup_id,
                publisher_priority,
                end_of_group,
                has_properties=properties_data is not None,
            )
            self._subgroups[request_id] = writer

        allowed, events = self._core.send_subgroup_object(writer.stream_id, object_id, None)
        await self._apply_events(events)
        if not allowed:
            # ローカルのフィルタで破棄するオブジェクトは送信しない
            return

        writer.last_object_id = object_id
        await self._ops.send_stream_data(writer.stream_id, data, False)

    async def _open_subgroup(
        self,
        request_id: int,
        track_alias: int,
        group_id: int,
        subgroup_id: int | None,
        publisher_priority: int | None,
        end_of_group: bool,
        *,
        has_properties: bool = False,
    ) -> SubgroupWriter:
        """新しい subgroup ストリームを開いてヘッダを書き込む。"""
        stream_id = await self._ops.open_uni_stream()
        if stream_id < 0:
            raise ConnectionError("failed to open a subgroup stream")
        await self._apply_events(
            self._core.send_subgroup_header(
                stream_id,
                request_id,
                track_alias,
                group_id,
                subgroup_id,
                publisher_priority,
                has_properties,
                end_of_group,
                False,
            )
        )
        header = _encode_subgroup_header(
            track_alias,
            group_id,
            subgroup_id,
            publisher_priority,
            has_properties=has_properties,
            end_of_group=end_of_group,
        )
        await self._ops.send_stream_data(stream_id, header, False)
        self._local_streams.add(stream_id)
        self._streams[stream_id] = StreamInfo(kind=_STREAM_DATA)
        return SubgroupWriter(stream_id=stream_id, group_id=group_id, has_properties=has_properties)

    async def _finish_subgroup_writer(self, request_id: int, writer: SubgroupWriter) -> None:
        """subgroup ストリームを FIN で終了する。"""
        self._subgroups.pop(request_id, None)
        await self._ops.send_stream_data(writer.stream_id, b"", True)
        await self._apply_events(self._core.send_data_stream_closed(writer.stream_id, False, None))

    async def finish_subgroup(self, request_id: int) -> None:
        """送信中の subgroup ストリームを終了する。"""
        writer = self._subgroups.get(request_id)
        if writer is not None:
            await self._finish_subgroup_writer(request_id, writer)

    async def send_object_datagram(
        self,
        request_id: int,
        group_id: int,
        object_id: int,
        payload: bytes,
        publisher_priority: int | None = None,
        properties_data: bytes | None = None,
        status: int | None = None,
    ) -> None:
        """オブジェクトデータグラムを送信する。"""
        track_alias = self._core.subscription_track_alias(request_id)
        if track_alias is None:
            raise MoqtError(f"subscription {request_id} has no track alias")
        # 状態機械へ通知する前にバイト列を組み立て、不正な組み合わせでは送信しない
        datagram = _encode_object_datagram(
            track_alias,
            group_id,
            object_id,
            payload,
            publisher_priority,
            properties_data=properties_data,
            status=status,
        )
        if len(datagram) > moqt.MAX_DATAGRAM_SIZE:
            # 上限を超えたデータグラムは経路によっては通知なく破棄され、送信側から
            # 検知できない (draft-ietf-moq-transport-21 §11.2.1 (Object Datagram))。
            # 原因が分からないまま受信待ちで止まらないよう、送信前に警告する
            logger.warning(
                "MoQT datagram size %d exceeds the portable limit %d; "
                "it may be dropped by the path without notification. "
                "Use a subgroup stream for larger objects.",
                len(datagram),
                moqt.MAX_DATAGRAM_SIZE,
            )
        allowed, events = self._core.send_object_datagram(
            request_id, group_id, object_id, properties_data, None
        )
        await self._apply_events(events)
        if not allowed:
            # ローカルのフィルタで破棄するオブジェクトは送信しない
            return
        await self._ops.send_datagram(datagram)

    # ─── ストリーム種別の判定 ───────────────────────────────

    def _classify_stream(self, stream_id: int, data: bytes) -> StreamInfo | None:
        """未登録ストリームの種別を判定する。

        MoQT の双方向ストリームは request stream、単方向ストリームは制御ストリーム
        または data stream である。QUIC のストリーム ID は下位 2 ビットで向きを
        表すため (RFC 9000 §2.1)、データ本体を読む前に判定できる。
        """
        if _is_bidirectional(stream_id):
            info = StreamInfo(kind=_STREAM_REQUEST)
            self._streams[stream_id] = info
            return info

        # 単方向ストリームは先頭の stream type で種別が決まる
        stream_type = _decode_first_varint(data)
        if stream_type is None:
            return None
        if stream_type == SETUP_STREAM_TYPE:
            info = StreamInfo(kind=_STREAM_CONTROL)
        else:
            info = StreamInfo(kind=_STREAM_DATA, stream_type=stream_type)
        self._streams[stream_id] = info
        return info

    # ─── イベント処理 ───────────────────────────────────────

    async def _apply_events(
        self,
        events: Iterable[NativeEvent],
        *,
        request_id: int | None = None,
    ) -> None:
        """native のイベントを I/O とアプリケーションへ振り分ける。"""
        for event in events:
            kind = event.kind
            if kind == "send_control":
                await self._send_control(event)
            elif kind == "send_request":
                await self._send_request(event, request_id)
            elif kind == "send_on_stream":
                await self._send_on_stream(event)
            elif kind == "reset_request_stream":
                await self._reset_request_stream(event)
            elif kind == "stop_sending_request_stream":
                await self._stop_sending_request_stream(event)
            elif kind == "established":
                await self._notify(self._events.on_established)
            elif kind == "close":
                await self._handle_close(event)
            elif kind == "object":
                await self._handle_object(event)
            elif kind in {
                "end_of_non_existent_range",
                "end_of_unknown_range",
                "end_of_timed_out_range",
            }:
                await self._notify(self._events.on_fetch_end, kind, event)
            elif kind in {"reset_data_stream", "send_padding_stream", "send_padding_datagram"}:
                await self._handle_data_control(event)
            elif kind in {"accepted", "unknown_track_alias", "discarded", "filtered_out"}:
                # データグラムの受理結果は送信側では使わない
                pass
            else:
                await self._handle_message_event(kind, event)

    async def _send_control(self, event: NativeEvent) -> None:
        """制御ストリームへメッセージを書き込む。"""
        stream_id = self._local_control_stream_id
        if stream_id is None:
            raise MoqtError("local control stream is not open")
        await self._ops.send_stream_data(stream_id, _event_bytes(event, "data"), False)

    async def _send_request(self, event: NativeEvent, request_id: int | None) -> None:
        """新しい bidi request stream を開いてメッセージを書き込む。"""
        stream_id = await self._ops.open_bidi_stream()
        if stream_id < 0:
            raise ConnectionError("failed to open a request stream")
        # 応答メッセージは Request ID を運ばないため、ストリームとの対応を登録する
        actual_request_id = request_id if request_id is not None else event.request_id
        if actual_request_id is None:
            raise MoqtError("send_request event without a request id")
        # 応答は Python 側から report されるため、ストリームは常に応答として扱う
        self._core.register_local_request_stream(stream_id, actual_request_id)
        self._local_streams.add(stream_id)
        self._request_streams[actual_request_id] = stream_id
        self._streams[stream_id] = StreamInfo(kind=_STREAM_REQUEST, request_id=actual_request_id)
        await self._ops.send_stream_data(stream_id, _event_bytes(event, "message_data"), False)

    async def _send_on_stream(self, event: NativeEvent) -> None:
        """既存の request stream へ応答を書き込む。"""
        request_id = event.request_id
        stream_id = self._request_streams.get(request_id) if request_id is not None else None
        # 自側が開始していない request への応答は、受信ストリームをそのまま使う
        if stream_id is None:
            stream_id = self._find_incoming_request_stream(request_id)
        if stream_id is None:
            raise MoqtError(f"no request stream for request id {request_id}")
        fin = bool(event.fin)
        await self._ops.send_stream_data(stream_id, _event_bytes(event, "message_data"), fin)
        if fin:
            if request_id is not None:
                self._request_streams.pop(request_id, None)
            self._streams.pop(stream_id, None)

    def _find_incoming_request_stream(self, request_id: int | None) -> int | None:
        """peer から届いた request stream を Request ID から引く。"""
        if request_id is None:
            return None
        for stream_id, info in self._streams.items():
            if info.kind == _STREAM_REQUEST and info.request_id == request_id:
                return stream_id
        return None

    async def _reset_request_stream(self, event: NativeEvent) -> None:
        """request stream を RESET_STREAM で終了する。"""
        request_id = event.request_id
        stream_id = self._request_streams.pop(request_id, None) if request_id is not None else None
        if stream_id is None:
            stream_id = self._find_incoming_request_stream(request_id)
        if stream_id is None:
            return
        self._streams.pop(stream_id, None)
        with contextlib.suppress(Exception):
            await self._ops.reset_stream(stream_id, int(event.code or 0))

    async def _stop_sending_request_stream(self, event: NativeEvent) -> None:
        """request stream の受信方向へ STOP_SENDING を送る。"""
        request_id = event.request_id
        stream_id = self._request_streams.get(request_id) if request_id is not None else None
        if stream_id is None:
            stream_id = self._find_incoming_request_stream(request_id)
        if stream_id is None:
            return
        with contextlib.suppress(Exception):
            await self._ops.stop_sending(stream_id, int(event.code or 0))

    async def _handle_data_control(self, event: NativeEvent) -> None:
        """データストリームの制御イベントを処理する。"""
        if event.kind == "reset_data_stream":
            stream_id = event.stream_id
            if stream_id is not None:
                with contextlib.suppress(Exception):
                    await self._ops.reset_stream(stream_id, int(event.code or 0))
        elif event.kind == "send_padding_stream":
            length = _message_int(event, "length")
            stream_id = await self._ops.open_uni_stream()
            if stream_id >= 0:
                await self._ops.send_stream_data(
                    stream_id, moqt.encode_varint(PADDING_STREAM_TYPE) + bytes(length), True
                )
        elif event.kind == "send_padding_datagram":
            length = _message_int(event, "length")
            await self._ops.send_datagram(moqt.encode_varint(PADDING_DATAGRAM_TYPE) + bytes(length))

    async def _handle_object(self, event: NativeEvent) -> None:
        """受信したオブジェクトをアプリケーションへ通知する。"""
        acceptance = event.acceptance
        if acceptance != "accepted":
            logger.debug(
                "dropped MoQT object: stream=%s object=%s reason=%s",
                event.stream_id,
                event.object_id,
                acceptance,
            )
            return
        await self._notify(self._events.on_object, event.stream_id, event, event.data)

    async def _handle_message_event(self, kind: str, event: NativeEvent) -> None:
        """アプリケーションへ通知するイベントを振り分ける。"""
        if kind in {"request_ok", "fetch_ok"}:
            # FETCH_OK は request の応答だが、状態機械は request_ok ではなく
            # fetch_ok として通知する。どちらも待っている request を解決する。
            await self._resolve_request(event)
            await self._notify(self._events.on_request_ok, event)
        elif kind == "request_error":
            await self._reject_request(event)
            await self._notify(self._events.on_request_error, event)
        elif kind == "request_terminated":
            await self._notify(self._events.on_request_terminated, event)
        elif kind == "request_update":
            # peer からの REQUEST_UPDATE には応答が必須である
            # (draft-ietf-moq-transport-21 §9.5 (REQUEST_UPDATE))。
            # アプリのコールバックを先に呼び、例外を送出した場合は REQUEST_ERROR で拒否する
            await self._respond_request_update(event)
        elif kind == "publish_done":
            await self._notify(self._events.on_publish_done, event)
        elif kind == "publish_state_notify":
            # 状態機械が購読の状態へ反映済みであり、応答は不要である
            # (draft-ietf-moq-transport-21 §9.10 (PUBLISH_STATE_NOTIFY))。
            # アプリが通知を観測できるようにする
            await self._notify(self._events.on_publish_state_notify, event)
        elif kind == "goaway":
            await self._notify(self._events.on_goaway, event)
        elif kind in {
            "subscribe",
            "publish",
            "fetch",
            "track_status",
        }:
            # peer から届いた request は、そのストリームを応答用に登録しておく
            self._remember_incoming_request(event)
            await self._notify(self._events.on_request, event)
        else:
            logger.warning("unhandled MoQT event: %s", kind)

    async def _respond_request_update(self, event: NativeEvent) -> None:
        """受信した REQUEST_UPDATE へ応答する。

        アプリのコールバックが例外を送出した場合は REQUEST_ERROR で拒否する。
        それ以外はパラメータを付けずに REQUEST_OK で受け入れる。

        REQUEST_UPDATE_OK で許可されるパラメータは EXPIRES と LARGEST_OBJECT だけ
        である (moqt-rs の `REQUEST_UPDATE_OK_ALLOWED_PARAMS`)。
        """
        request_id = event.request_id
        if request_id is None:
            logger.warning("REQUEST_UPDATE without a request id was ignored")
            return
        callback = self._events.on_request_update
        if callback is not None:
            # 拒否の意思表示はコールバックの例外で表す。_notify は例外を握り潰すため
            # ここでは直接呼び出す
            try:
                await callback(event)
            except Exception:
                logger.exception("the application rejected a REQUEST_UPDATE")
                await self.send_request_error(
                    request_id, moqt.REQUEST_NOT_SUPPORTED, "update rejected"
                )
                return
        await self.send_request_ok(request_id)

    def _remember_incoming_request(self, event: NativeEvent) -> None:
        """peer から届いた request のストリームを Request ID から引けるようにする。"""
        stream_id = event.stream_id
        if stream_id is None or event.request_id is None:
            return
        info = self._streams.get(stream_id)
        if info is None:
            self._streams[stream_id] = StreamInfo(kind=_STREAM_REQUEST, request_id=event.request_id)
            return
        info.request_id = event.request_id

    async def _resolve_request(self, event: NativeEvent) -> None:
        """自側が待っている request の応答を解決する。"""
        request_id = event.request_id
        if request_id is None:
            return
        pending = self._pending_requests.pop(request_id, None)
        if pending is not None and not pending.future.done():
            pending.future.set_result(event)

    async def _reject_request(self, event: NativeEvent) -> None:
        """自側が待っている request の失敗を解決する。"""
        request_id = event.request_id
        if request_id is None:
            return
        pending = self._pending_requests.pop(request_id, None)
        if pending is None or pending.future.done():
            return
        body = event.message or {}
        pending.future.set_exception(
            MoqtError(f"request {request_id} failed: {body.get('error_code')} {body.get('reason')}")
        )

    async def _handle_close(self, event: NativeEvent) -> None:
        """セッション終了を処理する。"""
        logger.debug("MoQT session closed: code=%s reason=%s", event.code, event.reason)
        await self._finish_session(int(event.code or 0), str(event.reason or ""))

    async def _finish_session(self, code: int, reason: str) -> None:
        """セッションを終了状態にし、待ち合わせとアプリケーションへ通知する。"""
        if self._closed:
            return
        self._closed = True
        for pending in self._pending_requests.values():
            if not pending.future.done():
                pending.future.set_exception(SessionClosedError(code, reason))
        self._pending_requests.clear()
        await self._notify(self._events.on_close, code, reason)

    async def _notify(self, callback: Callable[..., Awaitable[None]] | None, *args: object) -> None:
        """コールバックを 1 回呼ぶ。例外はタスクエラーとして通知する。"""
        if callback is None:
            return
        try:
            await callback(*args)
        except Exception as error:
            logger.exception("MoQT callback failed")
            if self._on_task_error is not None:
                await self._on_task_error(error)


# ─── エンコード補助 ─────────────────────────────────────────
#
# 制御メッセージは moqt-rs がエンコードするが、データストリームのヘッダと
# オブジェクトは I/O 層が組み立てる。
# - Subgroup Header: draft-ietf-moq-transport-21 §11.3.1
# - Subgroup Object: draft-ietf-moq-transport-21 §11.3.2
# - Object Datagram: draft-ietf-moq-transport-21 §11.2.1


def _event_bytes(event: NativeEvent, key: str) -> bytes:
    """イベントのバイト列属性を取り出す。"""
    if key == "data":
        value = event.data
    elif key == "message_data":
        value = event.message_data
    else:
        raise MoqtError(f"unknown byte attribute: {key}")
    if value is None:
        raise MoqtError(f"event {event.kind} has no {key}")
    return value


def _message_int(event: NativeEvent, key: str) -> int:
    """イベントのメッセージ本体から整数を取り出す。"""
    body = event.message
    if body is None:
        raise MoqtError(f"event {event.kind} has no message body")
    value = body.get(key)
    if not isinstance(value, int):
        raise MoqtError(f"event {event.kind} has no integer field {key}")
    return value


def _to_milliseconds(seconds: float) -> int:
    """秒をミリ秒の整数へ変換する。

    状態機械はミリ秒で期限を判定する。0 以下の値は期限を即時にするため、
    呼び出し側の意図しない設定を避けて 1 ms を下限にする。
    """
    return max(1, round(seconds * 1000))


def _is_bidirectional(stream_id: int) -> bool:
    """QUIC のストリーム ID が双方向ストリームを表すかを返す。

    RFC 9000 §2.1: 下位 2 ビットの bit1 が 0 なら双方向、1 なら単方向である。
    """
    return stream_id & 0b10 == 0


def _decode_first_varint(data: bytes) -> int | None:
    """先頭の vi64 の値だけを返す。途中で切れている場合は `None` を返す。

    vi64 のデコードは `moqt.moqt` が担う。ストリーム種別の判定では値だけが必要で、
    未完成かどうかは `None` で表す。
    """
    decoded = moqt.decode_varint_prefix(data)
    return None if decoded is None else decoded[0]


def _subgroup_type_byte(
    *,
    has_properties: bool,
    subgroup_id: int | None,
    end_of_group: bool,
    default_priority: bool,
    first_object: bool = False,
) -> int:
    """subgroup ヘッダの type byte を組み立てる (draft-ietf-moq-transport-21 §11.3.1)。

    bit 4 (0x10) は常に 1 でなければならない。SUBGROUP_ID_MODE は bits 1-2
    (mask 0x06) の 2 bit であり、Subgroup ID を明示する場合は 0b10 を置く。
    """
    type_byte = 0x10
    if has_properties:
        type_byte |= 0x01
    if subgroup_id is not None:
        # SUBGROUP_ID_MODE = 0b10 (Subgroup ID フィールドが存在する)
        type_byte |= 0x04
    if end_of_group:
        type_byte |= 0x08
    if default_priority:
        type_byte |= 0x20
    if first_object:
        type_byte |= 0x40
    return type_byte


def _encode_subgroup_header(
    track_alias: int,
    group_id: int,
    subgroup_id: int | None,
    publisher_priority: int | None,
    *,
    has_properties: bool = False,
    end_of_group: bool = False,
) -> bytes:
    """subgroup ヘッダをエンコードする (draft-ietf-moq-transport-21 §11.3.1)。"""
    type_byte = _subgroup_type_byte(
        has_properties=has_properties,
        subgroup_id=subgroup_id,
        end_of_group=end_of_group,
        default_priority=publisher_priority is None,
    )
    header = bytearray()
    header += moqt.encode_varint(type_byte)
    header += moqt.encode_varint(track_alias)
    header += moqt.encode_varint(group_id)
    if subgroup_id is not None:
        header += moqt.encode_varint(subgroup_id)
    if publisher_priority is not None:
        header.append(publisher_priority)
    return bytes(header)


def _encode_fetch_header(request_id: int) -> bytes:
    """FETCH_HEADER をエンコードする (draft-ietf-moq-transport-21 §11.4.1)。

    ストリーム先頭の stream type (0x05) と Request ID を並べる。
    """
    body = bytearray()
    body += moqt.encode_varint(FETCH_HEADER_TYPE)
    body += moqt.encode_varint(request_id)
    return bytes(body)


def _encode_fetch_object(
    writer: FetchWriter,
    group_id: int,
    object_id: int,
    payload: bytes,
    publisher_priority: int,
    subgroup_id: int,
) -> bytes:
    """fetch stream のオブジェクトをエンコードする
    (draft-ietf-moq-transport-21 §11.4.1.1 (Flags))。

    Group ID と Object ID の表現は直前のオブジェクトに依存する。

    - 先頭のオブジェクト: どちらも絶対値
    - Group が変わるとき: Group ID は差分 (`今回 - 前回 - 1`)、Object ID は絶対値
    - 同じ Group のとき: Group ID は省略 (前回を継承)、Object ID は差分 (`今回 - 前回`)

    Object ID の差分に +1 は付かない (subgroup とは異なる)。
    """
    flags = 0x03  # Subgroup ID: Explicit
    fields = bytearray()
    if writer.last_group_id is None or writer.last_object_id is None:
        flags |= 0x08  # Group ID Delta あり (先頭は絶対値)
        flags |= 0x04  # Object ID Delta あり (Group 変更時は絶対値)
        fields += moqt.encode_varint(group_id)
        fields += moqt.encode_varint(subgroup_id)
        fields += moqt.encode_varint(object_id)
    elif group_id != writer.last_group_id:
        flags |= 0x08
        flags |= 0x04
        fields += moqt.encode_varint(group_id - writer.last_group_id - 1)
        fields += moqt.encode_varint(subgroup_id)
        fields += moqt.encode_varint(object_id)
    else:
        # Group ID を省略すると直前の Group ID を継承する
        flags |= 0x04
        fields += moqt.encode_varint(subgroup_id)
        fields += moqt.encode_varint(object_id - writer.last_object_id)

    flags |= 0x10  # Publisher Priority
    body = bytearray()
    body += moqt.encode_varint(flags)
    body += fields
    body.append(publisher_priority)
    body += moqt.encode_varint(len(payload))
    body += payload
    return bytes(body)


def _object_status_to_write(status: int | None, payload: bytes) -> int | None:
    """Object Status フィールドに書く値を決める。

    ペイロード長 0 のオブジェクトは Object Status を明示しなければならない。
    非 0 長のオブジェクトは Normal 以外の status を持てない
    (draft-ietf-moq-transport-21 §11.1.2 (Object Status))。

    Returns:
        書くべき status。フィールドを書かない場合は `None`。
    """
    if status is None:
        return None if payload else moqt.OBJECT_STATUS_NORMAL
    if status not in (
        moqt.OBJECT_STATUS_NORMAL,
        moqt.OBJECT_STATUS_END_OF_GROUP,
        moqt.OBJECT_STATUS_END_OF_TRACK,
    ):
        raise MoqtError(f"unknown object status: {status:#x}")
    if payload:
        if status != moqt.OBJECT_STATUS_NORMAL:
            raise MoqtError(
                f"object status {status:#x} requires an empty payload, got {len(payload)} bytes"
            )
        # Normal は非 0 長のオブジェクトでは暗黙でありフィールドを書かない
        return None
    return status


def _properties_content(properties_data: bytes) -> bytes:
    """Properties ブロックから `Properties Length` を外して内容だけを返す。

    ワイヤ上のオブジェクトは `Properties Length | Key-Value-Pairs` を持つ
    (draft-ietf-moq-transport-21 §11.1.3 (Object Properties))。`ObjectProperties.encode`
    が返す値はこの全体であるため、オブジェクトへ書き込むときは長さ部分を分離する。

    長さが後続のバイト数と一致する場合は長さ付きとして扱い、一致しない場合は
    長さなしの内容として扱う。LOC のプロパティのように長さを含まないブロックを
    渡した場合にも対応する。
    """
    length, consumed = moqt.decode_varint(properties_data)
    if length == len(properties_data) - consumed:
        return properties_data[consumed:]
    return properties_data


def _encode_subgroup_object(
    object_id_delta: int,
    payload: bytes,
    status: int | None = None,
    properties_data: bytes | None = None,
) -> bytes:
    """subgroup オブジェクトをエンコードする。

    `object_id_delta` は最初のオブジェクトでは絶対値、以降は
    `(今回の Object ID) - (前回の Object ID) - 1` である
    (draft-ietf-moq-transport-21 §11.3.1 (Subgroup Header))。

    `properties_data` は `Properties Length | Key-Value-Pairs` の形である。
    省略した場合は Properties を書かない。
    """
    written = _object_status_to_write(status, payload)
    body = bytearray()
    body += moqt.encode_varint(object_id_delta)
    if properties_data is not None:
        # オブジェクトは `Properties Length | Key-Value-Pairs` の順に書く
        # (draft-ietf-moq-transport-21 §11.1.3 (Object Properties))。
        content = _properties_content(properties_data)
        body += moqt.encode_varint(len(content))
        body += content
    body += moqt.encode_varint(len(payload))
    if written is None:
        body += payload
    else:
        body += moqt.encode_varint(written)
    return bytes(body)


def _encode_object_datagram(
    track_alias: int,
    group_id: int,
    object_id: int,
    payload: bytes,
    publisher_priority: int | None,
    *,
    properties_data: bytes | None = None,
    end_of_group: bool = False,
    status: int | None = None,
) -> bytes:
    """オブジェクトデータグラムをエンコードする (draft-ietf-moq-transport-21 §11.2.1)。

    フィールドの並びは Type Flags、Track Alias、Group ID、Object ID である。
    bit 4 は未定義であり、設定してはならない。Object ID が 0 の場合は
    ZERO_OBJECT_ID bit を立てて Object ID フィールドを省略する。
    """
    written = _object_status_to_write(status, payload)
    type_byte = 0x00
    if properties_data is not None:
        type_byte |= 0x01
    if end_of_group:
        type_byte |= 0x02
    # Object ID が 0 の場合はフィールドを省略できる
    if object_id == 0:
        type_byte |= 0x04
    if publisher_priority is None:
        type_byte |= 0x08
    if written is not None:
        # データグラムはペイロード長を持たないため、Object Status を運ぶ場合は
        # STATUS bit を立ててペイロードが無いことを示す
        # (draft-ietf-moq-transport-21 §11.2.1 (Object Datagram))。
        type_byte |= 0x20
    datagram = bytearray()
    datagram += moqt.encode_varint(type_byte)
    datagram += moqt.encode_varint(track_alias)
    datagram += moqt.encode_varint(group_id)
    if object_id != 0:
        datagram += moqt.encode_varint(object_id)
    if publisher_priority is not None:
        datagram.append(publisher_priority)
    if properties_data is not None:
        # データグラムは `Properties Length | Key-Value-Pairs` を書く
        # (draft-ietf-moq-transport-21 §11.1.3 (Object Properties))。
        content = _properties_content(properties_data)
        datagram += moqt.encode_varint(len(content))
        datagram += content
    if written is None:
        datagram += payload
    else:
        datagram += moqt.encode_varint(written)
    return bytes(datagram)
