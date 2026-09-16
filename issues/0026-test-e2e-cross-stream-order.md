# e2e テストが subgroup ストリーム間の到着順に依存している

- Created: 2026-09-16
- Completed:
- Branch: feature/fix-e2e-cross-stream-order
- Polished:

## 目的

e2e テストの間欠失敗をなくす。

到着順に依存したテストは、実行環境の負荷によって順序が入れ替わったときにだけ失敗する。
原因が分かりにくく、無関係な変更の CI や pre-push を落とすため、テストの信頼性が失われる。

## 現状

`tests/test_e2e.py` の `test_objects_in_a_second_group_are_delivered` が、受信した
オブジェクトの並びを `[(1, 0), (1, 1), (2, 0)]` と完全一致で検証している。

Group 1 と Group 2 は別の subgroup ストリームである。MoQT の subgroup はそれぞれ独立した
単方向ストリームで運ばれるため、ストリーム間の到着順は保証されない。同じストリーム内の
Object の順序だけが保証される。

実行環境の負荷によっては `[(2, 0), (1, 0), (1, 1)]` の順で届き、テストが失敗する。

## 設計方針

ストリームをまたぐ順序を前提にしない。受信したオブジェクトを Group ID ごとにまとめ、
Group 内の並びだけを検証する。

## 完了条件

- `test_objects_in_a_second_group_are_delivered` が Group をまたぐ到着順に依存しないこと
- Group ごとの Object ID とペイロードの対応は引き続き検証されること
- テストを繰り返し実行しても失敗しないこと
