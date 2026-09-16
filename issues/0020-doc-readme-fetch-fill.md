# README の FETCH fill の記述を実装に合わせる

- Created: 2026-09-16
- Completed: 2026-09-16
- Branch: feature/update-readme-fetch-fill
- Polished:

## 目的

README の制限事項が実装と食い違っていると、利用者が使える機能を使わずに設計してしまう。

## 現状

`README.md` の「オブジェクトの送信」節の警告に「FETCH の fill (FILL_PARAMETERS による過去の
オブジェクトの補充) は未対応です」と書かれている。

しかし FILL_PARAMETERS には対応済みで、`moq.moq.Server.on_fill_fetch_stream` と
`moq._runtime.Runtime.open_fill_fetch_stream` が実装され、`tests/test_e2e.py` の
`test_fill_parameters_open_a_fill_fetch_stream` が検証している。

この警告は `moq.moq` を追加した時点の記述のままで、fill 対応時に更新されていない。

## 設計方針

警告から FETCH fill の記述を削除する。fill の使い方は `on_fill_fetch_stream` の doc に
書かれているため、README では言及しない。

## 完了条件

- README に「FETCH の fill は未対応」という記述が残っていないこと
- README の他の記述と実装が矛盾していないこと

## 解決方法

`README.md` の「オブジェクトの送信」節の警告から「FETCH の fill は未対応です」の行を
削除した。FILL_PARAMETERS は `moq.moq.Server.on_fill_fetch_stream` で対応済みであり、
記述が実装と矛盾していた。

README の他の記述 (低レベル API の例、対応仕様、制約) が実装と一致することを確認した。
