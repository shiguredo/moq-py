# 購読が確定する前に届いたデータでセッションが落ちる

- Created: 2026-09-16
- Completed:
- Branch: feature/fix-data-before-subscribe-ok
- Polished:

## 目的

購読が確定する前に届いたオブジェクトやデータグラムでセッションを落とさず、取りこぼさない。

publisher が SUBSCRIBE_OK を返した直後にオブジェクトを送るのは自然な実装である。一方で
SUBSCRIBE_OK は bidi ストリーム、subgroup は uni ストリーム、データグラムはデータグラムと
別々の経路で届くため、subscriber が SUBSCRIBE_OK を処理するより先にオブジェクトを
処理することがある。この並び順は異常ではなく、subscriber 側が耐えなければならない。

## 現状

e2e テストを並列で負荷をかけて繰り返すと 40 回に 2 から 3 回落ちる。

- `tests/test_e2e.py` の `test_object_published_right_after_subscribe_ok_is_delivered` が
  タイムアウトする
- `tests/test_e2e.py` の `test_datagram_properties_reject_a_non_normal_status` などの
  データグラムのテストがタイムアウトする
- `tests/test_e2e.py` の `test_object_status_is_delivered[datagram-*]` も同じ

計測すると、クライアントは次の順で処理していた。

```text
receive_stream stream=19 bytes=3 events=[]          # 最初の断片 (ヘッダの一部)
cb stream_data stream=19 bytes=11
cb stream_data error RuntimeError('session error 0x3: subgroup object received before subgroup header')
subscribe:resumed request_id=0 alias=1 pending=[]   # SUBSCRIBE_OK の処理はこの後
```

原因は 2 つある。

1. `moqt.moqt.Session.receive_data_stream` が `Session::recv_subgroup_header` の戻り値
   (`TrackDataAcceptance`) を捨てている。moqt-rs の `recv_subgroup_header` は
   `resolve_peer_track_alias` が空のとき `UnknownTrackAlias` を返し、ストリームの状態を
   `AwaitingHeader` のまま残す。moqt-py はそれを「ヘッダ受理」とみなして
   `data_headers_decoded` に記録し、続くオブジェクトを `recv_subgroup_object` へ渡すため
   `SESSION_PROTOCOL_VIOLATION` になりセッションが落ちる
2. データグラムは `receive_datagram` が `unknown_track_alias` を返して黙って破棄される

どちらも「購読がまだ確定していない」ことだけが理由であり、確定後は正常に処理できる。
sans I/O で再現できる。

```python
# SUBSCRIBE_OK を渡す前にデータグラムを渡すと unknown_track_alias になる
client.receive_datagram(datagram)  # => events == ["unknown_track_alias"]
client.receive_request_stream(4, subscribe_ok_bytes, "local")
client.receive_datagram(datagram)  # => events == ["object"]
```

## 設計方針

購読に紐づかなかったデータを保留し、購読が確定したあとに再試行する。

- `receive_data_stream` は `recv_subgroup_header` の戻り値を見て、ストリームが購読に
  紐づかなかった場合 (`UnknownTrackAlias`) はデコーダを破棄して受信バイト列を保持したまま
  にし、オブジェクトを状態機械へ渡さない。ストリームは保留として記録する
- `FilteredOut` は購読が確定していてもフィルタで落ちているため再試行しても変わらない。
  このストリームは以後無視する (データを読み捨て、セッションは落とさない)
- `Discarded` は状態機械が破棄対象として登録済みであり、以降のオブジェクトは破棄として
  吸収されるため、そのまま渡してよい
- 保留したストリームを再試行する API を追加する。購読の登録直後と定期処理から呼ぶ
- データグラムは `Runtime` が生バイト列を保留し、同じく購読の登録直後と定期処理で再試行する。
  `unknown_track_alias` が続く間は保持し、上限 (件数と試行回数) を超えたら破棄する
- 保留は上限つきとし、無関係なトラックのデータグラムでメモリを使い続けない

## 完了条件

- 購読が確定する前に届いた subgroup のオブジェクトが、購読の確定後に配信されること
- 購読が確定する前に届いたデータグラムが、購読の確定後に配信されること
- どちらの場合もセッションが落ちないこと
- 真に未知の Track Alias のデータは保留の上限を超えたら破棄され、セッションは落ちないこと
- `test_object_published_right_after_subscribe_ok_is_delivered` が並列負荷でも通ること
- データグラムの e2e テストが並列負荷でも通ること
- sans I/O で決定的に再現するテストを追加すること
