# Session の状態照会 API を公開する

- Created: 2026-09-16
- Completed:
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
