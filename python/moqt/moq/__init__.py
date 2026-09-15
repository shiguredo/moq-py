"""WebTransport 接続上で MoQT セッションを扱う高レベル API。

`webtransport-py` の asyncio API が WebTransport over HTTP/3 の I/O を担当し、
`moqt._native` の MoQT 状態機械と接続する。下位層である `moqt.moqt` の codec や
sans I/O 状態機械を直接扱う必要はない。

公開 API:

- `Client`: WebTransport 接続を張って MoQT セッションを開始する
- `Server`: WebTransport 接続を受け入れて MoQT セッションを開始する
- `Subscription` / `Fetch` / `TrackStatus`: client 側の要求
- `SubscriptionRequest` / `FetchRequest` / `Publication`: server 側の応答
- `moqt.moq.testing`: 他プロジェクトのテストから使う pytest fixture 群

`moqt.moq.testing` は `pytest` と `cryptography` を必要とするため、このモジュール
からは再輸出しない。`from moqt.moq import testing` のように明示して取り出す。

relay は含まない。MoQ は draft 由来であり、将来の改訂で変更される可能性がある。
"""

from moqt.moq.client import (
    Client,
    Fetch,
    MoqtObject,
    Subscription,
    TrackStatus,
)
from moqt.moq.server import (
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
]
