"""`moqt.loc` のプロパティ codec のテスト。"""

import pytest
from moqt import loc
from moqt.loc import Properties

# プロパティ ID の偶奇で値の型が決まる
# (draft-ietf-moq-loc-04 §2.3)。
EVEN_PROPERTY_ID = 0x10
ODD_PROPERTY_ID = 0x11

# Audio Level の上限 (draft-ietf-moq-loc-04 §2.3.3.2)。
AUDIO_LEVEL_MAX = 255

# Video Frame Marking の長さの範囲 (draft-ietf-moq-loc-04 §2.3.2.2)。
VIDEO_FRAME_MARKING_MIN_LENGTH = 1
VIDEO_FRAME_MARKING_MAX_LENGTH = 4


def test_empty_properties_encode_as_a_zero_length_block() -> None:
    """
    プロパティが 1 件も無い場合に Properties Length = 0 を書くことを確認する。

    draft-ietf-moq-transport-21 §11.3.1 (Subgroup Header): "Objects with no
    properties set Properties Length to 0" である。
    """
    assert Properties().encode() == b"\x00"
    assert len(Properties()) == 0


def test_known_properties_round_trip() -> None:
    """
    LOC の既知プロパティが encode と decode で往復することを確認する。

    偶数 ID は vi64、奇数 ID は長さ付きバイト列として表現される。名前付き
    アクセサと `to_dict` の両方から同じ値が読めることを検証する。
    """
    properties = Properties()
    properties.add(loc.TIMESTAMP, 1_234_567)
    properties.add(loc.TIMESCALE, 90000)
    properties.add(loc.VIDEO_FRAME_MARKING, b"\x80")
    properties.add(loc.AUDIO_LEVEL, 42)
    properties.add(loc.VIDEO_CONFIG, b"\x01\x02\x03")
    properties.add(loc.AUDIO_CONFIG, b"\x04\x05")

    encoded = properties.encode()
    decoded, consumed = Properties.decode(encoded)

    assert consumed == len(encoded)
    assert len(decoded) == 6
    assert decoded == properties
    assert decoded.timestamp == 1_234_567
    assert decoded.timescale == 90000
    assert decoded.video_frame_marking == b"\x80"
    assert decoded.audio_level == 42
    assert decoded.video_config == b"\x01\x02\x03"
    assert decoded.audio_config == b"\x04\x05"
    assert decoded.to_dict() == {
        loc.TIMESTAMP: 1_234_567,
        loc.TIMESCALE: 90000,
        loc.VIDEO_FRAME_MARKING: b"\x80",
        loc.AUDIO_LEVEL: 42,
        loc.VIDEO_CONFIG: b"\x01\x02\x03",
        loc.AUDIO_CONFIG: b"\x04\x05",
    }


def test_decode_leaves_the_following_bytes_unconsumed() -> None:
    """
    プロパティブロックの後ろに続くバイト列を消費しないことを確認する。

    オブジェクトのペイロードはプロパティブロックの直後に続くため、decode は
    消費バイト数を返して呼び出し側が続きを読めるようにする。
    """
    properties = Properties()
    properties.add(loc.TIMESTAMP, 1)
    encoded = properties.encode()
    trailing = b"\xde\xad\xbe\xef"

    decoded, consumed = Properties.decode(encoded + trailing)

    assert consumed == len(encoded)
    assert decoded.timestamp == 1


def test_decode_accepts_a_zero_length_block() -> None:
    """
    Properties Length = 0 のブロックを空のプロパティとして読むことを確認する。

    プロパティを持たないオブジェクトはこの 1 バイトだけを運ぶ。
    """
    decoded, consumed = Properties.decode(b"\x00")

    assert consumed == 1
    assert len(decoded) == 0
    assert decoded.timestamp is None


def test_encode_sorts_properties_by_id() -> None:
    """
    追加順にかかわらずプロパティ ID の昇順でエンコードされることを確認する。

    delta encoding は差分が常に正であることを前提とするため、encode は
    ID を昇順に並べ替える (draft-ietf-moq-loc-04 §2.3)。
    """
    ascending = Properties()
    ascending.add(loc.TIMESCALE, 1000)
    ascending.add(loc.TIMESTAMP, 2000)

    descending = Properties()
    descending.add(loc.TIMESTAMP, 2000)
    descending.add(loc.TIMESCALE, 1000)

    assert ascending.encode() == descending.encode()


@pytest.mark.parametrize(
    ("prop_id", "value"),
    [(EVEN_PROPERTY_ID, b"\x01"), (ODD_PROPERTY_ID, 1)],
    ids=["bytes-on-even-id", "varint-on-odd-id"],
)
def test_encode_rejects_a_value_whose_type_does_not_match_the_id(
    prop_id: int,
    value: int | bytes,
) -> None:
    """
    プロパティ ID の偶奇と値の型が食い違う場合に拒否することを確認する。

    偶数 ID は varint、奇数 ID は長さ付きバイト列でなければならない。
    (draft-ietf-moq-loc-04 §2.3)
    """
    properties = Properties()
    properties.add(prop_id, value)

    with pytest.raises(ValueError, match="property ID requires"):
        properties.encode()


def test_encode_rejects_a_duplicate_property_id() -> None:
    """
    同じプロパティ ID を 2 回追加した場合に拒否することを確認する。

    delta encoding は同一 ID の繰り返しを表現できない。
    """
    properties = Properties()
    properties.add(loc.TIMESTAMP, 1)
    properties.add(loc.TIMESTAMP, 2)

    with pytest.raises(ValueError, match="duplicate LOC property ID"):
        properties.encode()


def test_decode_rejects_a_duplicate_property_id() -> None:
    """
    delta が 0 のプロパティが 2 回現れる場合に拒否することを確認する。

    delta 0 は直前と同じプロパティ ID を意味するため、重複として扱う。
    """
    # Properties Length = 4, ID 0x10 の varint 値 1, 続けて delta 0 の varint 値 2
    block = b"\x04\x10\x01\x00\x02"

    with pytest.raises(ValueError, match="duplicate LOC property ID"):
        Properties.decode(block)


def test_encode_rejects_an_audio_level_over_the_8bit_range() -> None:
    """
    Audio Level が 8 bit に収まらない場合に拒否することを確認する。

    draft-ietf-moq-loc-04 §2.3.3.2 の値域は 0x00-0xFF である。
    """
    properties = Properties()
    properties.add(loc.AUDIO_LEVEL, AUDIO_LEVEL_MAX + 1)

    with pytest.raises(ValueError, match="Audio Level value exceeds 8-bit range"):
        properties.encode()


@pytest.mark.parametrize(
    "length",
    [VIDEO_FRAME_MARKING_MIN_LENGTH - 1, VIDEO_FRAME_MARKING_MAX_LENGTH + 1],
    ids=["too-short", "too-long"],
)
def test_encode_rejects_a_video_frame_marking_length_outside_1_to_4(length: int) -> None:
    """
    Video Frame Marking の長さが 1-4 バイトでない場合に拒否することを確認する。

    draft-ietf-moq-loc-04 §2.3.2.2 は "Length: Varies (1-4 bytes)" と定める。
    """
    properties = Properties()
    properties.add(loc.VIDEO_FRAME_MARKING, b"\x00" * length)

    with pytest.raises(ValueError, match="Video Frame Marking length must be 1-4 bytes"):
        properties.encode()


def test_decode_rejects_a_truncated_block() -> None:
    """
    Properties Length が実際のバイト数より大きい場合に拒否することを確認する。

    宣言長が入力に収まらない場合はデコードできない。
    """
    # Properties Length = 8 だが本文が 2 バイトしかない
    with pytest.raises(ValueError, match="unexpected end of buffer"):
        Properties.decode(b"\x08\x10\x01")


def test_add_rejects_a_value_that_is_neither_int_nor_bytes() -> None:
    """
    値に int でも bytes でもないものを渡した場合に拒否することを確認する。

    エラーメッセージには受け取った型が入る。
    """
    properties = Properties()

    with pytest.raises(ValueError, match="requires an int or bytes value, got float"):
        properties.add(loc.TIMESTAMP, 1.5)


def test_unknown_properties_are_preserved() -> None:
    """
    既知の ID ではないプロパティもそのまま往復することを確認する。

    このモジュールは Public / Private の区別も既知 ID の限定もしない
    (draft-ietf-moq-loc-04 §2.2)。
    """
    properties = Properties()
    properties.add(0x22, 7)
    properties.add(0x23, b"\xff")

    decoded, _ = Properties.decode(properties.encode())

    assert decoded.to_dict() == {0x22: 7, 0x23: b"\xff"}
    assert decoded.timestamp is None
