# SETUP オプションを送受信できるようにする

- Created: 2026-09-16
- Completed: 2026-09-16
- Branch: feature/add-session-setup-options
- Polished:

## 目的

moqt-rs の `SetupOptions` が扱う SETUP オプションを Python から設定・観測できるようにする。

現状は `MOQT_IMPLEMENTATION` しか送れないため、`MAX_FILTER_RANGES` を宣言できず Range Filter を使う
SUBSCRIBE を送信できない。`MAX_REQUEST_UPDATES` / `MAX_AUTH_TOKEN_CACHE_SIZE` / `AUTHORITY` / `PATH` /
`AUTHORIZATION_TOKEN` も同様に使えない。peer が宣言した値も読めないため、peer の上限に応じた
振る舞いをアプリが選べない。

## 現状

送信側の `CoreSession::new` は `SetupOptions` に `SETUP_OPTION_MOQT_IMPLEMENTATION` だけを push して
`Session::new_client` / `Session::new_server` に渡している。

受信側は `message_body_to_python` の `ControlMessage::Setup` 分岐が `moqt_implementation` で
`MOQT_IMPLEMENTATION` だけを取り出し、他のオプションを捨てている。

`moqt.moqt` は `SETUP_OPTION_*` を定数として公開済みだが、その値を使う口が無い。

## 設計方針

`CoreSession::client` / `CoreSession::server` に `setup_options` 引数を追加し、Setup Option Type を
キーにした辞書で受ける。偶数型は `int`、奇数型は `bytes`、`SETUP_OPTION_AUTHORIZATION_TOKEN` は
種別付きの辞書とする。

受信した SETUP の全オプションを Python から参照できる getter を追加し、peer が宣言した
`MAX_FILTER_RANGES` / `MAX_REQUEST_UPDATES` / `MAX_AUTH_TOKEN_CACHE_SIZE` / `AUTHORITY` / `PATH` /
`AUTHORIZATION_TOKEN` を取得できるようにする。

`moqt.moq.Client` / `moqt.moq.Server` にも同じ設定を渡せるようにする。

## 完了条件

- Python から上記 6 種の SETUP オプションを送信できること
- peer が宣言した値と受信した AUTHORIZATION_TOKEN を Python から取得できること
- `MAX_FILTER_RANGES` を宣言した publisher に対し、Range Filter 付き SUBSCRIBE が送信できること
- e2e テストで往復を確認できること

## 解決方法

`CoreSession::client` / `CoreSession::server` に `setup_options` 引数を追加し、Setup Option
Type をキーにした辞書で PATH / AUTHORITY / AUTHORIZATION_TOKEN /
MAX_AUTH_TOKEN_CACHE_SIZE / MAX_FILTER_RANGES / MAX_REQUEST_UPDATES を送れるようにした。
偶数型は varint、奇数型はバイト列、AUTHORIZATION_TOKEN は Token の辞書またはそのリストで
受ける。MOQT_IMPLEMENTATION は `implementation` 引数が担うため指定できない。

受信側は `CoreSession::receive_control` で peer の SETUP を控え、
`Session.peer_setup_options()` と `moq.moq.Client.peer_setup_options` から参照できるように
した。あわせて `Message.body["options"]` が全 Setup Option を返すようにした。

`tests/test_moqt.py` の `test_setup_options_are_sent_and_observed` で往復と
AUTHORIZATION_TOKEN の復元を、`test_range_filter_requires_the_peer_to_declare_max_filter_ranges`
で MAX_FILTER_RANGES の宣言後に Range Filter 付き SUBSCRIBE が送れることを確認する。
