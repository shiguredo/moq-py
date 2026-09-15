"""`moq.loc` のプロパティ codec に対する Property-Based Testing。"""

from hypothesis import given
from hypothesis import strategies as st
from moq import loc
from moq.loc import Properties

# ライブラリが値域や長さを検査しない未知のプロパティ ID を使う。
# 既知 ID の制約 (Audio Level は 8 bit、Video Frame Marking は 1-4 バイト) に
# 引っかからないようにするためである。
UNKNOWN_EVEN_IDS = st.integers(min_value=0x100, max_value=0x1FE).map(lambda value: value & ~1)
UNKNOWN_ODD_IDS = st.integers(min_value=0x101, max_value=0x1FF).map(lambda value: value | 1)

VARINT_VALUES = st.integers(min_value=0, max_value=2**62 - 1)
BYTE_VALUES = st.binary(max_size=64)


@st.composite
def loc_properties(draw: st.DrawFn) -> dict[int, int | bytes]:
    """ID の偶奇と値の型が対応したプロパティの辞書を生成する。"""
    varints = draw(st.dictionaries(UNKNOWN_EVEN_IDS, VARINT_VALUES, max_size=8))
    blobs = draw(st.dictionaries(UNKNOWN_ODD_IDS, BYTE_VALUES, max_size=8))
    return {**varints, **blobs}


@given(values=loc_properties())
def prop_loc_properties_round_trip(values: dict[int, int | bytes]) -> None:
    """
    任意のプロパティ集合が encode と decode で往復することを確認する。

    ID の昇順への並べ替えと delta encoding を挟んでも、ID と値の対応が
    保たれることを検証する。
    """
    properties = Properties()
    for prop_id, value in values.items():
        properties.add(prop_id, value)

    encoded = properties.encode()
    decoded, consumed = Properties.decode(encoded)

    assert consumed == len(encoded)
    assert decoded.to_dict() == values


@given(values=loc_properties(), trailing=st.binary(min_size=1, max_size=16))
def prop_decode_leaves_trailing_bytes(
    values: dict[int, int | bytes],
    trailing: bytes,
) -> None:
    """
    プロパティブロックの後ろのバイト列が消費されないことを確認する。

    オブジェクトのペイロードはプロパティブロックの直後に続くため、decode は
    ブロックちょうどの長さを返さなければならない。
    """
    properties = Properties()
    for prop_id, value in values.items():
        properties.add(prop_id, value)
    encoded = properties.encode()

    decoded, consumed = Properties.decode(encoded + trailing)

    assert consumed == len(encoded)
    assert decoded.to_dict() == values


@given(level=st.integers(min_value=0, max_value=255))
def prop_audio_level_round_trips(level: int) -> None:
    """
    Audio Level が 8 bit の全域で往復することを確認する。

    値域は 0x00-0xFF である (draft-ietf-moq-loc-04 §2.3.3.2)。
    """
    properties = Properties()
    properties.add(loc.AUDIO_LEVEL, level)

    decoded, _ = Properties.decode(properties.encode())

    assert decoded.audio_level == level


@given(marking=st.binary(min_size=1, max_size=4))
def prop_video_frame_marking_round_trips(marking: bytes) -> None:
    """
    Video Frame Marking が 1-4 バイトの全域で往復することを確認する。

    (draft-ietf-moq-loc-04 §2.3.2.2)
    """
    properties = Properties()
    properties.add(loc.VIDEO_FRAME_MARKING, marking)

    decoded, _ = Properties.decode(properties.encode())

    assert decoded.video_frame_marking == marking
