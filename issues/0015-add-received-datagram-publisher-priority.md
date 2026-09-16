# 受信した datagram の Publisher Priority を公開する

- Created: 2026-09-16
- Completed: 2026-09-16
- Branch: feature/add-received-datagram-publisher-priority
- Polished:

## 目的

明示的な Publisher Priority を持つ datagram の値をアプリから参照できるようにする。

DEFAULT_PRIORITY bit が立っていない datagram は Publisher Priority を明示しており、
スケジューリングの判断材料になる。これが読めないとアプリは受信した datagram の優先度を
知ることができない。

## 現状

datagram の受信経路で組み立てる `CoreEvent::object` が `publisher_priority: None` を固定で
入れており、moqt-rs の `ObjectDatagram::publisher_priority` の値を捨てている。

`moq.moq.client.MoqtObject` の doc は「データグラムでは常に `None` になる」としており、
この制約が仕様として書かれた状態になっている。

## 設計方針

DEFAULT_PRIORITY bit が立っていない datagram の明示値をイベントへ載せる。bit が立っている
datagram は従来どおり `None` とし、購読を確立した制御メッセージの値が使われることを
doc に明記する。

## 完了条件

- 明示 priority 付き datagram を受信すると `MoqtObject.publisher_priority` に値が入ること
- DEFAULT bit の datagram では `None` になること
- e2e テストで確認できること

## 解決方法

`src/core.rs` の `receive_datagram` が `CoreEvent::object` へ `publisher_priority: None` を
固定で入れていたのをやめ、`ObjectDatagram::publisher_priority` の値をそのまま載せた。
DEFAULT_PRIORITY bit が立っているデータグラムは moqt-rs 側で `None` になるため、
従来どおり購読を確立した制御メッセージの優先度を継承する。

doc は実態に合わせて次を更新した。

- `moq.moq.client.MoqtObject.publisher_priority`: データグラムでも明示値を持つ場合は
  値が入り、DEFAULT_PRIORITY bit が立っている場合だけ `None` になることを明記した
- `moq.moq.publisher.Publication.send_datagram`: `publisher_priority` を省略すると
  DEFAULT_PRIORITY bit が立ち、受信側では `None` になることを明記した
- `moq.moq._runtime` の `NativeEvent.publisher_priority`: データグラムも対象であることを
  明記した
- `MoqtObject.subgroup_id` の「データグラムでは常に `None`」は変更していない

テストは `tests/test_moqt.py` の `test_received_datagram_reports_the_publisher_priority`
(受信経路へ手組みのデータグラムを流し、明示値と DEFAULT_PRIORITY bit の双方を確認する) と、
`tests/test_e2e.py` の `test_datagram_publisher_priority_is_delivered` で確認する。
