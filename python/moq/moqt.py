"""MoQT (Media over QUIC Transport) のプロトコル層。

draft-ietf-moq-transport-21 の codec と sans I/O セッション状態機械を公開する。
この層はストリームの実体に触れない。呼び出し側が peer のストリーム種別を判定して
`Session.receive_*` を呼び、戻り値の `Event` に従ってバイト列を送る。

WebTransport を介した client / server は `moq.Client` と `moq.Server` が提供する。
このモジュールは、ワイヤのバイト列を直接組み立てて検証するテストや、実装が送出した
バイト列を検査するテストから使う。

MoQT は draft 由来であり、将来の改訂で変更される可能性がある。
"""

from moq import _native
from moq._native import (
    Event,
    Message,
    Session,
    classify_data_stream_type,
    decode_message,
    decode_varint,
    decode_varint_prefix,
    encode_varint,
    is_padding_datagram,
    setup_stream_type,
)

# SUBSCRIBER_PRIORITY の既定値
# (draft-ietf-moq-transport-21 §9.20.6 (SUBSCRIBER_PRIORITY Parameter))
DEFAULT_SUBSCRIBER_PRIORITY: int = 128

# ストリーム種別
# (draft-ietf-moq-transport-21 §6.4.1 (Unidirectional Streams) Table 3)
SETUP_STREAM_TYPE: int = _native.SETUP_STREAM_TYPE
FETCH_HEADER_TYPE: int = _native.FETCH_HEADER_TYPE
PADDING_STREAM_TYPE: int = _native.PADDING_STREAM_TYPE
PADDING_DATAGRAM_TYPE: int = _native.PADDING_DATAGRAM_TYPE

# メッセージパラメータ
# (draft-ietf-moq-transport-21 §9.20 (Control Message Parameters))
PARAM_OBJECT_DELIVERY_TIMEOUT: int = _native.PARAM_OBJECT_DELIVERY_TIMEOUT
PARAM_AUTHORIZATION_TOKEN: int = _native.PARAM_AUTHORIZATION_TOKEN
PARAM_RENDEZVOUS_TIMEOUT: int = _native.PARAM_RENDEZVOUS_TIMEOUT
PARAM_SUBGROUP_DELIVERY_TIMEOUT: int = _native.PARAM_SUBGROUP_DELIVERY_TIMEOUT
PARAM_EXPIRES: int = _native.PARAM_EXPIRES
PARAM_LARGEST_OBJECT: int = _native.PARAM_LARGEST_OBJECT
PARAM_FILL_TIMEOUT: int = _native.PARAM_FILL_TIMEOUT
PARAM_FORWARD: int = _native.PARAM_FORWARD
PARAM_SUBSCRIBER_PRIORITY: int = _native.PARAM_SUBSCRIBER_PRIORITY
PARAM_LOCATION_FILTER: int = _native.PARAM_LOCATION_FILTER
PARAM_GROUP_ORDER: int = _native.PARAM_GROUP_ORDER
PARAM_FILL_PARAMETERS: int = _native.PARAM_FILL_PARAMETERS
PARAM_SUBGROUP_FILTER: int = _native.PARAM_SUBGROUP_FILTER
PARAM_OBJECTID_FILTER: int = _native.PARAM_OBJECTID_FILTER
PARAM_PRIORITY_FILTER: int = _native.PARAM_PRIORITY_FILTER
PARAM_OBJECT_PROPERTY_FILTER: int = _native.PARAM_OBJECT_PROPERTY_FILTER
PARAM_TRACK_PROPERTY_FILTER: int = _native.PARAM_TRACK_PROPERTY_FILTER
PARAM_NEW_GROUP_REQUEST: int = _native.PARAM_NEW_GROUP_REQUEST
PARAM_TRACK_NAMESPACE_PREFIX: int = _native.PARAM_TRACK_NAMESPACE_PREFIX
PARAM_INCLUDE_PROPERTIES: int = _native.PARAM_INCLUDE_PROPERTIES

# REQUEST_ERROR のコード
# (draft-ietf-moq-transport-21 §16.11.2 (REQUEST_ERROR Codes))
REQUEST_INTERNAL_ERROR: int = _native.REQUEST_INTERNAL_ERROR
REQUEST_UNAUTHORIZED: int = _native.REQUEST_UNAUTHORIZED
REQUEST_TIMEOUT: int = _native.REQUEST_TIMEOUT
REQUEST_NOT_SUPPORTED: int = _native.REQUEST_NOT_SUPPORTED
REQUEST_GOING_AWAY: int = _native.REQUEST_GOING_AWAY
REQUEST_DOES_NOT_EXIST: int = _native.REQUEST_DOES_NOT_EXIST
REQUEST_INVALID_RANGE: int = _native.REQUEST_INVALID_RANGE
REQUEST_MALFORMED_TRACK: int = _native.REQUEST_MALFORMED_TRACK
REQUEST_PREFIX_OVERLAP: int = _native.REQUEST_PREFIX_OVERLAP

# PUBLISH_DONE のコード
# (draft-ietf-moq-transport-21 §16.11.3 (PUBLISH_DONE Codes))
PUBLISH_DONE_INTERNAL_ERROR: int = _native.PUBLISH_DONE_INTERNAL_ERROR
PUBLISH_DONE_UNAUTHORIZED: int = _native.PUBLISH_DONE_UNAUTHORIZED
PUBLISH_DONE_TRACK_ENDED: int = _native.PUBLISH_DONE_TRACK_ENDED
PUBLISH_DONE_GOING_AWAY: int = _native.PUBLISH_DONE_GOING_AWAY
PUBLISH_DONE_TOO_FAR_BEHIND: int = _native.PUBLISH_DONE_TOO_FAR_BEHIND
PUBLISH_DONE_MALFORMED_TRACK: int = _native.PUBLISH_DONE_MALFORMED_TRACK

# ストリーム reset のコード
# (draft-ietf-moq-transport-21 §16.11.4 (Stream Reset Codes))
STREAM_INTERNAL_ERROR: int = _native.STREAM_INTERNAL_ERROR
STREAM_CANCELLED: int = _native.STREAM_CANCELLED
STREAM_DELIVERY_TIMEOUT: int = _native.STREAM_DELIVERY_TIMEOUT
STREAM_MALFORMED_TRACK: int = _native.STREAM_MALFORMED_TRACK

# Object Status
# (draft-ietf-moq-transport-21 §11.1.2 (Object Status))
OBJECT_STATUS_NORMAL: int = 0x0
"""通常のオブジェクト。非 0 長のオブジェクトでは暗黙の値である。"""

OBJECT_STATUS_END_OF_GROUP: int = _native.OBJECT_STATUS_END_OF_GROUP
"""指定した Object ID 以降のオブジェクトが同じ Group に存在しない。"""

OBJECT_STATUS_END_OF_TRACK: int = _native.OBJECT_STATUS_END_OF_TRACK
"""指定した Location 以降のオブジェクトが存在しない。"""

# データグラムの合計サイズの目安。
#
# QUIC は 1200 バイトの UDP データグラムを必ず運べることを要求する
# (RFC 9000 §8.1)。DATAGRAM フレームは分割できないため、QUIC と HTTP/3 の
# オーバーヘッドを差し引いたこの値を超えるデータグラムは、経路 MTU によっては
# 通知なく破棄される。破棄は送信側から検知できない
# (draft-ietf-moq-transport-21 §11.2.1 (Object Datagram))。
#
# 経路 MTU が大きい場合 (典型的な Ethernet では 1500 バイト) はこれを超える
# データグラムも配送できるが、その上限は moq-py からは知り得ない。
MAX_DATAGRAM_SIZE: int = 1100
"""経路に依存せず配送できるデータグラムの合計サイズ (ヘッダを含む)。"""

# PUBLISHER_PRIORITY の既定値
# (draft-ietf-moq-transport-21 §9.20.5 (PUBLISHER_PRIORITY Parameter))
PUBLISHER_PRIORITY_DEFAULT: int = _native.PUBLISHER_PRIORITY_DEFAULT

__all__ = [
    "DEFAULT_SUBSCRIBER_PRIORITY",
    "FETCH_HEADER_TYPE",
    "MAX_DATAGRAM_SIZE",
    "OBJECT_STATUS_END_OF_GROUP",
    "OBJECT_STATUS_END_OF_TRACK",
    "OBJECT_STATUS_NORMAL",
    "PADDING_DATAGRAM_TYPE",
    "PADDING_STREAM_TYPE",
    "PARAM_AUTHORIZATION_TOKEN",
    "PARAM_EXPIRES",
    "PARAM_FILL_PARAMETERS",
    "PARAM_FILL_TIMEOUT",
    "PARAM_FORWARD",
    "PARAM_GROUP_ORDER",
    "PARAM_INCLUDE_PROPERTIES",
    "PARAM_LARGEST_OBJECT",
    "PARAM_LOCATION_FILTER",
    "PARAM_NEW_GROUP_REQUEST",
    "PARAM_OBJECTID_FILTER",
    "PARAM_OBJECT_DELIVERY_TIMEOUT",
    "PARAM_OBJECT_PROPERTY_FILTER",
    "PARAM_PRIORITY_FILTER",
    "PARAM_RENDEZVOUS_TIMEOUT",
    "PARAM_SUBGROUP_DELIVERY_TIMEOUT",
    "PARAM_SUBGROUP_FILTER",
    "PARAM_SUBSCRIBER_PRIORITY",
    "PARAM_TRACK_NAMESPACE_PREFIX",
    "PARAM_TRACK_PROPERTY_FILTER",
    "PUBLISHER_PRIORITY_DEFAULT",
    "PUBLISH_DONE_GOING_AWAY",
    "PUBLISH_DONE_INTERNAL_ERROR",
    "PUBLISH_DONE_MALFORMED_TRACK",
    "PUBLISH_DONE_TOO_FAR_BEHIND",
    "PUBLISH_DONE_TRACK_ENDED",
    "PUBLISH_DONE_UNAUTHORIZED",
    "REQUEST_DOES_NOT_EXIST",
    "REQUEST_GOING_AWAY",
    "REQUEST_INTERNAL_ERROR",
    "REQUEST_INVALID_RANGE",
    "REQUEST_MALFORMED_TRACK",
    "REQUEST_NOT_SUPPORTED",
    "REQUEST_PREFIX_OVERLAP",
    "REQUEST_TIMEOUT",
    "REQUEST_UNAUTHORIZED",
    "SETUP_STREAM_TYPE",
    "STREAM_CANCELLED",
    "STREAM_DELIVERY_TIMEOUT",
    "STREAM_INTERNAL_ERROR",
    "STREAM_MALFORMED_TRACK",
    "Event",
    "Message",
    "Session",
    "classify_data_stream_type",
    "decode_message",
    "decode_varint",
    "decode_varint_prefix",
    "encode_varint",
    "is_padding_datagram",
    "setup_stream_type",
]
