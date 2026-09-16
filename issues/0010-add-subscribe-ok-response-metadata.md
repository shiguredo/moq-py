# SUBSCRIBE_OK と REQUEST_OK の応答メタデータを公開する

- Created: 2026-09-16
- Completed:
- Branch: feature/add-subscribe-ok-response-metadata
- Polished:

## 目的

応答メッセージが運ぶ parameters と track_properties をアプリから参照できるようにする。

EXPIRES / LARGEST_OBJECT / GROUP_ORDER / DEFAULT_PUBLISHER_PRIORITY は publisher が購読条件を
確定するために返す値であり、subscriber がこれらを読めないと、いつ購読が切れるか、どこまで
オブジェクトがあるかをアプリが判断できない。

## 現状

`moq.moq.Client.subscribe` は `request_id, _event = await runtime.subscribe(...)` と待ち合わせが
返したイベントを捨て、Track Alias だけを状態機械から取り直している。`Subscription` に
parameters / track_properties を保持する場所が無い。

`moq.moq.Client.publish` も同じく `_event` を捨てており、`Publication` が REQUEST_OK の
parameters / track_properties を保持しない。

## 設計方針

待ち合わせが返すイベントの parameters と track_properties を `Subscription` / `Publication` の
フィールドとして保持する。`moq.moqt` の `Message.parameters` / `Message.track_properties` と
同じ表現（型番号をキーにした辞書）に揃える。

server 側の `Publication` についても、REQUEST_OK を受けた時点の値を保持する。

## 完了条件

- `Client.subscribe` が返す `Subscription` から parameters と track_properties を参照できること
- `Client.publish` が返す `Publication` から同様に参照できること
- e2e テストで LARGEST_OBJECT を含む応答が観測できること
