# MSF の構築 API の repr が Rust の bool 表記になる

- Created: 2026-09-16
- Completed:
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
