"""webtransport-py と moqt-rs を接続する実通信テスト。"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from moqt import Client, Server, ServerSession


@pytest.mark.asyncio
async def test_client_and_server_exchange_setup_over_webtransport(
    test_certificates: tuple[str, str],
) -> None:
    """localhost の実 WebTransport 接続上で MoQT SETUP が成立する。"""
    certfile, keyfile = test_certificates
    server_established = asyncio.Event()
    established_session: ServerSession | None = None
    server = Server(
        host="127.0.0.1",
        port=0,
        certfile=certfile,
        keyfile=keyfile,
    )

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

        assert client.established
        assert established_session is not None
        assert established_session.session_id >= 0
        assert established_session.address[0] == "127.0.0.1"
    finally:
        await client.close()
        await server.stop()
        server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server_task
