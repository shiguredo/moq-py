# MessageParameters の型付きアクセサを公開する

- Created: 2026-09-16
- Completed:
- Branch: feature/add-message-parameters-typed-accessors
- Polished:

## 目的

パラメータの意味付けをアプリ側で再実装せずに済むようにする。

LARGEST_OBJECT は Group ID と Object ID の組、LOCATION_FILTER は範囲、OBJECT_DELIVERY_TIMEOUT は
ミリ秒というように、パラメータごとに値の型と意味が draft で定まっている。生バイト列だけを
渡されると、アプリは draft の解釈を自前で持つことになる。

## 現状

パラメータは `Event.parameters` / `Message.parameters` で「型番号をキーにした
エンコード済みバイト列の辞書」として公開され、`moqt.moqt.decode_parameter` で 1 件ずつ
解釈するしかない。moqt-rs の `MessageParameters` が持つ
`largest_object` / `forward` / `expires` / `group_order` / `object_delivery_timeout` /
`subgroup_delivery_timeout` / `location_filter_typed` などの型付きアクセサに対応する口が無い。

## 設計方針

`MessageParameters` をラップする型を公開し、draft の意味付けを持つ getter を提供する。
辞書からの構築と辞書への変換も可能にし、既存の生バイト列の辞書とは相互に変換できるようにする。

## 完了条件

- LARGEST_OBJECT / FORWARD / EXPIRES / GROUP_ORDER / OBJECT_DELIVERY_TIMEOUT /
  SUBGROUP_DELIVERY_TIMEOUT / LOCATION_FILTER を Python から型付きで取得できること
- 生バイト列の辞書から構築でき、生バイト列の辞書へ戻せること
- テストで確認できること
