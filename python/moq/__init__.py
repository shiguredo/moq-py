"""Media over QUIC (MoQ) を WebTransport 上で扱う client / server ライブラリ。

`webtransport-py` が WebTransport over HTTP/3 の I/O を、`moqt-rs` が MoQT
(draft-ietf-moq-transport-21) の codec と sans I/O セッション状態機械を、
LOC (draft-ietf-moq-loc-04) と MSF (draft-ietf-moq-msf-01) の codec を提供する。
このパッケージがそれらを接続する。

relay は含まない。

公開 API:

- `Client` / `Server`: WebTransport 接続上で MoQT セッションを扱う
- `moq.moqt`: MoQT の codec と sans I/O セッション状態機械
- `moq.loc`: LOC プロパティの codec
- `moq.msf`: MSF のカタログとタイムラインの codec
- `moq.testing`: 他プロジェクトのテストから使う pytest fixture 群

MoQ は draft 由来であり、将来の改訂で変更される可能性がある。
"""

from moq import loc, moqt, msf
from moq.client import Client, Fetch, MoqtObject, Subscription, TrackStatus
from moq.server import (
    FetchRequest,
    FetchResponse,
    Publication,
    Server,
    ServerSession,
    SubscriptionRequest,
)

__all__ = [
    "Client",
    "Fetch",
    "FetchRequest",
    "FetchResponse",
    "MoqtObject",
    "Publication",
    "Server",
    "ServerSession",
    "Subscription",
    "SubscriptionRequest",
    "TrackStatus",
    "loc",
    "moqt",
    "msf",
]
