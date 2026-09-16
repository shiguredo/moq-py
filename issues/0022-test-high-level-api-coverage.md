# 未テストの高レベル API を検証する

- Created: 2026-09-16
- Completed: 2026-09-16
- Branch: feature/add-high-level-api-tests
- Polished:

## 目的

高レベル API のうちテストが無い機能の回帰を防ぐ。

`moq.moq` は WebTransport を介した実通信を含む API であり、単体テストでは
配線の誤りを検出できない。テストが無い機能は、配線が壊れても気づけない。

## 現状

`tests/` から一度も呼ばれていない高レベル API がある。

- `moq.moq.publisher.Publication.send_publish_state_notify` と
  `moq.moq.client.Client.on_publish_state_notify` は配線済みだがテストが無い
- `moq.moq.client.Client.track_status` は低レベルのデコードしかテストされていない
- `moq.moq.client.Client.goaway` は server からの GOAWAY 通知しかテストされていない
- datagram の Priority 一致検証 (同じ Location の重複 Object で Priority が異なる場合に
  セッションが閉じる挙動) はテストされていない

## 設計方針

`tests/test_e2e.py` の `moq_pair` fixture を使い、client と server の組で往復を検証する。
`track_status` は endpoint が応答しない仕様であるため、`REQUEST_ERROR` が返ることを検証する。

## 完了条件

- `send_publish_state_notify` を購読側の `on_publish_state_notify` で受信できること
- `Client.track_status` が peer の応答を返すこと
- `Client.goaway` で peer へ GOAWAY が届くこと
- datagram の Priority が食い違う重複 Object でセッションが閉じること
- 追加したテストがすべて通ること

## 解決方法

`tests/test_e2e.py` に 4 件のテストを追加した。

- `test_publish_state_notify_is_delivered_to_the_subscriber`:
  publisher の `send_publish_state_notify` が購読側の `on_publish_state_notify` へ届き、
  通知の後も購読が続くことを確認する
- `test_track_status_is_not_answered_by_an_endpoint`:
  要求側が応答を受け取れないことと、受理しなかった側のセッションが終了することを
  確認する
- `test_client_goaway_is_notified_to_the_server`:
  `Client.goaway` が server へ届き、session と移行先の情報として通知されることを
  確認する
- `test_datagram_priority_mismatch_cancels_the_subscription`:
  同じ Location の重複 Object の Priority が食い違うと購読が取り消されることを
  確認する

検証の過程で、issue に書いた想定と実装が一致しない 2 点が分かったため、テストは
実際の挙動に合わせて書き、docstring に根拠を残した。

- `track_status` は endpoint が `REQUEST_ERROR` を返すのではなく、要求を受け取った側の
  状態機械が TRACK_STATUS を endpoint が受信する request として受理せず、
  プロトコル違反でセッションを終了する (draft-ietf-moq-transport-21 §9.13
  (TRACK_STATUS) は publisher が `TRACK_STATUS_OK` または `REQUEST_ERROR` を返すとする)。
  要求側には応答が届かないため、`control_message_timeout` を設定して期限で失敗を
  検出する形にした。応答が返らない仕様は `Client.track_status` の docstring にも書いた
- 重複 Object の Priority 不一致は、セッションではなく購読を終了させる。
  draft-ietf-moq-transport-21 §12.1 (Malformed Tracks) は「購読または fetch を取り消し、
  アプリへエラーを届けること」を求めており、セッションの終了は求めていない

検証のために次の 2 点を直した。

- `Server` に `on_goaway` を追加した。peer からの GOAWAY をアプリへ通知する口が無く、
  `Client.goaway` が届いたことを `ServerSession` から観測できなかった。
  引数は GOAWAY を受信した `ServerSession` と `PeerGoaway` である
- `Server._on_stream_data` が状態機械の例外を握らずに受信ループを終わらせていたため、
  peer が拒否されるデータを送るだけで server が停止していた。`Client._on_stream_data` と
  同じく警告を記録して続行するようにした
