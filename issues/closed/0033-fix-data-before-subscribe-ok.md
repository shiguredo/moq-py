# 購読が確定する前に届いたデータが失われる

- Created: 2026-09-16
- Completed: 2026-09-17
- Branch: feature/fix-data-before-subscribe-ok
- Polished: 2026-09-17

## 目的

購読が確定する前に届いたオブジェクトやデータグラムを、購読の確定後に取りこぼさず配信する。

publisher が SUBSCRIBE_OK を返した直後にオブジェクトを送るのは自然な実装である。一方で
SUBSCRIBE_OK は bidi ストリーム、subgroup は uni ストリーム、データグラムはデータグラムと
別々の経路で届くため、subscriber が SUBSCRIBE_OK を処理するより先にオブジェクトを
処理することがある。この並び順は異常ではなく、subscriber 側が耐えなければならない。

現行実装ではこの並び順でオブジェクトが失われる。あわせて、subgroup では受信経路自体が
例外で失敗する。どちらも `Session` は `established` のまま残る。セッションを落とす経路では
ないため、購読の確定後に配信できるようにすることを目的とする。

## 現状

購読が確定する前は、subgroup とデータグラムのどちらも配信されない。subgroup では加えて
受信経路が例外で失敗する。sans I/O で決定的に再現できる。購読は
`tests/test_moqt.py` の `_subscribe_round_trip` と同じ手順で組み立てる。

```python
client, server = _setup()
events = client.send_subscribe([b"ns"], b"t", {})
request_id = _request_id(events[0])
client.register_local_request_stream(4, request_id)
server.receive_request_stream(4, _message_data(events[0]), "peer")
subscribe_ok = server.send_subscribe_ok(request_id, 1, {}, {})

# SUBSCRIBE_OK を渡す前に投入したデータは購読に紐づかない。
# データグラムは unknown_track_alias として黙って破棄される
events = client.receive_datagram(datagram)
assert [event.kind for event in events] == ["unknown_track_alias"]

# SUBSCRIBE_OK を処理した後は受理される
client.receive_request_stream(4, _message_data(subscribe_ok[0]), "local")
events = client.receive_datagram(datagram)
assert [event.kind for event in events] == ["object"]
```

subgroup は 2 つの経路で失敗する。どちらもエラーは同じである。

- ヘッダとオブジェクトを同じ断片で投入すると、`recv_subgroup_header` が
  `UnknownTrackAlias` を返した直後に同じ断片のオブジェクトが状態機械へ渡り、
  `RuntimeError: session error 0x3: subgroup object received before subgroup header` になる
- ヘッダだけを先に投入し、続けてオブジェクトだけを投入しても同じエラーになる

原因は 2 つある。

1. `moqt.moqt.Session.receive_data_stream` が `Session::recv_subgroup_header` の戻り値
   (`TrackDataAcceptance`) を捨てている。moqt-rs の `recv_subgroup_header` は
   `resolve_peer_track_alias` が空のとき `UnknownTrackAlias` を返し、ストリームの状態を
   `AwaitingHeader` のまま残す。moqt-py はそれを「ヘッダ受理」とみなして
   `data_headers` と `data_headers_decoded` に記録し、続くオブジェクトを
   `recv_subgroup_object` へ渡すため `SESSION_PROTOCOL_VIOLATION` になる
2. データグラムは `receive_datagram` が `unknown_track_alias` を返し、
   `Runtime._apply_events` が受理結果のイベントを捨てるため、オブジェクトが失われる

どちらも「購読がまだ確定していない」ことだけが理由であり、確定後は正常に処理できる。

`SESSION_PROTOCOL_VIOLATION` は moqt-rs の `recv_subgroup_object` が `Err` を返すだけで
`fail()` を呼ばないため、`Session` は `established` のまま残る。`last_error` はセッションを
閉じるイベントでだけ設定される値であり、None のままであることからもセッションが
終了していないことを確認できる。失われるのはオブジェクトと、例外を送出する受信経路である。

## 設計方針

購読に紐づかなかったデータを保留し、購読が確定したあとに再試行する。

### subgroup ストリーム (`src/core.rs` の `CoreSession`)

- `CoreSession::receive_data_stream` は `recv_subgroup_header` の戻り値を見て、受理結果が
  `UnknownTrackAlias` の場合はデコーダを破棄し、受信バイト列を `data_buffers` に保持した
  ままにする。`data_headers` と `data_headers_decoded` への記録は受理結果が `Accepted` の
  ときにだけ行い、オブジェクトを状態機械へ渡さない。ストリームは保留として記録する
- 受信バイト列を再試行で再投入してはならない (`data_buffers.push` が二重に積まれる)。
  再試行は保持済みのバイト列から新しいデコーダを作って回し、同じ断片をもう一度デコーダへ
  渡さない (デコーダは自分が受け取ったバイト列を保持する)
- 購読の確定後に再試行すると `recv_subgroup_header` が受理し、保留していたオブジェクトが
  配信される
- `FilteredOut` は購読が確定していてもフィルタで落ちているため再試行しても変わらない。
  デコーダと受信バイト列を捨て、ストリームを以後無視する
- `Discarded` は状態機械が破棄対象として登録済みであり、以降のオブジェクトは
  `recv_subgroup_object` の冒頭で破棄として吸収されるため、そのまま渡してよい
- 保留したストリームを再試行する `CoreSession.retry_pending_data_streams` を追加する。
  購読を登録した直後と定期処理から呼ぶ
- 保留は上限つきとする。保持バイト列が既存の `MAX_STREAM_BUFFER_BYTES` (128 KiB) を
  超えるストリームは保留せず捨てる。判定は `StreamBuffers.push` が例外を返す前に
  行い、受信経路が例外を送出しないようにする (`push` の例外は I/O 層まで伝播して
  接続を失敗させる)。保留するストリームの本数は `MAX_PENDING_DATA_STREAMS` (256) を
  新設し、超えたら保留をすべて捨てる。順序を持つ構造は持ち込まない
- 保留中のストリームが FIN または RESET で終端した場合は、保留と保持バイト列を捨てる。
  閉じたストリームを再試行すると `recv_subgroup_header` が未知の stream id として失敗する

### データグラム (`python/moqt/moq/_runtime.py` の `Runtime`)

- `Runtime.receive_datagram` が `unknown_track_alias` を受け取った場合、生バイト列を
  到着順に保留する。保留したデータグラムは `Runtime.retry_pending_datagrams` が
  再試行し、1 回の呼び出しで各データグラムを 1 回だけ試す
- `filtered_out` と `discarded` は保持しない。前者は購読が確定していてフィルタで
  落ちており、後者は状態機械が破棄対象として登録済みであり、どちらも再試行しても
  結果が変わらない
- オブジェクトのイベントは購読が確定した後に届くため、購読の登録が `Client` 側で
  終わる前に届いた分は `Client._pending_objects` の経路で取りこぼされない
- 再試行は `Runtime.tick` の定期処理から行う。データグラムには購読の登録に相当する
  明示的な契機が無いため、`Client.subscribe` からは呼ばない
- 保持する件数は `MAX_PENDING_DATAGRAMS` (256)、再試行の回数は
  `MAX_DATAGRAM_RETRY_ATTEMPTS` (64) を新設して上限とする。`unknown_track_alias` を
  返した回数を 1 回として数え、超えたデータグラムは破棄する
- セッションが終了したとき (`Runtime.close` / `Runtime._finish_session`) は保持を捨てる。
  購読の終了 (`request_terminated` / PUBLISH_DONE) では捨てない。Track Alias の共有や
  再購読で同じ Alias が再び使われる可能性があり、購読単位で保持を判断できないためである
- 購読に紐づかないと確定したデータグラムは破棄され、受信経路は例外を送出しない

## 実装対象

- `src/core.rs`: `receive_data_stream` の受理結果の分岐、保留の保持と再試行
  (`retry_pending_data_streams`)、`receive_data_stream_closed` での解放
- `python/moqt/moq/_runtime.py`: データグラムの保留と再試行
  (`Runtime.retry_pending_datagrams`)、`Runtime.tick` からの呼び出し、
  セッション終了時の解放
- `python/moqt/moq/client.py`: `Client.subscribe` が購読を登録した直後の再試行。
  `_subscriptions_by_alias` の登録後、`_pending_objects` を流す前に呼ぶ
- `tests/test_moqt.py`: sans I/O のテスト
- `tests/test_e2e.py`: SUBSCRIBE_OK の直後にデータグラムを送るテスト

## 完了条件

- 購読が確定する前に届いた subgroup のオブジェクトが、購読の確定後に配信されること
- 購読が確定する前に届いたデータグラムが、購読の確定後に配信されること
- どちらの場合も受信経路が例外を送出せず、`Session` が `established` のままであること
- 購読に紐づかない subgroup ストリームが保留の上限を超えたら捨てられ、受信経路が
  例外を送出しないこと
- 購読に紐づかないデータグラムが保持の上限を超えたら捨てられ、受信経路が
  例外を送出しないこと
- `FilteredOut` の subgroup ストリームを受信経路の例外なしに読み捨てられること
- e2e テスト `test_object_published_right_after_subscribe_ok_is_delivered` が通ること
- SUBSCRIBE_OK の直後にデータグラムを送る e2e テストを追加し、通ること
- sans I/O で決定的に再現するテストを追加すること
  - `receive_data_stream` の `UnknownTrackAlias` 経路 (ヘッダとオブジェクトが同じ断片の場合と
    別々の断片の場合)
  - `receive_datagram` の `unknown_track_alias` 経路
  - 保留したストリームが FIN で終端した場合に捨てられること

## 解決方法

購読に紐づかなかったデータを保留し、購読が確定したあとに再試行するようにした。

### 受信経路 (`src/core.rs`)

`CoreSession::receive_data_stream` を、種別の通知と上限判定を行う薄い入口に変え、
デコード本体を `receive_buffered_data_stream` へ切り出した。

- `recv_subgroup_header` の戻り値 (`TrackDataAcceptance`) を見て分岐するようにした。
  `Accepted` のときだけ `data_headers` と `data_headers_decoded` に記録する
- `UnknownTrackAlias` のときはデコーダを破棄し、受信バイト列を `data_buffers` に
  保持したまま `pending_data_streams` へ入れる。オブジェクトは状態機械へ渡さない
- `FilteredOut` のときはデコーダと受信バイト列を捨て、`ignored_data_streams` に
  入れて以後のバイト列を読み捨てる
- `Discarded` のときはそのまま状態機械へ渡す (以降のオブジェクトは破棄として吸収される)
- 保留したストリームを再試行する `retry_pending_data_streams` を追加した。
  `data_buffers` のバイト列からデコーダを作り直すため、同じ断片を二重に積まない
- 保留の本数は `MAX_PENDING_DATA_STREAMS` (256) を上限とし、超えたら保留をすべて捨てる。
  保持バイト列が `MAX_STREAM_BUFFER_BYTES` (128 KiB) を超えるストリームは
  `StreamBuffers::fits` で `push` の前に判定し、例外ではなく破棄で扱う
- `receive_data_stream_closed` で保留と無視の登録を捨てる

### データグラム (`python/moqt/moq/_runtime.py`)

- `Runtime.receive_datagram` が `unknown_track_alias` を受け取ったとき、生バイト列を
  `_pending_datagrams` に到着順で保持する
- `Runtime.retry_pending_datagrams` が 1 回の呼び出しで各データグラムを 1 回だけ試し、
  まだ `unknown_track_alias` のものは保持し直す
- 保持は `MAX_PENDING_DATAGRAMS` (256)、再試行は `MAX_DATAGRAM_RETRY_ATTEMPTS` (64) を
  上限とし、超えたら警告ログを出して破棄する
- `Runtime.tick` から再試行を呼ぶ。セッション終了時 (`Runtime.close` と
  `Runtime._finish_session`) は保持を捨てる

### 購読の登録直後 (`python/moqt/moq/client.py`)

`Client.subscribe` が `_subscriptions_by_alias` へ登録した直後、`_pending_objects` を
流す前に `Runtime.retry_pending_data_streams` を呼ぶ。

### テスト

- `tests/test_moqt.py`: 同じ断片・別断片で届いた subgroup が購読の確定後に配信されること、
  購読に紐づかない subgroup が上限超過で捨てられセッションが継続すること、
  `FilteredOut` の subgroup を例外なしに読み捨てること、購読の確定前に届いた
  データグラムが確定後に受理されることを追加した
- `tests/test_e2e.py`: SUBSCRIBE_OK の直後にデータグラムを送る
  `test_datagram_published_right_after_subscribe_ok_is_delivered` を追加した
