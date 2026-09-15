# moqt-py

[![PyPI](https://img.shields.io/pypi/v/moqt-py)](https://pypi.org/project/moqt-py/)
[![image](https://img.shields.io/pypi/pyversions/moqt-py.svg)](https://pypi.python.org/pypi/moqt-py)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Actions status](https://github.com/shiguredo/moqt-py/workflows/CI/badge.svg)](https://github.com/shiguredo/moqt-py/actions)

## About Shiguredo's open source software

We will not respond to PRs or issues that have not been discussed on Discord. Also, Discord is only available in Japanese.

Please read <https://github.com/shiguredo/oss/blob/master/README.en.md> before use.

## 時雨堂のオープンソースソフトウェアについて

利用前に <https://github.com/shiguredo/oss> をお読みください。

## moqt-py について

moqt-py は IETF Media over QUIC (MoQ) を扱う Python ライブラリです。中継 (relay) は含みません。API は 2 層に分かれています。

- `moqt.moq`: WebTransport 上で MoQT セッションを扱う高レベル API (client / server)
- `moqt.moqt` / `moqt.loc` / `moqt.msf`: MoQT の codec と sans I/O セッション状態機械、LOC と MSF の codec を直接扱う低レベル API

実装には次のライブラリを利用しています。

- MoQT の codec と Session 状態機械、LOC と MSF の codec に [moqt-rs](https://github.com/shiguredo/moqt-rs) を PyO3 経由で利用しています
- WebTransport over HTTP/3 の I/O に [webtransport-py](https://pypi.org/project/webtransport-py/) を利用しています

## 対応仕様

- Media over QUIC Transport: draft-ietf-moq-transport-21
- Low Overhead Media Container: draft-ietf-moq-loc-04
- MOQT Streaming Format: draft-ietf-moq-msf-01

いずれも draft 由来であり、将来の改訂で変更される可能性があります。対応仕様は moqt-rs に追従します。

## 対応プラットフォーム

wheel は配布しておらず、Rust 1.93 以降の toolchain を使ってソースからビルドします。開発と CI で確認している環境は次のとおりです。

- macOS 26 arm64
- Ubuntu 24.04 x86_64 (GitHub Actions の `ubuntu-latest`)

## 対応 Python

- 3.14
- 3.14t (Free-Threading)

## インストール

```bash
uv add moqt-py
```

## 使い方（高レベル API）

`moqt.moq` が提供する高レベル API です。WebTransport の接続と MoQT セッションをまとめて扱います。

- `moqt.moq.Client` で MoQT セッションを張り、`moqt.moq.Server` で受けます
- `moqt.moq.testing` で他プロジェクトのテストから client と server の組を用意できます
- 低レベル API は [moqt.moqt](#moqtmoqt) / [moqt.loc](#moqtloc) / [moqt.msf](#moqtmsf) を参照してください

### client

```python
import asyncio

from moqt.moq import Client


async def main() -> None:
    # verify_peer=False は自己署名証明書を使う開発時の設定
    client = Client(url="https://127.0.0.1:4433/webtransport", verify_peer=False)
    await client.connect()
    print(client.established)

    # Track を購読し、届いたオブジェクトを順に処理する
    subscription = await client.subscribe([b"moqt-py", b"test"], b"video")
    async for obj in subscription.objects():
        print(obj.group_id, obj.object_id, len(obj.payload), obj.status)

    await client.close()


asyncio.run(main())
```

### server

```python
import asyncio

from moqt.moq import Server
from moqt.moq.server import SubscriptionRequest


async def main() -> None:
    server = Server(
        host="127.0.0.1",
        port=4433,
        certfile="cert.pem",
        keyfile="key.pem",
    )

    async def on_subscribe(request: SubscriptionRequest) -> None:
        # SUBSCRIBE_OK を返して配信を開始する
        publication = await request.subscribe_ok(1)
        await publication.send_object(0, 0, b"hello")
        await publication.close()

    server.on_subscribe(on_subscribe)
    await server.start()
    await server.run()


asyncio.run(main())
```

### オブジェクトの送信

`Publication.send_object` は subgroup ストリームで、`send_datagram` はデータグラムで送ります。

```python
from moqt import moqt

# subgroup ストリームで送る。subgroup_id / publisher_priority / end_of_group も指定できる
await publication.send_object(1, 0, b"payload")

# End of Group を通知する。このとき payload は空でなければならない
await publication.send_object(1, 1, b"", status=moqt.OBJECT_STATUS_END_OF_GROUP)

# データグラムで送る
await publication.send_datagram(1, 0, b"datagram payload")
```

> [!WARNING]
>
> - データグラムは経路 MTU を超えると通知なく破棄され、送信側からは検知できません (draft-ietf-moq-transport-21 §11.2.1)。`moqt.moqt.MAX_DATAGRAM_SIZE` を超えるデータグラムを送ると警告を記録します。大きいオブジェクトは subgroup ストリームで送ってください
> - 中継 (relay) は含みません
> - FETCH の fill (FILL_PARAMETERS による過去のオブジェクトの補充) は未対応です

### moqt.moq.testing

pytest の rootdir に置いた `conftest.py` で宣言すると、client と server の組を用意する fixture が使えます。

```python
pytest_plugins = ["moqt.moq.testing"]
```

```python
from moqt.moq.server import Publication, SubscriptionRequest
from moqt.moq.testing import MoqPair, collect_objects, wait_until


async def test_objects_are_delivered(moq_pair: MoqPair) -> None:
    """server が送ったオブジェクトを client が受け取れることを確認する。"""
    published: list[Publication] = []

    async def on_subscribe(request: SubscriptionRequest) -> None:
        published.append(await request.subscribe_ok(1))

    moq_pair.server.on_subscribe(on_subscribe)

    subscription = await moq_pair.client.subscribe([b"ns"], b"video")
    await wait_until(lambda: bool(published))
    await published[0].send_object(1, 0, b"hello")

    received = await collect_objects(subscription.objects(), 1, 5.0)
    assert received[0].payload == b"hello"
```

`moqt.moq.testing` は `pytest` / `pytest-asyncio` / `cryptography` を使います。

```bash
uv add "moqt-py[testing]"
```

## 使い方（低レベル API）

### moqt.moqt

MoQT の codec と sans I/O セッション状態機械です。ストリームの実体には触れず、呼び出し側がバイト列をやり取りします。

```python
from moqt.moqt import Session, decode_message

# 自側の制御ストリームの先頭バイト列を作る
client = Session.client("my-implementation")
data = client.start()
# 先頭 2 バイトは制御ストリームの stream type (0x2F00)
print(data[:2])

# 制御メッセージを 1 件デコードする
message, consumed = decode_message(data[2:])
print(message.kind, hex(message.type_id), message.body)
```

### moqt.loc

LOC (Low Overhead Media Container) のプロパティ codec です。

```python
from moqt import loc

properties = loc.Properties()
properties.add(loc.TIMESTAMP, 1_234_567)
properties.add(loc.TIMESCALE, 90000)
properties.add(loc.VIDEO_FRAME_MARKING, b"\x80")

encoded = properties.encode()
decoded, consumed = loc.Properties.decode(encoded)
print(decoded.timestamp, decoded.timescale, decoded.video_frame_marking)
```

### moqt.msf

MSF (MOQT Streaming Format) のカタログとタイムラインの codec です。カタログは draft の MUST に照らして検証されます。

```python
from moqt import msf

catalog = msf.Catalog.parse(
    '{"version":"draft-01","tracks":[{"name":"video","packaging":"loc","isLive":true}]}'
)
print(catalog.tracks)

# delta 更新を適用する
catalog.apply_delta('{"deltaUpdate":[{"op":"remove","tracks":[{"name":"video"}]}]}')
print(catalog.encode())

# タイムラインは gzip 圧縮にも対応する
timeline = msf.MediaTimeline()
timeline.add(1000, 1, 2, 0)
print(msf.MediaTimeline.decode(timeline.encode(gzip=True)).entries)
```

## 開発

開発ビルド、テスト、Git フック、moqt-rs への追従は [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) を参照してください。

## moqt-py ライセンス

Apache License 2.0

```text
Copyright 2026, Shiguredo Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```
