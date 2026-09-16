# 終了した request を状態機械から回収する

- Created: 2026-09-16
- Completed:
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
