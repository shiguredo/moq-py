# Server の到達しない TRACK_STATUS 応答分岐を削除する

- Created: 2026-09-16
- Completed:
- Branch: feature/remove-unreachable-track-status-branch
- Polished:

## 目的

到達しないコードを残さない。

到達しない分岐は、読む人に「この経路が動く」と誤解させる。moqt-py は独自のエンコーダと
状態機械の薄いラッパーで成り立っており、どこで何が起きるかの正確な見取り図が重要である。

## 現状

`moq.moq.server.Server._on_request` に `event.kind == "track_status"` の分岐があり、
`REQUEST_DOES_NOT_EXIST` を返す。

しかし moqt-rs の `Session::recv_request` は TRACK_STATUS を endpoint が受信する request
として受理せず、`SESSION_PROTOCOL_VIOLATION` でセッションを閉じる。したがって
アプリケーションのコールバックに `track_status` の request が届くことはなく、この分岐は
実行されない。

`tests/test_e2e.py` の `test_track_status_is_not_answered_by_an_endpoint` が、応答が
返らないことを確認している。

## 設計方針

到達しない `track_status` 分岐を削除する。あわせて、この分岐に依存している記述
(コメント・docstring) を整理する。

`moq.moq.Client.track_status` 自体の扱いは別途判断する。

## 完了条件

- `Server._on_request` から `track_status` の分岐が消えていること
- 既存のテストがすべて通ること
