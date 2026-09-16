# GOAWAY で新しいセッション URI を指定できるようにする

- Created: 2026-09-16
- Completed: 2026-09-16
- Branch: feature/add-goaway-new-session-uri
- Polished:

## 目的

draft-ietf-moq-transport-21 §9.2 (GOAWAY) が運ぶ new session URI を送れるようにする。

new session URI は、セッションを閉じる側が接続先の移行先を peer へ伝えるための仕組みであり、
これが送れないとサーバの再起動や移転をクライアントへ通知できない。

## 現状

native の `Session.send_goaway` は `new_session_uri` と `timeout` を受けるが、
`moq._runtime.Runtime.send_goaway` が常に空バイト列を渡している。

`moq.moq.Client.goaway` と `moq.moq.ServerSession.goaway` は `timeout` しか受け取らず、
URI を指定する口が無い。

## 設計方針

高レベル API の `goaway` に `new_session_uri` 引数を追加し、`bytes` で受ける。
moqt-rs の `MAX_NEW_SESSION_URI_LENGTH` を超える値は送出前に `MoqtError` にする。

## 完了条件

- 任意の URI を載せた GOAWAY を送信できること
- peer 側の `Client.peer_goaway` から URI を復元できること
- 長さ上限を超える URI が拒否されること
- e2e テストで確認できること

## 解決方法

`moqt.moq.Runtime.send_goaway` が `new_session_uri` を受け取れるようにし、空バイト列を
固定で渡す実装をやめた。`moqt.moq.Client.goaway` と `moqt.moq.ServerSession.goaway` にも
`new_session_uri` 引数 (既定値は空バイト列、`timeout` の次の位置) を追加した。

- `MAX_NEW_SESSION_URI_LENGTH` を超える URI は状態機械へ渡す前に `MoqtError` にする。
  上限ちょうどの URI は送信できる
- URI を通知できるのは Server だけであり、Client が空でない URI を送ると状態機械が
  `PROTOCOL_VIOLATION` で拒否する。この規則は `goaway` の doc に明記した
  (draft-ietf-moq-transport-21 §9.2 (GOAWAY))
- 受信側は `PeerGoaway.new_session_uri` から URI を復元する

テストは `tests/test_e2e.py` の `test_server_goaway_carries_a_new_session_uri` と
`test_goaway_rejects_a_new_session_uri_beyond_the_length_limit` で確認する。
