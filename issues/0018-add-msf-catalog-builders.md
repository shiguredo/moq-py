# MSF カタログを構築する API を追加する

- Created: 2026-09-16
- Completed:
- Branch: feature/add-msf-catalog-builders
- Polished:

## 目的

MSF カタログと delta 更新を、JSON 文字列を経由せずに組み立てられるようにする。

配信側のアプリは Track の一覧を動的に作る。現状はカタログを組み立てる手段が
JSON 文字列の組み立てしかなく、draft の MUST に沿った形へ正規化する責任がアプリ側にある。

## 現状

`moqt.msf.Catalog` は `new` / `decode` / `parse` / `apply_delta` (JSON 文字列) / `encode` のみで、
Track を直接追加・削除する API が無い。delta 更新も `DeltaUpdate` が decode 専用で、
操作を組み立てる口が無い。

moqt-rs は `MsfTrack` / `MsfCloneTrack` / `MsfRemoveTrack` / `MsfDeltaOperation` /
`MsfDeltaUpdate` を持ち、検証済みの構造として組み立てられる。

## 設計方針

moqt-rs の型に対応する構築 API を追加する。追加した Track と delta 操作は encode 時に
draft の MUST に照らして検証し、不正な状態は `ValueError` にする。

## 完了条件

- Python から Track を追加・削除・複製したカタログを組み立てて encode できること
- delta 更新の操作列を組み立てて encode でき、適用側で反映されること
- 不正な組み合わせが `ValueError` になること
- テストで確認できること
