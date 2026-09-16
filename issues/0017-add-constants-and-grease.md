# 未公開の定数と grease API を公開する

- Created: 2026-09-16
- Completed: 2026-09-16
- Branch: feature/add-constants-and-grease
- Polished:

## 目的

moqt-rs が公開している残りの定数と grease のヘルパーを Python から使えるようにする。

grease は未知の値を無視する実装を検証するための仕組みであり、相互運用テストで
意図的に未知のパラメータやメッセージを混ぜるために使う。

## 現状

次が `moqt.moqt` から参照できない。

- `MAX_NEW_SESSION_URI_LENGTH`
- `DEFAULT_PUBLISHER_GROUP_ORDER_ASCENDING`
- `DEFAULT_PEER_ALIAS_RETENTION_MS`
- `PUBLISH_DONE_STREAM_COUNT_UNKNOWN`
- `MAX_OUT_OF_ORDER_REQUEST_IDS`
- `grease` の `GREASE_BASE` / `GREASE_INTERVAL` / `GREASE_MAX` と `generate` / `is_grease`

`PARAM_*` / `SETUP_OPTION_*` / エラーコード / `PROP_*` / `MSF_*` は公開済みである。

## 設計方針

`moqt.moqt` の定数と関数として追加し、`__all__` に含める。`generate` は乱数源を
引数で受け取れるようにし、テストで決定的に検証できるようにする。

## 完了条件

- 上記の定数が `moqt.moqt` から参照できること
- `generate` が grease 値を作り、`is_grease` がそれを判定できること
- テストで確認できること

## 解決方法

`src/grease.rs` を追加し、`moqt-rs` の `grease` モジュールを包む `generate` /
`is_grease` と定数 `GREASE_BASE` / `GREASE_INTERVAL` / `GREASE_MAX` を公開した。

- `generate(source=None)`: `source` は `stop` を 1 つ受け取り `[0, stop)` の整数を
  返す呼び出し可能オブジェクトである。省略時は `random.randrange` を使う。
  乱数源を引数で受け取るため、テストから決定的に検証できる
- 乱数源へ渡す上限は `(GREASE_MAX - GREASE_BASE) // GREASE_INTERVAL + 1` とする。
  `GREASE_MAX / GREASE_INTERVAL` では上限を超える値を作る連番を渡してしまうため、
  `GREASE_MAX` ちょうどが生成できる最大の連番を求める
- 乱数源が整数以外を返した場合と、上限を超える連番を返した場合は `ValueError` にする

その他の定数は `lib.rs` の `init` で `moqt-rs` から登録した。

- `DEFAULT_PUBLISHER_GROUP_ORDER_ASCENDING` / `DEFAULT_PEER_ALIAS_RETENTION_MS`
- `MAX_NEW_SESSION_URI_LENGTH` / `MAX_OUT_OF_ORDER_REQUEST_IDS` (Rust 側は `usize` の
  ため Python の int へは `u64` として渡す)
- `PUBLISH_DONE_STREAM_COUNT_UNKNOWN`

`python/moqt/moqt.py` では `generate` / `is_grease` に draft の節番号と乱数源の
契約を書いた docstring を付け、`__all__` へ追加した。

テストは `tests/prop_moqt.py` の `prop_grease_round_trip` などと、
`tests/test_moqt.py` の `test_grease_constants_are_drafted_values` などで確認する。
