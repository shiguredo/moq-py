"""LOC (Low Overhead Media Container) のプロパティ。

draft-ietf-moq-loc-04 の LOC Properties を encode / decode する。プロパティ ID の
偶奇で値の型が決まる。偶数 ID は vi64、奇数 ID は長さ付きバイト列である
(draft-ietf-moq-loc-04 §2.3)。

LOC Properties は Public と Private に分類され、Public は MOQT の Object
Properties に、Private は MOQT の Object Payload に置かれる
(draft-ietf-moq-loc-04 §2.2)。どちらに置くかはアプリケーション層の責務であり、
このモジュールは両者を区別しない。

LOC は draft 由来であり、将来の改訂で変更される可能性がある。
"""

from moq import _native
from moq._native import LocProperties as Properties

# LOC プロパティ ID (draft-ietf-moq-loc-04 §2.3)

TIMESTAMP: int = _native.LOC_PROP_TIMESTAMP
"""エンコードされたメディアフレームのタイムスタンプ (§2.3.1.1)。"""

TIMESCALE: int = _native.LOC_PROP_TIMESCALE
"""Timestamp の単位 (1 秒あたりのユニット数) (§2.3.1.2)。"""

VIDEO_FRAME_MARKING: int = _native.LOC_PROP_VIDEO_FRAME_MARKING
"""RFC 9626 のビデオフレームフラグ。長さは 1-4 バイト (§2.3.2.2)。"""

AUDIO_LEVEL: int = _native.LOC_PROP_AUDIO_LEVEL
"""RFC 6464 の音声レベル。vi64 の下位 8 bit (§2.3.3.2)。"""

VIDEO_CONFIG: int = _native.LOC_PROP_VIDEO_CONFIG
"""ビデオコーデックの設定 (extradata) (§2.3.2.1)。"""

AUDIO_CONFIG: int = _native.LOC_PROP_AUDIO_CONFIG
"""音声コーデックの設定 (§2.3.3.1)。"""

__all__ = [
    "AUDIO_CONFIG",
    "AUDIO_LEVEL",
    "TIMESCALE",
    "TIMESTAMP",
    "VIDEO_CONFIG",
    "VIDEO_FRAME_MARKING",
    "Properties",
]
