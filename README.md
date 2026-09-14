# moqt-py

`moqt-py` は `webtransport-py` 上で IETF MoQT (Media over QUIC Transport) を扱う
Python ライブラリです。MoQT の codec と Session 状態機械には
[shiguredo/moqt-rs](https://github.com/shiguredo/moqt-rs) を PyO3 経由で利用します。

対応仕様は `moqt-rs` に追従し、Media over QUIC Transport は draft-21 です。

現時点の最小実装は WebTransport over HTTP/3 上で client/server の SETUP を交換し、
MoQT Session を確立するところまで対応しています。PUBLISH、SUBSCRIBE、FETCH と
data stream / datagram は未実装です。

## 開発

`moqt-rs` は公開リポジトリの開発ブランチを、`Cargo.toml` で特定コミットに固定して
参照します。リリースタグが作成されたら `rev` から `tag` 指定へ切り替えます。

`webtransport-py` はローカルの checkout を参照します。開発時は
`shiguredo/moqt-py` と `shiguredo/webtransport-py` を同じディレクトリへ並べて
配置してください。

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
