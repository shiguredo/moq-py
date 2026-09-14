# FETCH の応答受信後にセッションが閉じる

- Created: 2026-09-15
- Completed:
- Branch: feature/fix-fetch-response-closes-session
- Polished:
- Reporter: @voluntas

## 目的

MoQT の FETCH を使い、過去のオブジェクトを取得できるようにする。

クライアント役の機能として FETCH の送信と fetch stream の受信は実装済みだが、
FETCH_OK を受け取った後にセッションが閉じてしまい、実際にはオブジェクトを
取得できない。この不具合を解消する。

## 現状

`moqt.Client.fetch()` から FETCH を送信し、サーバが FETCH_OK と fetch stream で
オブジェクトを返すと、クライアントの MoQT セッションが `code=0 reason=internal error`
で閉じる。`tests/test_e2e.py` の `test_fetch_receives_objects` がこの状態を再現する
（現在は `xfail` で無効化している）。

Sans I/O facade のレベルでは同じ手順が成功する。

- `tests/test_core_propagation.py` は `send_fetch` / `recv_request_stream` /
  `send_fetch_ok` / `recv_stream_message` の往復が通ることを確認している
- `send_fetch_header` / `send_fetch_object` / `send_fetch_data_stream_closed` も
  単体では動作する

したがって、WebTransport を介したときにだけ発生する順序の問題か、fetch 固有の
状態遷移のいずれかに原因がある。

## 再現手順

1. `Server` を起動し `Server.on_fetch` にコールバックを登録する
2. コールバックで `FetchRequest.respond()` を呼び、続けて
   `FetchResponse.send_object()` を 2 回呼び、`FetchResponse.close()` を呼ぶ
3. クライアントから `Client.fetch()` を呼ぶ

期待: `Fetch.objects()` から 2 件のオブジェクトが得られる
実際: セッションが `code=0 reason=internal error` で閉じ、`Client.fetch()` が
`SessionClosedError` を送出する

## 調査の手掛かり

- `moqt._native._CoreSession.last_error` に状態機械が通知した直近のエラー理由が
  入る。この不具合では `0x0 internal error` であり、個別の理由が失われている。
  ライブラリが `Session::fail()` を呼ぶ経路では `reason` が保持されるため、
  実際には `SessionError` 以外の理由で閉じている可能性がある
- FETCH_OK の受信と fetch stream のオブジェクト受信がほぼ同時に到着するため、
  `moqt._runtime.Runtime._start_request` が待つ future の解決と、fetch stream の
  デコードの順序が影響している可能性がある
- `moqt._runtime.Runtime._handle_close` に到達する前に例外が送出されている場合は
  `moqt._runtime.Runtime._on_stream_data` の `_fail_connect` が原因を握り潰す

## 完了条件

`tests/test_e2e.py` の `test_fetch_receives_objects` から `xfail` を外して通り、
FETCH で取得したオブジェクトの Group ID / Object ID / ペイロードが送信側と
一致すること。

## 解決方法

未調査。
