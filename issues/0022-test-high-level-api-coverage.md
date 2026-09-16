# 未テストの高レベル API を検証する

- Created: 2026-09-16
- Completed:
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
