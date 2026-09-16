# Session の状態照会 API を公開する

- Created: 2026-09-16
- Completed: 2026-09-16
- Branch: feature/add-session-state-accessors
- Polished:

## 目的

セッションの内部状態をアプリから観測できるようにする。

moqt-rs は peer の宣言値やタイムアウト設定、GOAWAY の drain 進行状況を照会する API を公開して
いるが、moqt-py からは参照できず、アプリが終了処理や上限管理を判断できない。

## 現状

native の Session が公開する照会系 API は `subscription_track_alias` / `subscription_cleanup_ready` /
`fetch_cleanup_ready` / `state` / `last_error` などに限られる。次に対応する口が無い。

- `peer_max_auth_token_cache_size` (peer が SETUP で宣言した MAX_AUTH_TOKEN_CACHE_SIZE)
- `peer_alias_retention_ms` / `set_peer_alias_retention_ms`
- `control_message_timeout_ms` / `data_stream_timeout_ms` の getter (setter のみ公開済み)
- `goaway_drain_snapshot` / `goaway_drain_ready`
- `open_outgoing_fill_stream_count`
- `subscription` / `subscriptions` / `fetch` / `fetches` の一覧
- `track_status_request` / `track_status_requests`

## 設計方針

副作用の無い getter を追加する。一覧系は辞書またはタプルで返し、drain 状態は辞書で返す。
SETUP で受け取った値はキャッシュせず、状態機械から都度取得する。

## 完了条件

- 上記の値を Python から取得できること
- 値を取得してもセッションの状態が変化しないこと
- テストで確認できること

## 解決方法

`src/core.rs` に照会系の API を追加し、`moqt.moq` の高レベル層からも参照できるようにした。
SETUP で受け取った値はキャッシュせず、呼び出しごとに状態機械から取得する。

`moqt.moqt.Session` に追加した API:

- `peer_max_auth_token_cache_size` / `peer_alias_retention_ms` /
  `control_message_timeout_ms` / `data_stream_timeout_ms` は getter プロパティである。
  タイムアウトは無効の場合 `None` になる
- `set_peer_alias_retention_ms`: キャンセル済み alias の保持期間を変更する
- `goaway_drain_snapshot`: `blocking_subscription_request_ids` /
  `blocking_fetch_request_ids` / `blocking_track_status_request_ids` をキーにした辞書を返す
- `goaway_drain_ready`: drain が完了しているかを返す
- `open_outgoing_fill_stream_count`: open 中の送信 fill fetch stream 数を返す
- `subscription` / `subscriptions` / `fetch` / `fetches` /
  `track_status_request` / `track_status_requests`: 状態機械が保持する request の状態を
  辞書で返す。一覧は Request ID をキーにした辞書であり、保持していない Request ID は
  `None` になる
- subscription の辞書は `state` / `my_role` / `initiator` / `track_alias` / `forward` /
  `subscriber_priority` / `group_order` / `largest_location` /
  `largest_received_location` を持つ
- fetch の辞書は `state` / `my_role` / `fetch_start` / `end_location` / `end_of_track` /
  `response_received` を持つ
- TRACK_STATUS の辞書は `response` (`pending` / `ok` / `error`) と
  `largest_location` を持つ

`moqt.moq.Runtime` / `moqt.moq.Client` / `moqt.moq.ServerSession` から同じ値を
参照できる。`fetch` と `track_status` は request を開始する既存 API であるため、
1 件の状態照会は `fetch_state` / `track_status_state` という名前にした。

テストは `tests/test_moqt.py` の
`test_session_state_accessors_report_peer_declared_values` /
`test_subscription_state_accessors_report_the_subscription` /
`test_fetch_state_accessors_report_the_fetch` /
`test_track_status_state_accessors_report_the_response` /
`test_goaway_drain_accessors_report_blocking_requests` /
`test_session_state_accessors_do_not_change_the_session` などと、
`tests/test_e2e.py` の `test_session_state_accessors_report_the_live_session` で確認する。
