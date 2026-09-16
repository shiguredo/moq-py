# Message.kind の doc から削除済みメッセージを除去する

- Created: 2026-09-16
- Completed:
- Branch: feature/fix-message-kind-doc
- Polished:

## 目的

削除済みの API を doc に残さない。

moqt-rs は relay 専用の namespace 発見・告知機構を削除しており、moqt-py も
`CODEBASE.md` の方針として削除された API を残さないと定めている。
doc に残っていると、存在しないメッセージ種別を前提とした実装を利用者が書いてしまう。

## 現状

`src/codec.rs` の `Message::kind` の doc が `setup` / `goaway` / `subscribe` / `subscribe_ok` /
`request_ok` / `request_error` / `request_update` / `publish` / `publish_done` /
`publish_skipped` / `publish_state_notify` / `fetch` / `fetch_ok` / `track_status` /
`publish_namespace` / `namespace` / `namespace_done` / `subscribe_namespace` /
`subscribe_tracks` を列挙している。

実際に `message_kind` が返すのは `setup` / `goaway` / `request_ok` / `request_error` /
`subscribe` / `subscribe_ok` / `request_update` / `publish` / `publish_done` /
`publish_state_notify` / `fetch` / `fetch_ok` / `track_status` の 13 種で、
`publish_skipped` / `publish_namespace` / `namespace` / `namespace_done` /
`subscribe_namespace` / `subscribe_tracks` は存在しない。

この doc は生成された `python/moqt/_native.pyi` にも出力される。

## 設計方針

doc の列挙を `message_kind` が実際に返す 13 種に合わせる。列挙の重複管理を避けるため、
列挙は `message_kind` の実装と一致していることをテストで確認する。

## 完了条件

- `Message::kind` の doc に削除済みメッセージが残っていないこと
- 型スタブを再生成しても同じ内容になること
