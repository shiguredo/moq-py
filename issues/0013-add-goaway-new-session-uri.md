# GOAWAY で新しいセッション URI を指定できるようにする

- Created: 2026-09-16
- Completed:
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
