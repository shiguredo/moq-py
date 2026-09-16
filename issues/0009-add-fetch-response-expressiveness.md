# FETCH 応答で End of Range と properties と datagram 起源を扱えるようにする

- Created: 2026-09-16
- Completed:
- Branch: feature/add-fetch-response-expressiveness
- Polished:

## 目的

FETCH 応答で moqt-rs が表現できるものを Python からも出せるようにする。

FETCH は要求された範囲の一部が存在しない場合や不明な場合に End of Range を返す。
これが送れないと、publisher は範囲の欠落を subscriber へ伝えられない。

## 現状

受信側は `moq.moq.Fetch.ranges()` で `end_of_non_existent_range` / `end_of_unknown_range` /
`end_of_timed_out_range` を公開しており、送信側だけが非対称になっている。

`moq.moq.server.FetchResponse` は `send_object` と `close` しか持たず、End of Range を送る API が無い。

`moq._runtime._encode_fetch_object` は flags を固定で組み立てており、Properties と
Datagram 起源 bit に対応していない。subgroup id も常に explicit で書いている。
`FetchResponse.send_object` に properties を渡す口も無い。

## 設計方針

`FetchResponse` に End of Range 3 種を送る API を追加する。`send_object` に properties と
datagram 起源を指定する引数を追加し、`_encode_fetch_object` をそれに合わせて拡張する。

Group Order が Descending の場合の差分表現は FETCH の Group Order 対応と揃える。

## 完了条件

- End of Range 3 種を送信でき、peer の `Fetch.ranges()` で観測できること
- properties 付き fetch オブジェクトを送信でき、peer の `MoqtObject.properties` で観測できること
- datagram 起源のオブジェクトを送信できること
- e2e テストで確認できること
