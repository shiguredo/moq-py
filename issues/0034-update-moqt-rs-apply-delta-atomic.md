# moqt-rs の develop 追従で delta 更新の失敗時の意味論が変わる

- Created: 2026-09-18
- Completed:

## 目的

`Cargo.toml` が追従すると定めている moqt-rs の `develop` ブランチの最新へ
`Cargo.lock` を更新し、moqt-rs 側の挙動変更に moqt-py の doc とテストを追随させる。

## 現状

`Cargo.lock` は moqt-rs の `482fd86` (2026-09-17) を固定している。`develop` の最新は
`424d1a5` (2026-09-18) であり、公開 API は rustdoc JSON の比較で完全に一致するが、
`MsfCatalog::apply_delta` の失敗時の挙動が変わっている。

- `482fd86`: 操作を配列順に適用し、途中で失敗しても先行する操作の結果が残る
- `424d1a5`: 複製へ適用して成功時のみ差し替える (copy-on-write)。失敗した呼び出しは
  カタログを変更しない

moqt-py は `src/msf.rs` の `Catalog::apply_delta` の doc で「途中で失敗した場合、
それまでの操作は取り消されない」と説明し、`tests/test_msf.py` の
`test_catalog_apply_delta_leaves_earlier_operations_applied_on_failure` でその挙動を
固定している。`cargo update -p shiguredo_moqt` を実行するとこのテストが失敗する。

## 設計方針

moqt-rs の新しい挙動 (原子的) を正とし、doc とテストをそれに合わせる。あわせて
生成物である `python/moqt/_native.pyi` を再生成する。

## 実装対象

- `Cargo.lock`: `shiguredo_moqt` を `424d1a5` へ更新する
- `src/msf.rs`: `Catalog::apply_delta` の doc を原子的な挙動に合わせる
- `python/moqt/_native.pyi`: `maturin develop --generate-stubs` で再生成する
- `tests/test_msf.py`: 失敗時にカタログが変更されないことを検証するテストへ変更する

## 完了条件

- `cargo update -p shiguredo_moqt` 後の `Cargo.lock` で `uv run pytest` が全件通ること
- `cargo test` が通ること
- `cargo fmt` / `cargo clippy` / `ruff` が通ること
- `python/moqt/_native.pyi` がビルドから再生成した内容と一致すること
