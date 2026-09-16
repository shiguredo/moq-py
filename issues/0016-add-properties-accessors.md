# TrackProperties の encode/decode と ObjectProperties の汎用アクセサを公開する

- Created: 2026-09-16
- Completed: 2026-09-16
- Branch: feature/add-properties-accessors
- Polished:

## 目的

properties ブロックを単体でシリアライズ・走査できるようにする。

moqt-py は properties を「型番号をキーにした辞書」として扱うため、未知の型番号を持つ
properties をそのまま往復させたり、任意の型番号の値を引いたりできない。subscriber が
アプリ独自の Property を扱うには生バイト列と KVP の走査 API が要る。

## 現状

`TrackProperties` は `add` / `to_dict` / 個別 getter のみで、`encode` / `decode` が無い。
`ObjectProperties` には `encode` / `decode` があるが、`as_slice` / `iter` / `find_varint` に
相当する API が無く、個別 getter と `to_dict` だけである。

moqt-rs は両方に `encode` / `decode` / `as_slice` / `iter` / `find_varint` を持つ。

## 設計方針

`TrackProperties` に `encode` / `decode` を追加する。両方に生バイト列の取得と
`(型番号, エンコード済み値)` の列挙、任意の型番号の varint 取得を追加する。

## 完了条件

- `TrackProperties` を単体で encode / decode できること
- 両方の properties で生バイト列の取得と KVP の列挙ができること
- 任意の型番号の varint 値を引けること
- テストで確認できること

## 解決方法

`src/properties.rs` に次の API を追加した。

- `TrackProperties.encode()` / `TrackProperties.decode(data)`: 長さプレフィックスを
  持たない KVP 列そのものを扱う。`moqt-rs` の `TrackProperties::encode` / `decode` に
  対応し、空の集合は 0 バイトになる
- `ObjectProperties.items()` / `TrackProperties.items()`: `(型番号, 値)` の列を
  `add()` で追加した順に返す。`__iter__` も同じ列を返し、列挙は
  スナップショットに対して行うため列挙中の `add()` の影響を受けない
- `ObjectProperties.find_varint(prop_type)` / `TrackProperties.find_varint(prop_type)`:
  任意の型番号の varint 値を引く。どちらも `IMMUTABLE_PROPERTIES` の内側を探索し、
  外側の値を優先する
- `ObjectProperties.encode()` を `encode_properties` へ改名し、`__bytes__` からの
  呼び出しだけに使う内部メソッドとして型スタブへ出さないようにした
- バイト列型の参照は `ObjectPropertiesExt` trait へ移し、Python の公開 API に
  含めないようにした

テストは `tests/prop_moqt.py` の
`prop_object_properties_round_trip` /
`prop_track_properties_round_trip` /
`prop_track_properties_encode_matches_session_output` と、
`tests/test_moqt.py` の `test_track_properties_round_trip` などで確認する。
