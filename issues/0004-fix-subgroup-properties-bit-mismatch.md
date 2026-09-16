# PROPERTIES bit と一致しない subgroup オブジェクトの送出を拒否する

- Created: 2026-09-16
- Completed:
- Branch: feature/fix-subgroup-properties-bit-mismatch
- Polished:

## 目的

Subgroup Header の PROPERTIES bit と矛盾するオブジェクトを wire に出さない。

## 現状

`moq._runtime.Runtime.send_subgroup_object` は「Properties を持たないストリームへ後から
`properties_data` を渡す」場合だけを拒否している。逆に「Properties を持つヘッダで
`properties_data` を省略したオブジェクトを送る」場合は素通しし、`_encode_subgroup_object` は
Properties フィールドを書かないため、ヘッダの PROPERTIES bit と一致しないオブジェクトが送出される。

moqt-rs の `SubgroupObject::encode` は `has_properties` が真で `properties_data` が無い組み合わせを
`ProtocolViolation` として拒否するが、moqt-py は独自エンコーダを使っておりこの検査が無い。

## 設計方針

Subgroup ストリームの送信状態を保持する `FetchWriter` / Subgroup の writer が持つ
`has_properties` と、そのオブジェクトに Properties があるかの一致を双方向で検査し、
不一致は `MoqtError` にする。

## 完了条件

- PROPERTIES bit が立った subgroup で Properties 無しのオブジェクトを送ろうとするとエラーになること
- Properties 無しの subgroup で Properties 付きのオブジェクトを送ろうとするとエラーになること
- どちらの場合も wire にバイト列が書かれないこと
- テストで確認できること
