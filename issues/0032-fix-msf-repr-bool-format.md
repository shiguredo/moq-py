# MSF の構築 API の repr が Rust の bool 表記になる

- Created: 2026-09-16
- Completed: 2026-09-17
- Branch: feature/fix-msf-repr-bool-format
- Polished:

## 目的

Python から見た `repr` を Python の表記に揃える。

`repr` は対話環境やログ、テストの失敗メッセージに出る。Rust の内部表現が漏れると、
利用者は値を Python の値として解釈できず混乱する。

## 現状

`moqt.msf.Track` と `moqt.msf.Catalog` の `repr` に Rust の bool 表記が現れる。

```python
>>> repr(msf.Track(name="v", packaging="loc", is_live=True))
"Track(name=v, packaging=loc, is_live=true)"
>>> repr(msf.Catalog())
'Catalog(version=draft-01, tracks=0, is_complete=false)'
```

Python の bool は `True` / `False` と表示されるべきである。

`moqt.msf.Buffers` と `moqt.msf.RemoveTrack` の `Option` 表記は別途修正済みである。

## 設計方針

MSF の構築 API の `repr` を確認し、Rust の bool 表記が現れるものを Python の表記へ直す。
他の型にも同じ漏れがないか実際に `repr` を出して確認する。

## 完了条件

- 構築 API の `repr` に `true` / `false` が現れないこと
- テストで `repr` の内容を確認できること

## 解決方法

構築 API の `repr` を実際に出力して確認したところ、Rust の bool 表記が現れたのは
`Track` と `Catalog` の 2 つだった。

`src/msf.rs` に `bool` を Python の値と同じ表記で書き出す `format_bool` を
`format_optional` の隣に追加し、この 2 つの `__repr__` で bool をそのまま埋め込むのを
やめた。

- `Track.__repr__` の `is_live` は `True` や `False` と表示される
- `Catalog.__repr__` の `is_complete` は `True` や `False` と表示される

残りの構築 API (`CloneTrack` / `RemoveTrack` / `InitData` / `Buffers` / `Template` /
`AuthInfo` / `Accessibility` / `DeltaUpdate` / `MediaTimeline` / `EventTimeline` /
`Uri`) は bool のフィールドを `repr` に含めていないため変更していない。

`tests/test_msf.py` に `test_builders_repr_use_python_bool_notation` を追加した。
`Track` と `Catalog` は `repr` を完全一致で確認し、`is_complete` を真にした場合も
確認する。残りの構築 API は `repr` に `true` / `false` が現れないことを確認する。
