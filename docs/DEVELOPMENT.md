# 開発

```bash
# 依存の同期と Rust 拡張の開発ビルド (型スタブも生成する)
uv sync
uv run maturin develop --generate-stubs

# テスト
uv run pytest
```

Git フックは prek で管理しています。`prek.toml` のフック (cargo fmt / cargo clippy / ruff / ty / tombi / pytest / cargo test) をコミット時とプッシュ時に実行します。

```bash
prek install --prepare-hooks
prek run --all-files
```

moqt-rs は公開リポジトリの `develop` ブランチを追従します。実際にビルドしたコミットは `Cargo.lock` が固定します。最新へ更新するときは次を実行します。

```bash
cargo update -p shiguredo_moqt
```
