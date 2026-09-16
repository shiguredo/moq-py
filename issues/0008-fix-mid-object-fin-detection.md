# オブジェクト途中の FIN を検出してセッションを閉じる

- Created: 2026-09-16
- Completed: 2026-09-17
- Branch: feature/fix-mid-object-fin-detection
- Polished:

## 目的

draft-ietf-moq-transport-21 §11.3 (Object) が求める、オブジェクトのシリアライズ途中で
ストリームが FIN された場合の検出を行う。

現状はシリアライズ途中の FIN が無検査で通るため、途中で切れたオブジェクトを正常な
オブジェクトとして扱ってしまう。

## 現状

受信側のデータストリームは moqt-rs の `SubgroupStreamDecoder` / `FetchStreamDecoder` で
デコードしているが、ストリーム終端で `finish()` を呼んでいない。

moqt-rs の `Session::report_mid_object_fin` は「Session は decoder を持たないため、
アプリケーションが decoder の `finish()` 失敗を検出して呼ぶ」と明記している。
moqt-py はこの API を native に公開しているが、Python 層から一度も呼んでおらず、
`finish()` を呼ぶ経路も無い。

## 設計方針

データストリームの終端処理で decoder の `finish()` を呼び、失敗した場合は
`report_mid_object_fin` を呼んでセッションを `PROTOCOL_VIOLATION` で閉じる。

`finish()` の結果を Python 層から使えるようにするため、native の `receive_data_stream_closed`
相当の処理の中で完結させる。

## 完了条件

- オブジェクトのシリアライズ途中で FIN したストリームを受信すると
  セッションが `PROTOCOL_VIOLATION` で閉じること
- ヘッダのみで FIN した空の Subgroup は正常に受理されること
- テストで確認できること

## 解決方法

`src/core.rs` の `receive_data_stream_closed` が、デコーダを破棄する前に
`SubgroupStreamDecoder::finish` / `FetchStreamDecoder::finish` を呼び、失敗した場合は
`Session::report_mid_object_fin` を呼ぶようにした。状態機械はセッションを
`PROTOCOL_VIOLATION` で閉じ、`close` イベントが `Runtime` へ届く。`report_mid_object_fin`
の戻り値は「呼び出し自体がセッションを閉じる判断」であるため、エラーは期待どおりの
結果として扱い、閉じるイベントは `drain_events` から取り出す。

RESET による終端は検査対象外とした。draft-ietf-moq-transport-21 §11.3.2
(Closing Subgroup Streams) の途中終了の検査は graceful な FIN に対するものである。
padding stream はバイト列を読み捨てるだけでオブジェクトを持たないため検査しない。
ヘッダのみでオブジェクトを持たない空の Subgroup と空の FETCH 応答は `finish()` が
成功するため、そのまま受理される。

`finish()` は native の終端処理の中で完結するため、新たな公開 API は追加していない。

テストは `tests/test_moqt.py` の `test_mid_object_fin_closes_the_session` /
`test_fetch_mid_object_fin_closes_the_session` /
`test_mid_object_reset_does_not_close_the_session` /
`test_empty_subgroup_fin_is_accepted` で確認する。公開 API はオブジェクトを途中まで
書く手段を提供しないため、実通信テストではなく状態機械へ直接バイト列を流し込む
低レベルテストで確認する。
