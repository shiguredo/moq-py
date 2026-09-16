# SubgroupIdMode::FirstObjectId に対応する

- Created: 2026-09-16
- Completed: 2026-09-16
- Branch: feature/add-subgroup-id-first-object-id
- Polished:

## 目的

Subgroup Header の Subgroup ID モード 3 種すべてを Python から扱えるようにする。

FirstObjectId モードは Subgroup ID を最初の Object ID で表現する圧縮形式であり、
Subgroup ID を別フィールドで送らない分だけ wire が短くなる。送信で選べないと
moqt-rs が持つ表現力を活かせず、受信で解決できないとオブジェクトがどの Subgroup に
属するかをアプリが判断できない。

## 現状

送信側の `CoreSession.send_subgroup_header` は `subgroup_id` が `None` のとき
`SubgroupIdMode::Zero` を選び、FirstObjectId を指定する手段が無い。

受信側は `SubgroupIdMode::FirstObjectId` のヘッダに対して `subgroup_id` を `None` のまま
イベントへ載せており、moqt-rs の `SubgroupStreamDecoder::resolved_subgroup_id` が返す
「最初のオブジェクト受信後に確定する Subgroup ID」を使っていない。

## 設計方針

送信側は `subgroup_id` に加えてモードを明示する引数を追加し、FirstObjectId を選べるようにする。
受信側は最初のオブジェクトを受信した時点の解決済み Subgroup ID をイベントへ載せる。

## 完了条件

- FirstObjectId モードで subgroup を送信できること
- FirstObjectId モードで受信したオブジェクトの `MoqtObject.subgroup_id` に
  最初の Object ID が入ること
- ヘッダ受信直後 (オブジェクト未受信) は `None` のままであること
- テストで確認できること

## 解決方法

送信側は `src/core.rs` の `send_subgroup_header` に `subgroup_id_mode` を追加し、
`zero` / `first_object_id` / `explicit` の 3 モードを指定できるようにした。モードと
`subgroup_id` の組み合わせが不正な場合は送信前に `ValueError` で拒否する。モード名は
`src/lib.rs` の `SUBGROUP_ID_MODE_*` として登録し、`moqt.moqt` から参照できる。

Python 側は `moqt.moq._runtime` に `SUBGROUP_ID_MODE_*` を置き、`_subgroup_type_byte`
が SUBGROUP_ID_MODE の 2 bit を組むようにした。Subgroup ID フィールドを書くのは
`explicit` モードだけである。`moqt.moq.Publication.send_object` に `subgroup_id_mode` を
追加し、省略時は `subgroup_id` の有無から `explicit` と `zero` を選ぶ。
`SubgroupWriter` がモードを保持し、同じ Group の途中でモードが変わると `MoqtError` に
なる。

受信側は `SubgroupStreamDecoder::resolved_subgroup_id` を使い、`first_object_id` モードの
オブジェクトイベントに「最初のオブジェクト受信後に確定した Subgroup ID」を載せる。
ヘッダ受信直後 (オブジェクト未受信) は `None` のままである。
`MoqtObject.subgroup_id` の doc を実態に合わせて更新した。

テストは `tests/test_e2e.py` の
`test_subgroup_id_mode_first_object_id_is_delivered` /
`test_subgroup_id_mode_cannot_change_within_a_group` /
`test_subgroup_id_modes_are_resolved_on_the_sending_side` と、`tests/test_moqt.py` の
`test_received_subgroup_resolves_the_subgroup_id_from_the_first_object` /
`test_received_subgroup_header_alone_does_not_resolve_the_subgroup_id` /
`test_received_subgroup_reports_an_explicit_subgroup_id_from_the_header` /
`test_send_subgroup_header_uses_the_first_object_id_mode` /
`test_send_subgroup_header_rejects_a_subgroup_id_in_the_first_object_id_mode` で確認する。
