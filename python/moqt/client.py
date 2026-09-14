"""WebTransport over HTTP/3 を利用する最小 MoQT client。"""

from __future__ import annotations

import asyncio
from types import TracebackType

from webtransport import h3

from moqt._native import _CoreEvent, _CoreSession


class Client:
    """WebTransport 接続上で MoQT SETUP を交換する client。"""

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
        self._core = _CoreSession.client(implementation)
        self._transport.on_stream_data(self._on_stream_data)
        self._transport.on_session_closed(self._on_session_closed)
        self._established_event = asyncio.Event()
        self._connect_error: BaseException | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._peer_control_stream_id: int | None = None

    @property
    def established(self) -> bool:
        """MoQT SETUP 交換が完了しているかを返す。"""
        return self._core.established

    async def connect(self, timeout: float = 10.0) -> None:
        """WebTransport へ接続し、MoQT SETUP 交換の完了を待つ。"""
        if self._run_task is not None:
            raise RuntimeError("client has already been started")

        # 接続に失敗した場合は webtransport-py が具体的な例外を送出する
        await self._transport.connect(timeout=timeout)

        control_stream_id = await self._transport.open_stream(unidirectional=True)
        if control_stream_id < 0:
            await self._transport.close()
            raise ConnectionError("failed to open the local MoQT control stream")

        await self._transport.send_stream_data(control_stream_id, self._core.start())
        self._run_task = asyncio.create_task(self._transport.run())
        self._run_task.add_done_callback(self._on_run_done)

        try:
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
        await self._transport.close()
        if self._run_task is not None:
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
            finally:
                self._run_task = None

    async def _on_stream_data(self, stream_id: int, data: bytes) -> None:
        """peer 制御ストリームの受信データを Sans I/O facade へ渡す。"""
        if self._peer_control_stream_id is None:
            self._peer_control_stream_id = stream_id
        elif self._peer_control_stream_id != stream_id:
            self._fail_connect(
                RuntimeError(
                    "received a non-control stream before the minimum SETUP handshake completed"
                )
            )
            return

        try:
            events = self._core.receive_control(data)
        except BaseException as error:
            self._fail_connect(error)
            return
        self._consume_events(events)

    async def _on_session_closed(self, session_id: int) -> None:
        """SETUP 完了前の WebTransport session close を接続失敗として扱う。"""
        if not self.established:
            self._fail_connect(
                ConnectionError(
                    f"WebTransport session {session_id} closed before MoQT SETUP completed"
                )
            )

    def _consume_events(self, events: list[_CoreEvent]) -> None:
        """native session event を client の状態へ反映する。"""
        for event in events:
            if event.kind == "established":
                self._established_event.set()
            elif event.kind == "close":
                self._fail_connect(
                    ConnectionError(f"peer closed MoQT session: {event.code} {event.reason}")
                )
            elif event.kind == "send_control":
                self._fail_connect(
                    NotImplementedError(
                        "sending control messages after SETUP is not implemented yet"
                    )
                )
            else:
                self._fail_connect(RuntimeError(f"unknown native session event: {event.kind}"))

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


__all__ = ["Client"]
