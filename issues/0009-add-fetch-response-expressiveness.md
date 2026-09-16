# FETCH 応答で End of Range と properties と datagram 起源を扱えるようにする

- Created: 2026-09-16
- Completed: 2026-09-17
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

## 解決方法

`moq.moq.server.FetchResponse` に End of Range 3 種を送る API を追加した。
`send_end_of_non_existent_range` / `send_end_of_unknown_range` /
`send_end_of_timed_out_range` が `moq._runtime.Runtime.send_fetch_end_of_range` を呼び、
`_encode_fetch_end_of_range` が Serialization Flags の特殊値 (0x8C / 0x10C / 0x20C) と
Group ID / Object ID の絶対値を書く。End of Range の後は Group ID と Object ID の基準が
End of Range の値になるため、`FetchWriter` が保持する基準も更新する
(draft-ietf-moq-transport-21 §11.4.1.2 (End of Range))。

`FetchResponse.send_object` に `properties_data` と `datagram_origin` を追加し、
`_encode_fetch_object` を拡張した。`properties_data` は `_properties_blob` で
`Properties Length | Key-Value-Pairs` に正規化してから bit 5 を立てて Publisher Priority
の後ろに書く。`datagram_origin` を真にすると bit 6 を立てて Subgroup ID フィールドを
書かない (下位 2 bit は 0 になる)
(draft-ietf-moq-transport-21 §11.4.1.1 (Flags))。Group ID の差分は、先行して対応した
`FetchWriter.group_order` の向きをそのまま使う。

テストは `tests/test_e2e.py` の `test_fetch_response_reports_end_of_range` と
`test_fetch_response_carries_properties_and_datagram_origin` で確認する。後者は
End of Range の後に送ったオブジェクトの基準が更新されること、datagram 起源の
オブジェクトが Subgroup 単位の Priority 一貫性検査の対象外になること (bit 6 が
立っていなければ受信側が拒否する) も併せて確認する。

## 残っている制約

`MoqtObject.properties` で受信した fetch オブジェクトの Properties を観測することは
できない。moqt-rs 7c2c3aa の `FetchStreamDecoder` は entry の Properties を
Malformed Track の検証にだけ使い、`DecodedFetchObject` に含めないためである
(subgroup の `DecodedSubgroupObject::properties_bytes` に相当する値が無い)。
この制約は moqt-rs 側の API 追加で解消する必要があり、moqt-py からは回避できない。
受信側のデコーダは Properties を読み取って宣言長の不一致や Malformed Track を検出する
ため、送信した Properties が wire 上で正しいことは e2e テストで確認できる。
