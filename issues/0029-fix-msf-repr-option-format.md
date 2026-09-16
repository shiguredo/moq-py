# MSF の構築 API の repr が Rust の Option 表記になる

- Created: 2026-09-16
- Completed: 2026-09-17
- Branch: feature/fix-msf-repr-option-format
- Polished:

## 目的

Python から見た `repr` を Python の表記に揃える。

`repr` は対話環境やログ、テストの失敗メッセージに出る。Rust の内部表現が漏れると、
利用者は値を Python の値として解釈できず混乱する。

## 現状

`moqt.msf.RemoveTrack` などの `repr` に Rust の `Option` の表記が現れる。

```python
>>> repr(msf.RemoveTrack(name="v", namespace="ns"))
'RemoveTrack(name=v, namespace=Some("ns"))'
```

`namespace` は Python 側では `str | None` であり、`None` のときは `None` と表示されるべきで
ある。

## 設計方針

MSF の構築 API (`Track` / `CloneTrack` / `RemoveTrack` / `InitData` / `Buffers` /
`Template` / `AuthInfo` / `Accessibility` など) の `repr` を確認し、Rust の `Option` 表記が
現れるものを Python の表記へ直す。

## 完了条件

- 構築 API の `repr` に `Some(` が現れないこと
- `None` の値が `None` と表示されること
- テストで `repr` の内容を確認できること

## 解決方法

構築 API の `repr` を実際に出力して確認したところ、Rust の `Option` 表記が現れたのは
`Buffers` と `RemoveTrack` の 2 つだった。

`src/msf.rs` に `Option` を Python の値と同じ表記で書き出す `format_optional` を
追加し、この 2 つの `__repr__` で `{:?}` を使うのをやめた。

- `Buffers.__repr__` の `target` / `min` / `max` (`Option<u64>`) は `100` や
  `None` と表示される
- `RemoveTrack.__repr__` の `namespace` (`Option<String>`) は `ns` や `None` と
  表示される

残りの構築 API (`Track` / `CloneTrack` / `InitData` / `Template` / `AuthInfo` /
`Accessibility` / `Catalog` / `DeltaUpdate` / `MediaTimeline` / `EventTimeline` /
`Uri`) は `Option` のフィールドを `repr` に含めていないため変更していない。

`tests/test_msf.py` に `test_builders_repr_omit_rust_option_notation` を追加した。
`RemoveTrack` と `Buffers` は `repr` を完全一致で確認し、`None` が `None` と表示される
ことも確認する。残りの構築 API は `repr` に `Some(` が現れないことを確認する。

なお `Track.is_live` と `Catalog.is_complete` の `repr` は Rust の bool 表記
(`true` / `false`) のままである。設計方針が対象とするのは `Option` の表記であるため、
この変更には含めず、別途対応する。
