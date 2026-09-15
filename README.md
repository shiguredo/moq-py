# moq-py

`moq-py` は `webtransport-py` 上で IETF Media Over QUIC (MoQ) を扱う Python の
client / server ライブラリです。MoQT の codec と Session 状態機械、LOC と MSF の
codec には [shiguredo/moqt-rs](https://github.com/shiguredo/moqt-rs) を PyO3 経由で
利用します。relay は含みません。

対応仕様は `moqt-rs` に追従します。

- Media over QUIC Transport: draft-ietf-moq-transport-21
- Low Overhead Media Container: draft-ietf-moq-loc-04
- MOQT Streaming Format: draft-ietf-moq-msf-01

いずれも draft 由来であり、将来の改訂で変更される可能性があります。

## 公開 API

- `moq.Client` / `moq.Server`: WebTransport over HTTP/3 上で MoQT セッションを扱う
- `moq.moqt`: MoQT の codec と sans I/O セッション状態機械
- `moq.loc`: LOC プロパティの codec
- `moq.msf`: MSF のカタログとタイムラインの codec
- `moq.testing`: 他プロジェクトのテストから使う pytest fixture 群

## オブジェクトの配送

`Publication.send_object` は subgroup ストリームで、`Publication.send_datagram` は
データグラムでオブジェクトを送ります。`send_object` には `subgroup_id` /
`publisher_priority` / `end_of_group` / `status` を渡せます。

`status` には `moq.moqt.OBJECT_STATUS_END_OF_GROUP` や
`moq.moqt.OBJECT_STATUS_END_OF_TRACK` を指定できます。このとき `payload` は空で
なければなりません (draft-ietf-moq-transport-21 §11.1.2 (Object Status))。受信側では
`MoqtObject.status` から読めます。

データグラムは経路 MTU を超えると通知なく破棄され、送信側からは検知できません
(同 §11.2.1 (Object Datagram))。`moq.moqt.MAX_DATAGRAM_SIZE` を超えるデータグラムを
送ると警告を記録します。大きいオブジェクトは subgroup ストリームで送ってください。

## 開発

`moqt-rs` は公開リポジトリの `develop` ブランチを追従します。実際にビルドした
コミットは `Cargo.lock` が固定します。最新へ更新するときは次を実行します。

```console
cargo update -p shiguredo_moqt
```

リリースタグが作成されたら `branch` から `tag` 指定へ切り替えます。

`webtransport-py` は PyPI から取得します。

```console
uv sync
uv run maturin develop --generate-stubs
uv run pytest
```

## 最小例

```python
import asyncio

from moq import Client


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

## テストライブラリとして使う

他の実装をテストするときは、`moq.testing` の fixture で client と server の組を
用意できます。pytest の rootdir に置いた `conftest.py` で宣言します。

```python
pytest_plugins = ["moq.testing"]
```

```python
from moq.msf import Catalog
from moq.testing import MoqPair, collect_objects


async def test_catalog_is_delivered(moq_pair: MoqPair) -> None:
    """server が配信したカタログを client が読めることを確認する。"""
    ...
```

`moq.testing` は `pytest` / `pytest-asyncio` / `cryptography` を使います。

```console
uv add "moq-py[testing]"
```
