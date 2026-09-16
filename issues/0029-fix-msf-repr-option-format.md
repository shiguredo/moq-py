# MSF の構築 API の repr が Rust の Option 表記になる

- Created: 2026-09-16
- Completed:
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
