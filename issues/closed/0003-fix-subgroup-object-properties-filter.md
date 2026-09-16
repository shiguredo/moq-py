# 送信 subgroup オブジェクトの Properties をフィルタ評価へ渡す

- Created: 2026-09-16
- Completed: 2026-09-16
- Branch: feature/fix-subgroup-object-properties-filter
- Polished:

## 目的

OBJECT_PROPERTY_FILTER を設定した購読に対して publisher がオブジェクトを送れるようにする。

## 現状

`moq._runtime.Runtime.send_subgroup_object` は、wire へ書くバイト列を組み立てるために
`_encode_subgroup_object` へ `properties_data` を渡しているが、状態機械へ通知する
`CoreSession.send_subgroup_object` には常に `None` を渡している。

moqt-rs の `Session::send_subgroup_object` は `properties_bytes` を使って
`object_passes_filters` の OBJECT_PROPERTY_FILTER を評価する。Property が付いていない Object は
条件を満たせないため、フィルタ付きの購読では全オブジェクトが `SendRequestError::LocalFilterMismatch`
になり 1 件も送信されない。

## 再現手順

1. publisher 側が `MAX_FILTER_RANGES` を宣言する
2. subscriber が `PARAM_OBJECT_PROPERTY_FILTER` を含む SUBSCRIBE を送る
3. publisher が `Publication.send_object` で Condition を満たす Object を送る

期待: Object が届く
実際: `LocalFilterMismatch` として破棄され、1 件も届かない

## 設計方針

`_encode_subgroup_object` に渡している `properties_data` を状態機械にも渡す。wire へ書くバイト列と
状態機械が評価するバイト列を同一にし、二重管理をなくす。

## 完了条件

- OBJECT_PROPERTY_FILTER の条件を満たす Object が送信され、満たさない Object は送信されないこと
- e2e テストで確認できること

## 解決方法

`moq._runtime.Runtime.send_subgroup_object` が、wire へ書く Properties と同じ
`Properties Length | Key-Value-Pairs` のバイト列を `CoreSession.send_subgroup_object` へ
渡すようにした。`_properties_blob` で正規化したバイト列をエンコードと状態機械の評価の
両方に使う。

`tests/test_e2e.py` の `test_object_property_filter_selects_objects_by_property` で、
OBJECT_PROPERTY_FILTER を満たすオブジェクトだけが送信されることを確認する。
