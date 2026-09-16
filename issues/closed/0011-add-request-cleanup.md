# 終了した request を状態機械から回収する

- Created: 2026-09-16
- Completed: 2026-09-16
- Branch: feature/add-request-cleanup
- Polished:

## 目的

終了した subscription / fetch / track_status がセッション内に残り続けるのを防ぐ。

状態機械は request ごとに購読状態と送受信ストリームの簿記を保持する。終了した request を
回収しないと、長時間動くセッションでメモリ使用量が増え続ける。

## 現状

native の `Session.forget_subscription` / `Session.forget_fetch` は公開されているが、
`moq.moq` の Python 層から一度も呼ばれていない。`Client._on_request_terminated` は
受信キューの終端を積むだけで、状態機械へ回収を指示しない。

`Session.forget_track_status` は native にも公開されていないため、`Client.track_status()` は
応答を受け取った後に状態を破棄できない。

## 設計方針

request の終了を観測した時点で `subscription_cleanup_ready` / `fetch_cleanup_ready` を確認し、
回収可能なら forget を呼ぶ。track_status についても native に `forget_track_status` と
`track_status_request` を追加し、応答受信後に回収する。

購読が終了しても同じ Request ID への参照が残っている可能性があるため、回収は
`*_cleanup_ready` が真を返したときだけ行う。

## 完了条件

- subscription / fetch / track_status の終了後に状態機械からエントリが消えること
- 終了前に forget を呼んでも安全であること
- テストで確認できること

## 解決方法

`moqt.moq._runtime.Runtime` に `cleanup_terminated_requests` を追加し、終了した
subscription / fetch / track_status を状態機械から回収するようにした。回収は
`subscription_cleanup_ready` / `fetch_cleanup_ready` が真を返したときだけ行う。
TRACK_STATUS には `*_cleanup_ready` が無いため、応答 (`pending` 以外) が記録された
エントリだけを回収する。

回収を呼ぶ契機は 2 つある。

- `Client._on_request_terminated`: request の終了を観測した時点で回収する
- `Runtime.tick`: 購読の drain 満了やデータストリームの終端はイベントを伴わずに
  後から回収可能になるため、定期処理でも観測する

native 側は `src/core.rs` に `Session.forget_track_status` を追加した。応答の有無は
既存の `track_status_request` で確認できる。`forget_track_status` は応答前に request
stream が終端した場合も REQUEST_ERROR が記録されているため回収できる。

`moq.moq.Fetch` に `cancel()` を追加し、subscriber 側から fetch を取り消せるように
した。`Runtime.send_fetch_stop_sending` が状態機械へ STOP_SENDING を指示し、データ
ストリームの終端を確認したうえで回収される。

テストは `tests/test_e2e.py` の
`test_terminated_subscription_is_removed_from_the_session` /
`test_cancelled_fetch_is_removed_from_the_session` と、`tests/test_moqt.py` の
`test_terminated_subscription_is_forgotten_only_after_cleanup_is_ready` /
`test_terminated_fetch_is_forgotten_only_after_cleanup_is_ready` /
`test_track_status_is_forgotten_only_after_the_response` /
`test_track_status_is_forgotten_after_the_stream_ended_without_a_response` で確認する。
