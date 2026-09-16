# MSF カタログを構築する API を追加する

- Created: 2026-09-16
- Completed: 2026-09-16
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

## 解決方法

`src/msf.rs` に構築 API を追加し、`moqt.msf` から公開した。

- `Track` / `CloneTrack` / `RemoveTrack` / `InitData` / `Buffers` / `Template` /
  `AuthInfo` / `Accessibility` を追加した。それぞれ `MsfTrack` / `MsfCloneTrack` /
  `MsfRemoveTrack` / `MsfInitData` / `MsfBuffers` / `MsfTemplate` / `MsfAuthInfo` /
  `MsfAccessibility` に対応する。属性名は draft のフィールド名を snake_case にしたもので、
  `#[pyclass(get_all, set_all)]` が getter / setter を生成する
- `Track` は `(name, packaging, is_live)`、`CloneTrack` は `(name, parent_name)` だけを
  位置引数に取り、残りは属性で設定する。`parentName` は clone 操作の中だけで意味を持つ
  ため (draft-ietf-moq-msf-01 §5.2.33)、`Track` には持たせない
- `Catalog.add_track` / `add_publish_track` / `add_init_data` / `apply_delta_update` と、
  `generated_at` / `is_complete` の setter を追加した
- `DeltaUpdate` に `new` と `add_tracks` / `remove_tracks` / `clone_tracks` と
  `generated_at` の setter を追加した。操作は追加した順に配列へ並び、その順に適用される
- `Template.resolve_entry` で draft §7.4.1 の式を Python から使えるようにした
- `packaging` は draft §5.2.4 の許容値へ変換し、一致しない場合は追加時に `ValueError` に
  する。draft の MUST 検証は encode 時に `MsfCatalogDocument::encode` が行うため、
  eventType と packaging の組み合わせや targetLatency と buffers の共存などは
  encode 時の `ValueError` になる
- README の `moqt.msf` に構築 API の例を追記した

テストは `tests/test_msf.py` の
`test_catalog_builds_tracks_without_json` /
`test_delta_update_adds_removes_and_clones_tracks_in_a_catalog` /
`test_catalog_rejects_event_type_outside_an_event_timeline` などで確認する。
