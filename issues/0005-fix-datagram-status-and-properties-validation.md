# 送信 datagram の status を状態機械へ渡し MUST 検査を行う

- Created: 2026-09-16
- Completed: 2026-09-17
- Branch: feature/fix-datagram-status-and-properties-validation
- Polished:

## 目的

datagram でも Object Status に応じたフィルタ評価を効かせ、draft の MUST に反するバイト列を
送出しないようにする。

## 現状

`moq._runtime.Runtime.send_object_datagram` は `CoreSession.send_object_datagram` の `status` に
常に `None` を渡している。`properties_data` は渡しているため、状態機械から見たオブジェクトと
wire に書くオブジェクトが食い違う。

また datagram のエンコードは `_encode_object_datagram` による独自実装であり、moqt-rs の
`ObjectDatagram::encode` が行う次の検査が無い。

- Properties Length = 0 はプロトコル違反 (draft-ietf-moq-transport-21 §11.2.1)
- 非 Normal status に Properties を付けるのは不可 (draft-ietf-moq-transport-21 §11.1.3)
- STATUS と END_OF_GROUP の同時指定は不可

加えて `_properties_content` は、先頭の varint が残り長と一致しない `properties_data` を
「長さなしの内容」と解釈して長さを付け直すため、宣言長と実データ長が一致しない blob が
黙って書き換えられる。

## 設計方針

status を状態機械へ渡す。エンコード前に moqt-rs と同じ MUST 検査を行い、違反は `MoqtError` にする。
Properties ブロックの解釈は「Properties Length を含む生バイト列」に一本化し、宣言長と実データ長の
不一致を拒否する。

## 完了条件

- status 付き datagram が状態機械のフィルタ評価に反映されること
- Properties Length = 0 / 非 Normal status への Properties / STATUS と END_OF_GROUP の同時指定が
  送出前にエラーになること
- 宣言長と実データ長が一致しない Properties が拒否されること
- e2e テストで確認できること

## 解決方法

`moq._runtime.Runtime.send_object_datagram` が、状態機械へ渡す `status` に呼び出し側が
指定した値をそのまま渡すようにした。`properties_data` は `_properties_blob` で
`Properties Length | Key-Value-Pairs` の形へ正規化したバイト列を wire と状態機械の
両方へ渡し、状態機械から見たオブジェクトと wire に書くオブジェクトを一致させた。

`_encode_object_datagram` は Properties を正規化済みのバイト列として受け取り、
wire を組み立てる前に draft の MUST を検査するようにした。検査内容は Properties
Length = 0 (draft-ietf-moq-transport-21 §11.2.1 (Object Datagram))、非 Normal status への
Properties 付与 (§11.1.3 (Object Properties))、STATUS と END_OF_GROUP の同時指定
(§11.2.1 (Object Datagram)) であり、違反は `MoqtError` になる。

`_properties_content` は、宣言長が後続バイト数と一致しないブロックを「長さなしの内容」と
解釈するのをやめ、`MoqtError` で拒否するようにした。Properties の解釈は
`Properties Length` を含む生バイト列に一本化され、subgroup オブジェクトも同じ検査を
受ける。`Properties Length` が途中で切れたブロックも同じ経路で拒否する。

`moq.moq.Publication.send_object` / `send_datagram` の doc に、`properties_data` が
`Properties Length` を含む生バイト列であることと、データグラム固有の制約を追記した。

テストは `tests/test_e2e.py` の `test_datagram_properties_reject_an_empty_block` /
`test_properties_reject_a_length_mismatch` / `test_datagram_properties_reject_a_non_normal_status`
と、`tests/test_moqt.py` の `test_send_object_datagram_evaluates_the_object_status` で確認する。
状態機械は送信するデータグラムの status を見て非 Normal status への Properties 付与を
拒否するため、Python 側は実際の status を渡さなければこの検査を受けられない。
