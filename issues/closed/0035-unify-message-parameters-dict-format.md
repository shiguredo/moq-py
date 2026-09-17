# Message Parameters の辞書が 2 系統あり送信経路で値が壊れる

- Created: 2026-09-18
- Completed: 2026-09-18

## 目的

`Event.parameters` / `Message.parameters` / `MessageParameters.to_dict()` が返す辞書を、
そのまま送信経路 (`Session.send_*` と `Client` / `Server` の `parameters` 引数) へ
渡しても壊れないようにする。

## 現状

パラメータ辞書の値の形式が 2 系統ある。

- 受信側: 型番号 → パラメータ 1 件分のエンコード済みバイト列。長さ付きバイト列の型は
  長さプレフィックスを含む。LOCATION_FILTER なら `02 0900` である
- 送信側 (`src/core.rs` の `parameter_value_from_python`): LOCATION_FILTER と 5 種の
  Range Filter には長さプレフィックスを含まない本体 (`0900`) を期待する。uint8 / vi64 は
  `int`、LARGEST_OBJECT は `(group_id, object_id)`、TRACK_NAMESPACE_PREFIX は `bytes` の
  リストといった型付きの値も期待する

このため受信した辞書を送り直すという自然な操作で 19 型のうち 14 型が壊れる。実測すると
次のとおりである。

```python
# LOCATION_FILTER: 無言で別のフィルタになる
parameters = MessageParameters(
    {moqt.PARAM_LOCATION_FILTER: LocationFilter("absolute_start", start_group=9, start_object=0)}
)
parameters.to_dict()  # {0x21: b"\x02\x09\x00"}
# これを send_subscribe へ渡すと wire では長さが二重になり、受信側は
# absolute_start{9,0} ではなく absolute_range{start={2,9}, end_group_delta=0} と解釈する
# Range Filter 5 種 (SUBGROUP / OBJECTID / PRIORITY / OBJECT_PROPERTY / TRACK_PROPERTY) も同じ

# uint8 / vi64 など: TypeError になる
MessageParameters({moqt.PARAM_FORWARD: 1}).to_dict()  # {0x10: b"\x01"}
# これを send_subscribe へ渡すと TypeError: 'bytes' object cannot be interpreted as an integer
# GROUP_ORDER / INCLUDE_PROPERTIES / EXPIRES / LARGEST_OBJECT / AUTHORIZATION_TOKEN /
# FILL_PARAMETERS / TRACK_NAMESPACE_PREFIX も同様に TypeError になる

# 型付きの LocationFilter は送信経路だけ受け付けない
# send_subscribe([b"ns"], b"t", {moqt.PARAM_LOCATION_FILTER: LocationFilter("next_object")})
# TypeError: 'LocationFilter' object is not an instance of 'Sequence'
```

## 設計方針

辞書の値の形式を「パラメータ 1 件分のエンコード済みバイト列」に統一する。これは
`Event.parameters` / `Message.parameters` / `MessageParameters.to_dict()` が返す形式と
同じであり、受信した辞書をそのまま送信経路へ渡せるようになる。

- `bytes` は常にエンコード済みの値として解釈し、長さ付きバイト列の型は長さプレフィックスと
  宣言長を検証する。本体だけのバイト列は `ValueError` にして、無言で別の値にしない
- 型付きの値 (`int` / `(group_id, object_id)` / `bytes` のリスト / 入れ子の辞書 /
  AUTHORIZATION_TOKEN の辞書 / `LocationFilter`) は従来どおり受け付ける
- 辞書から `MessageParameters` を構築する経路 (`src/message_parameters.rs` の
  `message_parameters_from_dict`) と送信経路 (`src/core.rs` の
  `parameter_value_from_python`) を 1 つの実装へ統合する
- `LocationFilter.encode` / `decode` と `MessageParameters.location_filter` /
  `range_filters` はフィルタ本体を扱う低レベルの口として残す。`LocationFilter.encode` の
  doc は「`MessageParameters` の辞書の値として渡せる」と書いてあるが、実際は長さ
  プレフィックスが無いため渡せない。doc を実態に合わせる

## 実装対象

- `src/core.rs`: `parameter_value_from_python` を統合した変換 (`parameter_from_python`) へ
  置き換える。`is_length_prefixed` / `validate_encoded_value` を移設する
- `src/message_parameters.rs`: `parameter_from_dict` / `message_parameters_from_dict` を
  統合版へ委譲する。`LocationFilter.encode` の doc を修正する
- `python/moqt/moqt.py`: モジュール docstring のパラメータの説明を更新する
- `python/moqt/moq/client.py` など: パラメータの形式に触れている doc を更新する
- `tests/test_moqt.py`: 送信経路へ渡す値を統一形式へ変更し、往復と拒否のテストを追加する
- `python/moqt/_native.pyi`: 再生成する

## 完了条件

- `MessageParameters(...).to_dict()` をそのまま `Session.send_*` へ渡すと、受信側で同じ
  パラメータとして解釈されること (LOCATION_FILTER と Range Filter を含む)
- `Event.parameters` / `Message.parameters` をそのまま送信経路へ渡しても同じであること
- 長さプレフィックスを含まないフィルタ本体を渡した場合は `ValueError` になること
- 型付きの値 (`int` / `(group_id, object_id)` / `bytes` のリスト / 入れ子の辞書 /
  AUTHORIZATION_TOKEN の辞書 / `LocationFilter`) を受け付けること
- `uv run pytest` が全件通ること
- `cargo fmt` / `cargo clippy` / `ruff` / `ty` が通ること

## 解決方法

パラメータ辞書の値の形式を「パラメータ 1 件分のエンコード済みバイト列」に統一した。

### 変換の統合 (`src/core.rs`)

- 辞書の値 1 件をパラメータへ変換する `parameter_from_python` を追加し、受信側が返す
  辞書と同じ形式を第一に受け付けるようにした
  - `bytes` は長さ付きバイト列の型では長さプレフィックスと宣言長を検証し、
    `decode_parameter_entry` でデコードする。本体だけのバイト列は `ValueError` にし、
    エラーメッセージで `LocationFilter` を渡すか `MessageParameters.to_dict()` を使うよう促す
  - `LOCATION_FILTER` は `LocationFilter` の型付き表現も受け付ける
  - それ以外の型は従来どおり `int` / `(group_id, object_id)` / `bytes` のリスト /
    入れ子の辞書 / AUTHORIZATION_TOKEN の辞書を受け付ける (`typed_parameter_value_from_python`)
- `message_parameters_from_python` を `parameter_from_python` に委譲し、
  `MessageParameters` の構築経路と送信経路を 1 つの実装に統合した
- `is_length_prefixed` / `validate_encoded_value` を `src/message_parameters.rs` から
  `src/core.rs` へ移した (両経路で共有するため)

### `MessageParameters` (`src/message_parameters.rs`)

- 重複していた `parameter_from_dict` / `message_parameters_from_dict` を削除し、
  `message_parameters_from_python` へ委譲した
- `LocationFilter.encode` の doc を実態 (フィルタ本体を返すため辞書の値にはそのまま
  使えない) に合わせた。型付きのフィルタはそのまま辞書の値として渡せる

### テスト

- `tests/test_moqt.py`: `MessageParameters.to_dict()` をそのまま `send_subscribe` へ
  渡すと受信側で同じパラメータとして解釈されること、受信した辞書をそのまま別の
  セッションの購読へ渡せること、フィルタ本体を渡すと `ValueError` になることを
  追加した。Range Filter を送るテストは長さプレフィックスを含む値へ変更した
- `tests/test_e2e.py`: `_range_filter` が長さプレフィックスを含むパラメータ値を
  返すようにした

`uv run pytest` は 331 件すべて通り、`cargo fmt` / `cargo clippy` / `ruff` / `ty` も
通ることを確認した。
