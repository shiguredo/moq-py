"""他プロジェクトのテストから moqt-py を使うための pytest fixture 群。

``conftest.py`` (pytest の rootdir に置くもの) で次のように宣言すると使える。

.. code-block:: python

    pytest_plugins = ["moqt.moq.testing"]

非同期 fixture を含むため ``pytest-asyncio`` が必要である。``asyncio_mode`` は
``auto`` でも ``strict`` でも動作する。

提供する fixture:

- ``moq_certificates``: localhost 用の自己署名証明書 ``(certfile, keyfile)``
- ``moq_server``: localhost で待ち受ける起動済みの ``Server``
- ``moq_client_factory``: ``moq_server`` へ接続済みの ``Client`` を作る factory
- ``moq_pair``: 接続済みの client / server と、確立した ``ServerSession``

証明書生成だけを単体で使いたい場合は :func:`generate_certificates` を、
述語の待ち合わせだけを単体で使いたい場合は :func:`wait_until` を使う。
"""

import asyncio
import contextlib
import datetime
import ipaddress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import pytest
import pytest_asyncio
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from moqt.moq.client import Client
from moqt.moq.server import Server, ServerSession

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

# fixture が待ち合わせる既定の秒数
DEFAULT_TIMEOUT = 5.0

# 述語の待ち合わせで再確認する間隔 (秒)
_POLL_INTERVAL = 0.01


class ClientFactory(Protocol):
    """`moq_server` へ接続済みの `Client` を作る。"""

    async def __call__(
        self,
        *,
        verify_peer: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
        url: str | None = None,
    ) -> Client:
        """接続を確立した `Client` を返す。

        Args:
            verify_peer: サーバー証明書を検証するか。
            timeout: 接続と MoQT SETUP の待ち合わせ秒数。
            url: 接続先。省略した場合は `moq_server` の待ち受け先を使う。
        """
        ...


@dataclass(frozen=True, slots=True)
class MoqPair:
    """接続済みの client / server の組。"""

    client: Client
    """接続済みの client。"""

    server: Server
    """client を受け入れている server。"""

    session: ServerSession
    """server 側で確立した MoQT session。"""


def generate_certificates(
    directory: Path,
    *,
    common_name: str = "localhost",
    days: int = 1,
) -> tuple[str, str]:
    """localhost 用の自己署名証明書を生成する。

    証明書と秘密鍵を `directory` へ書き出し、そのパスを返す。SAN には
    `common_name` と 127.0.0.1 を入れる。

    Args:
        directory: 証明書と秘密鍵の書き出し先。
        common_name: 証明書の CN と SAN の DNS 名。
        days: 証明書の有効期間 (日)。

    Returns:
        `(certfile, keyfile)` のパス。
    """
    certfile = directory / "cert.pem"
    keyfile = directory / "key.pem"
    private_key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName(common_name),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )

    keyfile.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    certfile.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return str(certfile), str(keyfile)


async def wait_until(predicate: Callable[[], bool], timeout: float = DEFAULT_TIMEOUT) -> None:
    """述語が真になるまで待つ。

    受信コールバックが記録した状態をテストから待ち合わせるときに使う。述語は
    短い間隔で繰り返し評価する。

    Args:
        predicate: 真になったら待ち合わせを終える述語。
        timeout: 待ち合わせの上限秒数。

    Raises:
        TimeoutError: `timeout` 以内に述語が真にならなかった。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(_POLL_INTERVAL)
    raise TimeoutError(f"condition was not satisfied within {timeout} seconds")


async def collect_objects[T](iterator: AsyncIterator[T], count: int, timeout: float) -> list[T]:
    """非同期イテレータから指定件数の要素を取り出す。

    Args:
        iterator: 要素を取り出す非同期イテレータ。
        count: 取り出す件数。
        timeout: 1 件ごとの待ち合わせの上限秒数。

    Returns:
        取り出した要素を順に並べたリスト。
    """
    collected: list[T] = []
    for _ in range(count):
        collected.append(await asyncio.wait_for(anext(iterator), timeout=timeout))
    return collected


@pytest.fixture(scope="session")
def moq_certificates(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    """localhost 用の自己署名証明書をテストセッションで 1 度だけ生成する。"""
    return generate_certificates(tmp_path_factory.mktemp("moq-certificates"))


@pytest_asyncio.fixture
async def moq_server(moq_certificates: tuple[str, str]) -> AsyncIterator[Server]:
    """localhost の空きポートで待ち受ける `Server` を起動する。

    peer からの SUBSCRIBE / FETCH を扱うには、この fixture を受け取ったテストが
    `on_subscribe` / `on_fetch` を登録してから client を接続する。
    """
    certfile, keyfile = moq_certificates
    server = Server(host="127.0.0.1", port=0, certfile=certfile, keyfile=keyfile)
    await server.start()
    run_task = asyncio.create_task(server.run())
    try:
        yield server
    finally:
        await server.stop()
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task


@pytest_asyncio.fixture
async def moq_client_factory(moq_server: Server) -> AsyncIterator[ClientFactory]:
    """`moq_server` へ接続済みの `Client` を作る factory を返す。

    factory が作った client は fixture の終了時にすべて閉じる。
    """
    clients: list[Client] = []

    async def connect(
        *,
        verify_peer: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
        url: str | None = None,
    ) -> Client:
        target = url or f"https://127.0.0.1:{moq_server.actual_port}/webtransport"
        client = Client(url=target, verify_peer=verify_peer)
        await client.connect(timeout=timeout)
        clients.append(client)
        return client

    try:
        yield connect
    finally:
        for client in clients:
            with contextlib.suppress(Exception):
                await client.close()


@pytest_asyncio.fixture
async def moq_pair(
    moq_server: Server,
    moq_client_factory: ClientFactory,
) -> AsyncIterator[MoqPair]:
    """WebTransport で接続し、MoQT SETUP まで終えた client / server の組を返す。"""
    established: list[ServerSession] = []
    established_event = asyncio.Event()

    async def on_session_established(session: ServerSession) -> None:
        established.append(session)
        established_event.set()

    moq_server.on_session_established(on_session_established)
    client = await moq_client_factory()
    await asyncio.wait_for(established_event.wait(), timeout=DEFAULT_TIMEOUT)
    yield MoqPair(client=client, server=moq_server, session=established[0])


__all__ = [
    "DEFAULT_TIMEOUT",
    "ClientFactory",
    "MoqPair",
    "collect_objects",
    "generate_certificates",
    "moq_certificates",
    "moq_client_factory",
    "moq_pair",
    "moq_server",
    "wait_until",
]
