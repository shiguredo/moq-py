"""MSF (MOQT Streaming Format) のカタログとタイムライン。

draft-ietf-moq-msf-01 のカタログ・メディアタイムライン・イベントタイムライン・
URI を扱う。いずれも JSON 文書であり、Rust 側が draft の MUST に照らした検証と
delta 更新の適用を担う。

カタログは ``catalog`` という Track 名で配信する (draft-ietf-moq-msf-01 §4.1)。
``CATALOG_TRACK_NAME`` がその名前である。

MSF は draft 由来であり、将来の改訂で変更される可能性がある。
"""

from moqt import _native
from moqt._native import (
    Catalog,
    DeltaUpdate,
    EventTimeline,
    MediaTimeline,
    Uri,
    parse_fragment_pairs,
    resolve_catalog_variables,
)

MSF_VERSION: str = _native.MSF_VERSION
"""このライブラリが対応する MSF のバージョン (draft-ietf-moq-msf-01 §5.1.1)。"""

CATALOG_TRACK_NAME: bytes = _native.MSF_CATALOG_TRACK_NAME
"""カタログを配信する Track 名 (draft-ietf-moq-msf-01 §4.1)。"""

__all__ = [
    "CATALOG_TRACK_NAME",
    "MSF_VERSION",
    "Catalog",
    "DeltaUpdate",
    "EventTimeline",
    "MediaTimeline",
    "Uri",
    "parse_fragment_pairs",
    "resolve_catalog_variables",
]
