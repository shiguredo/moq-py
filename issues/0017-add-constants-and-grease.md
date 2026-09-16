# 未公開の定数と grease API を公開する

- Created: 2026-09-16
- Completed:
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
