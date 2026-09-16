# オブジェクト途中の FIN を検出してセッションを閉じる

- Created: 2026-09-16
- Completed:
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
