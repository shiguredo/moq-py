# TrackProperties の encode/decode と ObjectProperties の汎用アクセサを公開する

- Created: 2026-09-16
- Completed:
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
