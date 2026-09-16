# SubgroupIdMode::FirstObjectId に対応する

- Created: 2026-09-16
- Completed:
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
