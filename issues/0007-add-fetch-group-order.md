# FETCH の GROUP_ORDER を反映する

- Created: 2026-09-16
- Completed: 2026-09-17
- Branch: feature/add-fetch-group-order
- Polished:

## 目的

Group Order が Descending の FETCH を正しく扱えるようにする。

FETCH の Group Order は Group ID の差分表現の解決方向と、届いた Group の順序検証に使われる。
Ascending 固定のまま Descending の応答を受け取ると、Group ID が誤って解決されるか
`ProtocolViolation` でセッションが閉じる。

## 現状

`CoreSession` は fetch ストリームの受信デコーダを
`FetchStreamDecoder::new_with_group_order(DEFAULT_PUBLISHER_GROUP_ORDER_ASCENDING)` で生成しており、
`PARAM_GROUP_ORDER` を反映する経路が無い。`moqt.moqt.PARAM_GROUP_ORDER` は定数として公開されて
いるだけである。

送信側の fetch オブジェクトのエンコードは `moq._runtime._encode_fetch_object` による独自実装で、
こちらも Group ID の差分を Ascending 前提で書いている。

## 設計方針

FETCH の `GROUP_ORDER` パラメータを購読と同じ規則で解決し、fetch ストリームのデコーダと
エンコーダの両方へ渡す。ストリームごとに解決結果を保持し、同じ fetch stream 内で一貫させる。

## 完了条件

- `GROUP_ORDER=Descending` の FETCH で Group ID が正しく解決されること
- 送信側も Descending の差分表現で書くこと
- テストで確認できること

## 解決方法

受信側は `src/core.rs` のデータストリーム処理を、stream type の通知とデコーダの作成に
分けた。fetch ストリームのデコーダは、Request ID を運ぶ FETCH_HEADER をデコードできる
まで作らない。Group Order は Request ID から引き (`Session::fetch` の `group_order`、
fill fetch stream は `Session::subscription` の `group_order`)、
`FetchStreamDecoder::new_with_group_order` へ渡す。省略された要求は既定値の
Ascending (0x1) になる。ヘッダが複数の断片に分かれる場合は、ヘッダが揃うまで
断片をバッファへ保持してデコードを保留する。

送信側は `moq._runtime` の `FetchWriter` が Group Order を保持し、
`_encode_fetch_object` が Group ID の差分を向きに応じて書くようにした。Ascending では
`今回 - 前回 - 1`、Descending では `前回 - 今回 - 1` である。要求と同じ向きで進まない
Group は差分で表現できないため `MoqtError` にする。Group Order は
`Runtime.open_fetch_stream` / `open_fill_fetch_stream` が状態機械から解決するため、
アプリが指定する必要はない。

`Session.fetch()` が返す辞書に `group_order` を追加した。

テストは `tests/test_e2e.py` の `test_fetch_responds_in_a_descending_group_order` と、
`tests/test_moqt.py` の `test_received_fetch_stream_resolves_descending_group_ids` /
`test_received_fetch_stream_waits_for_the_fetch_header` で確認する。
