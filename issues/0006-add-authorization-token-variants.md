# AUTHORIZATION_TOKEN の 4 種を往復できるようにする

- Created: 2026-09-16
- Completed:
- Branch: feature/add-authorization-token-variants
- Polished:

## 目的

draft-ietf-moq-transport-21 §8.9 (Authorization Token Compression) が定める AUTHORIZATION_TOKEN の
4 種 (DELETE / REGISTER / USE_ALIAS / USE_VALUE) を Python から扱えるようにする。

AUTHORIZATION_TOKEN は alias を使った Token 圧縮のための仕組みであり、USE_VALUE しか送れないと
圧縮の恩恵が受けられず、peer が REGISTER した alias を参照できない。受信側も alias を復元できないと
登録内容を追跡できない。

## 現状

`parameter_value_from_python` は `(token_type, token_value)` のタプルだけを受け付け、
`MessageParameterValue::AuthorizationToken(AuthorizationToken::UseValue)` しか構築できない。
DELETE / REGISTER / USE_ALIAS を送る手段が無い。

`parameter_value_to_python` は `AuthorizationToken::Register` の `alias` を `let _ = alias;` として
捨てており、辞書へ `kind` / `token_type` / `token_value` しか入れない。`UseAlias` も `alias` を
`token_type` という名前で返しており、種別ごとの意味が揃っていない。

## 設計方針

パラメータ値の表現を `{"kind": "...", ...}` の辞書に統一し、4 種すべてを構築・復元できるようにする。
辞書のキーは種別ごとに `alias` / `token_type` / `token_value` を使い分ける。既存の
`(token_type, token_value)` タプルは後方互換のため受け付け続ける。

`decode_parameter` の doc と `moqt.moqt` の公開ドキュメントを新しい表現に合わせる。

## 完了条件

- 4 種すべてを Python から送信できること
- 受信した 4 種すべてから alias / token_type / token_value を正しく復元できること
- 既存のタプル表現が引き続き動作すること
- テストで往復を確認できること
