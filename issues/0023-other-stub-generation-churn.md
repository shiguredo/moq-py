# 型スタブ生成時の空白差分を解消する

- Created: 2026-09-16
- Completed: 2026-09-16
- Branch: feature/fix-stub-generation-churn
- Polished:

## 目的

`maturin develop --generate-stubs` のたびに `python/moqt/_native.pyi` に無意味な差分が出るのを防ぐ。

生成のたびに差分が出ると、`git status` が汚れて本当の変更が見えにくくなり、
コミットに無関係な空白変更が混ざる原因になる。

## 現状

`maturin develop --generate-stubs` が生成する `_native.pyi` は、doc コメントの空行に
行末空白を含む。リポジトリにコミットされている `_native.pyi` は `prek` の
`trailing-whitespace` フックで行末空白を除去した状態であり、生成するたびに
95 行の空白のみの差分が出る。差分は `git diff -w` で空になることを確認済みである。

## 設計方針

`python/moqt/_native.pyi` は maturin が生成する成果物であるため、`prek.toml` の
`trailing-whitespace` フックの対象から除外し、生成物の内容をそのままコミットする。
除外はファイル単位で指定し、他のファイルへのフックは維持する。

## 完了条件

- `uv run maturin develop --generate-stubs` の前後で `python/moqt/_native.pyi` に差分が出ないこと
- `prek run --all-files` が通ること
- 他のファイルに対する `trailing-whitespace` の検査が維持されていること

## 解決方法

`prek.toml` の `trailing-whitespace` フックに `python/moqt/_native.pyi` の除外を追加し、
maturin が生成する型スタブを行末空白ごとそのままコミットするようにした。

`uv run maturin develop --generate-stubs` の前後で `git status` が汚れないこと、
他のファイルでは `trailing-whitespace` が引き続き動作することを確認した。
