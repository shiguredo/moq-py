# 送信 datagram の status を状態機械へ渡し MUST 検査を行う

- Created: 2026-09-16
- Completed:
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
