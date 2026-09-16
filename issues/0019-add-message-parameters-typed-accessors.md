# MessageParameters の型付きアクセサを公開する

- Created: 2026-09-16
- Completed: 2026-09-16
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

## 解決方法

`src/message_parameters.rs` を追加し、`moqt.moqt` から `MessageParameters` /
`LocationFilter` / `LocationFilterUpdate` を公開した。

- `MessageParameters` は `Event.parameters` / `Message.parameters` が返す「型番号を
  キーにしたエンコード済みバイト列の辞書」を受け取り、`to_dict()` で同じ辞書へ戻す。
  往復で内容が変わらない
- 型付きアクセサとして `largest_object` / `forward` / `expires` / `has_expires` /
  `group_order` / `object_delivery_timeout` / `subgroup_delivery_timeout` /
  `fill_timeout` / `subscriber_priority` / `include_properties` / `new_group_request` /
  `location_filter` / `location_filter_typed` / `location_filter_update` /
  `fill_parameters` / `authorization_tokens` / `track_namespace_prefix` /
  `range_filters` / `has_range_filters` / `range_filter_count` を追加した
- `bytes` 以外の値は型ごとの Python 表現 (`int` / `(group_id, object_id)` /
  namespace のフィールド列 / 入れ子の辞書 / Token) としても受け取る。`LOCATION_FILTER`
  には `LocationFilter` も渡せる
- 長さ付きバイト列のパラメータは、`Session.send_subscribe` などが取る長さプレフィックスを
  含まない本体と紛らわしいため、長さが合わない値は `ValueError` で拒否する
- `LocationFilter` は 5 種の種別 (`relative_group` / `next_object` / `absolute_start` /
  `absolute_range` / `absolute_range_with_end`) を持ち、`encode()` と `decode()` で
  wire 形式のフィルタ本体と往復する。種別と過不足のあるフィールドの組み合わせは
  `ValueError` になる
- `LocationFilterUpdate` は REQUEST_UPDATE の 3 状態 (`unchanged` / `removed` / `set`)
  を `kind` と `filter` で表す
- `MessageParameters` は moqt-rs の `MessageParameters` を包むため、パラメータ 1 件分の
  バイト列のデコードを `src/core.rs` の `decode_parameter_entry` へ集約し、
  `decode_parameter` と共用する

テストは `tests/test_moqt.py` の
`test_message_parameters_reads_typed_values` /
`test_message_parameters_round_trips_the_encoded_dictionary` /
`test_location_filter_round_trips_through_bytes` などと、
`tests/prop_moqt.py` の `prop_message_parameters_round_trip` /
`prop_location_filter_round_trip` で確認する。
