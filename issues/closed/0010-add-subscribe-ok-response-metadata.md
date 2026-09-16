# SUBSCRIBE_OK と REQUEST_OK の応答メタデータを公開する

- Created: 2026-09-16
- Completed: 2026-09-16
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

## 解決方法

`Subscription` と `Publication` に `parameters` / `track_properties` を追加し、
待ち合わせが返すイベントの値を保持するようにした。表現は
`moqt.moqt.Message.parameters` / `Message.track_properties` と同じであり、
parameters は型番号をキーにしたエンコード済みバイト列の辞書、track_properties は
偶数型が `int`、奇数型が `bytes` の辞書である。

状態機械の `RequestOkReceived` イベントは応答の Track Properties を運ばないため、
`src/core.rs` の `CoreEvent` に `track_properties` を追加し、受信した生バイト列から
`decode_track_properties_data` で取り出すようにした。応答メッセージを処理する
`receive_request_stream` は、生バイト列をイベント変換へ渡す
`drain_events_with_data` を使う。Track Properties を運ばない応答では空の辞書に
なり、応答以外のメッセージでは `None` になる。

保持する値は次のとおりである。

- `Client.subscribe` が返す `Subscription` は SUBSCRIBE_OK の値を保持する
- `Client.publish` が返す `Publication` は REQUEST_OK の値を保持する
- `Server` の `SubscriptionRequest.subscribe_ok` と `PublisherRequest.accept` が
  返す `Publication` は、自側が送った応答の値を保持する

REQUEST_OK が Track Properties を運べるのは TRACK_STATUS への応答だけであり、
PUBLISH への応答では空でなければならない (draft-ietf-moq-transport-21 §9.3
(REQUEST_OK))。moqt-rs も空でない Track Properties を拒否するため、server 側の
`Publication.track_properties` は PUBLISH の応答では空になる。

テストは `tests/test_e2e.py` の `test_subscribe_ok_metadata_is_exposed` /
`test_request_ok_metadata_is_exposed` と、`tests/test_moqt.py` の
`test_request_ok_event_reports_the_response_metadata` /
`test_request_ok_event_without_track_properties_reports_an_empty_dict` で確認する。
