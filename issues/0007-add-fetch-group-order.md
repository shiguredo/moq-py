# FETCH の GROUP_ORDER を反映する

- Created: 2026-09-16
- Completed:
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
