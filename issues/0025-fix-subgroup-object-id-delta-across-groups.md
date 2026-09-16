# 同一 subscription で 2 つ目の Group を送ると Object ID の差分が負になる

- Created: 2026-09-16
- Completed: 2026-09-16
- Branch: feature/fix-subgroup-object-id-delta-across-groups
- Polished:

## 目的

1 本の subscription で複数の Group を配信できるようにする。

映像や音声の配信では Group が連続して進む。2 つ目の Group を送った時点で例外になり
配信が継続できないと、ライブラリとして実用にならない。

## 現状

`moq._runtime.Runtime.send_subgroup_object` は、Object ID の差分を
「直前のオブジェクトの Object ID との差」として計算する。この計算は Group が変わって
新しい subgroup ストリームを開く場合にも、閉じる前の writer が保持する
`last_object_id` を使って行われる。

そのため、Group 1 の Object 0 を送った後に Group 2 の Object 0 を送ると
`0 - 0 - 1 = -1` となり、`moqt.encode_varint` が
`OverflowError: can't convert negative int to unsigned` を送出する。

subgroup ストリームの最初のオブジェクトの Object ID は絶対値で書く
(draft-ietf-moq-transport-21 §11.3.1 (Subgroup Header)) ため、Group をまたぐ場合は
直前の Group の Object ID を基準にしてはならない。

## 再現手順

1. `Server.on_subscribe` で `subscribe_ok` を返す
2. `client.subscribe` で購読する
3. `Publication.send_object` を Group 1 / Object 0 で呼ぶ
4. `Publication.send_object` を Group 2 / Object 0 で呼ぶ

期待: 2 件とも届く
実際: `OverflowError: can't convert negative int to unsigned` になる

## 設計方針

Object ID の差分の基準を「同じ subgroup ストリーム内の直前のオブジェクト」に限定する。
新しい subgroup ストリームを開く場合は Object ID を絶対値として書く。

## 完了条件

- 同一 subscription で Group を進めても `OverflowError` にならないこと
- 2 つ目の Group のオブジェクトが受信側で正しい Group ID と Object ID で観測されること
- 同じ Group 内の 2 件目以降は従来どおり差分で表現されること
- e2e テストで確認できること

## 解決方法

`moq._runtime.Runtime.send_subgroup_object` の Object ID 差分の計算で、新しい subgroup
ストリームを開く場合 (`opens_stream` が真) は絶対値を使うようにした。差分の基準は
「同じ subgroup ストリーム内の直前のオブジェクト」だけであり、Group が変わると基準が
無くなる。

`tests/test_e2e.py` の `test_objects_in_a_second_group_are_delivered` で、Group 1 の 2 件と
Group 2 の 1 件が正しい Group ID と Object ID で届くことを確認する。
