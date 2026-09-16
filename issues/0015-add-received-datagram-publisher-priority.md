# 受信した datagram の Publisher Priority を公開する

- Created: 2026-09-16
- Completed:
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
