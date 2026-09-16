# Datagram で END_OF_GROUP を送れるようにする

- Created: 2026-09-16
- Completed:
- Branch: feature/add-datagram-end-of-group
- Polished:

## 目的

Object Datagram で Group の終端を通知できるようにする。

End of Group は Group 内の最後のオブジェクトであることを示す。datagram で送れると、
subgroup ストリームを開かずに Group の区切りを伝えられる。

## 現状

`moq._runtime._encode_object_datagram` は `end_of_group` 引数を持ち、
`draft-ietf-moq-transport-21 §11.2.1` の END_OF_GROUP bit を立てられるが、
呼び出し元の `moq._runtime.Runtime.send_object_datagram` が常に偽を渡しており、
`moq.moq.publisher.Publication.send_datagram` にも指定する口が無い。

moqt-rs の `Session::send_object_datagram` にも `end_of_group` に相当する引数が無いため、
状態機械へ通知する経路が存在しない。moqt-py だけでは実装できない。

## 設計方針

moqt-rs の `Session::send_object_datagram` に END_OF_GROUP を指定する引数が追加されたら、
moqt-py の native と Python 層に同じ引数を通す。

## 完了条件

- `Publication.send_datagram` から END_OF_GROUP を指定できること
- 受信側の `MoqtObject.status` で観測できること

## pending にする理由

moqt-rs の `Session::send_object_datagram` が `end_of_group` を受け取らないため、
moqt-py だけでは状態機械へ通知できない。

END_OF_GROUP の datagram を送るには、moqt-rs 側で
`Session::send_object_datagram` に引数を追加し、datagram のフィルタ評価と
重複 Object 検証へ反映する必要がある。moqt-rs の API が追加された時点で
この issue を reopened にして対応する。
