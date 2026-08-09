# moqt-py

`moqt-py` は `webtransport-py` 上で IETF MoQT (Media over QUIC Transport) を扱う
Python ライブラリです。MoQT の codec と Session 状態機械にはローカルの
`moqt-rs` を PyO3 経由で利用します。

現時点の最小実装は WebTransport over HTTP/3 上で client/server の SETUP を交換し、
MoQT Session を確立するところまで対応しています。PUBLISH、SUBSCRIBE、FETCH と
data stream / datagram は未実装です。

## 開発

`Cargo.toml` は次のローカル checkout を参照します。

- `../../shiguredo-rust/moqt-rs`
- `../../shiguredo-oss/webtransport-py`

```console
uv sync
uv run maturin develop --generate-stubs
uv run pytest
```

## 最小例

```python
import asyncio

from moqt import Client


async def main() -> None:
    client = Client(
        url="https://127.0.0.1:4433/webtransport",
        verify_peer=False,
    )
    await client.connect()
    print(client.established)
    await client.close()


asyncio.run(main())
```
