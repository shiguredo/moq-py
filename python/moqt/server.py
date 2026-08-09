"""WebTransport over HTTP/3 を利用する最小 MoQT server。"""

from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Self

from webtransport import h3

from moqt._native import _CoreEvent, _CoreSession

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@dataclass(frozen=True, slots=True)
class ServerSession:
    """確立した MoQT server session の識別情報。"""

    session_id: int
    address: tuple[str, int]


@dataclass(slots=True)
class _Connection:
    """1 本の WebTransport session に対応する MoQT の内部状態。"""

    core: _CoreSession
    local_control_stream_id: int | None = None
    peer_control_stream_id: int | None = None


class Server:
    """WebTransport 接続上で MoQT SETUP を交換する server。"""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        certfile: str,
        keyfile: str,
        allowed_origins: list[str] | None = None,
        implementation: str = "moqt-py",
    ) -> None:
        self._transport = h3.Server(
            host=host,
            port=port,
            certfile=certfile,
            keyfile=keyfile,
            allowed_origins=allowed_origins,
        )
        self._implementation = implementation
        self._connections: dict[tuple[tuple[str, int], int], _Connection] = {}
        self._on_session_established: Callable[[ServerSession], Awaitable[None]] | None = None
        self._transport.on_session_ready(self._on_session_ready)
        self._transport.on_session_closed(self._on_session_closed)
        self._transport.on_stream_data(self._on_stream_data)

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

    async def start(self) -> None:
        """WebTransport server を開始する。"""
        await self._transport.start()

    async def run(self) -> None:
        """停止されるまで WebTransport server の受信ループを実行する。"""
        await self._transport.run()

    async def stop(self) -> None:
        """全接続と WebTransport server を停止する。"""
        self._connections.clear()
        await self._transport.stop()

    async def _on_session_ready(self, session_id: int, address: tuple[str, int]) -> None:
        """WebTransport session ごとに server role の MoQT Session を開始する。"""
        key = (address, session_id)
        if key in self._connections:
            raise RuntimeError(f"duplicate WebTransport session: {session_id} from {address}")

        connection = _Connection(core=_CoreSession.server(self._implementation))
        self._connections[key] = connection
        stream_id = await self._transport.open_stream(
            address,
            session_id,
            unidirectional=True,
        )
        if stream_id < 0:
            del self._connections[key]
            raise ConnectionError(
                f"failed to open the local MoQT control stream: {session_id} from {address}"
            )
        connection.local_control_stream_id = stream_id
        await self._transport.send_stream_data(address, stream_id, connection.core.start())

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
        """peer 制御ストリームの受信データを Sans I/O facade へ渡す。"""
        key = (address, session_id)
        connection = self._connections.get(key)
        if connection is None:
            raise RuntimeError(f"unknown WebTransport session: {session_id} from {address}")
        if connection.peer_control_stream_id is None:
            connection.peer_control_stream_id = stream_id
        elif connection.peer_control_stream_id != stream_id:
            raise RuntimeError(
                "received a non-control stream before the minimum SETUP handshake completed"
            )

        events = connection.core.receive_control(data)
        await self._consume_events(events, session_id, address)

    async def _consume_events(
        self,
        events: list[_CoreEvent],
        session_id: int,
        address: tuple[str, int],
    ) -> None:
        """native session event を server の状態へ反映する。"""
        for event in events:
            if event.kind == "established":
                if self._on_session_established is not None:
                    await self._on_session_established(ServerSession(session_id, address))
            elif event.kind == "close":
                raise ConnectionError(f"peer closed MoQT session: {event.code} {event.reason}")
            elif event.kind == "send_control":
                raise NotImplementedError(
                    "sending control messages after SETUP is not implemented yet"
                )
            else:
                raise RuntimeError(f"unknown native session event: {event.kind}")

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.stop()


__all__ = ["Server", "ServerSession"]
