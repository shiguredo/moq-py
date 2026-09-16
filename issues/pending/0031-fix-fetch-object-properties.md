# FETCH 応答オブジェクトの Properties が受信側で観測できない

- Created: 2026-09-16
- Completed:
- Branch: feature/fix-fetch-object-properties
- Polished:

## 目的

FETCH で受け取ったオブジェクトの Properties をアプリから参照できるようにする。

subgroup で受け取るオブジェクトは `MoqtObject.properties` から Properties を参照できる。
FETCH でも同じことができるはずで、参照できないと FETCH 経由でしか取得しない場合に
アプリが Properties を扱えない。

## 現状

`moq.moq.server.FetchResponse.send_object` は Properties を指定して送信できる。

しかし受信側では参照できない。moqt-rs の `FetchStreamDecoder` は受信した entry の
Properties を Malformed Track 検証にだけ使い、`DecodedFetchObject` に含めないため、
moqt-py が受け取るイベントに Properties の生バイト列が存在しない。

`tests/test_e2e.py` の `test_fetch_response_carries_properties_and_datagram_origin` は
送信側が指定した Properties が peer のデコーダで検証されることまでを確認しており、
受信側での参照は確認できていない。

## 設計方針

moqt-rs の `DecodedFetchObject` が Properties の生バイト列を返すようになったら、
moqt-py の FETCH 受信経路でそれをイベントへ載せ、`MoqtObject.properties` から参照できる
ようにする。

## 完了条件

- FETCH で受け取ったオブジェクトの Properties を `MoqtObject.properties` から参照できること
- e2e テストで往復を確認できること

## pending にする理由

moqt-rs の `DecodedFetchObject` が Properties を保持しておらず、moqt-py だけでは
実装できない。moqt-rs 側の API 追加待ちである。
