"""`moq.msf` のカタログとタイムラインに対する Property-Based Testing。"""

import json

from hypothesis import given
from hypothesis import strategies as st
from moq.msf import Catalog, EventTimeline, MediaTimeline

# MSF のタイムラインが運ぶ値の範囲。Group ID と Object ID は vi64 の全域を取る。
TIMELINE_FIELDS = st.integers(min_value=0, max_value=2**62 - 1)

# イベントのデータは単一の JSON object でなければならない
# (draft-ietf-moq-msf-01 §8.1 (Event Timeline data format))。
EVENT_DATA = st.dictionaries(
    st.text(min_size=1, max_size=8),
    st.integers() | st.text(max_size=8),
    max_size=4,
)

# カタログのトラック名は空でなく、名前が重複しない必要がある
# (draft-ietf-moq-msf-01 §5.2.3 (Track name))。
TRACK_NAMES = st.lists(st.text(min_size=1, max_size=16), unique=True, max_size=5)


@given(
    entries=st.lists(
        st.tuples(TIMELINE_FIELDS, TIMELINE_FIELDS, TIMELINE_FIELDS, TIMELINE_FIELDS),
        max_size=16,
    ),
    gzip=st.booleans(),
)
def prop_media_timeline_round_trips(
    entries: list[tuple[int, int, int, int]],
    *,
    gzip: bool,
) -> None:
    """
    任意のメディアタイムラインが encode と decode で往復することを確認する。

    gzip 圧縮の有無にかかわらず同じエントリ列が復元されることを検証する。
    (draft-ietf-moq-msf-01 §7.1 (Media Timeline track payload))
    """
    timeline = MediaTimeline()
    for pts_ms, group_id, object_id, wallclock_ms in entries:
        timeline.add(pts_ms, group_id, object_id, wallclock_ms)

    decoded = MediaTimeline.decode(timeline.encode(gzip=gzip))

    assert decoded.entries == entries


@given(
    entries=st.lists(
        st.tuples(
            st.sampled_from(["l", "t", "m"]),
            TIMELINE_FIELDS,
            TIMELINE_FIELDS,
            EVENT_DATA,
        ),
        max_size=8,
    ),
    gzip=st.booleans(),
)
def prop_event_timeline_round_trips(
    entries: list[tuple[str, int, int, dict[str, int | str]]],
    *,
    gzip: bool,
) -> None:
    """
    任意のイベントタイムラインが encode と decode で往復することを確認する。

    インデックスの種別 (`l` / `t` / `m`) と `data` の JSON がそのまま復元される
    ことを検証する。(draft-ietf-moq-msf-01 §8.1 (Event Timeline data format))
    """
    timeline = EventTimeline()
    expected: list[dict[str, object]] = []
    for kind, first, second, data in entries:
        # Python の json.dumps の既定値は BMP 外の文字をサロゲートペアへエスケープする。
        # JSON のパーサはサロゲートペアを結合して 1 文字として読まなければならない
        # (RFC 8259 §7 (String))。
        encoded_data = json.dumps(data)
        if kind == "l":
            timeline.add_location(first, second, encoded_data)
            expected.append({"l": [first, second], "data": data})
        elif kind == "t":
            timeline.add_wallclock(first, encoded_data)
            expected.append({"t": first, "data": data})
        else:
            timeline.add_media_pts(first, encoded_data)
            expected.append({"m": first, "data": data})

    decoded = EventTimeline.decode(timeline.encode(gzip=gzip))

    assert decoded.entries == expected


@given(
    names=TRACK_NAMES,
    # mediatimeline / eventtimeline は他のトラックへの depends と mimeType を
    # 必須とするため (draft-ietf-moq-msf-01 §7.2 / §8.2)、ここでは扱わない。
    packaging=st.sampled_from(["loc", "moqlog", "moqmetrics"]),
    is_live=st.booleans(),
)
def prop_catalog_track_names_round_trip(
    names: list[str],
    *,
    packaging: str,
    is_live: bool,
) -> None:
    """
    任意のトラック名を持つカタログが encode と decode で往復することを確認する。

    JSON のエスケープを挟んでもトラック名と必須フィールドが保たれることを
    検証する。(draft-ietf-moq-msf-01 §5.1.4 (Tracks))
    """
    document = json.dumps(
        {
            "version": "draft-01",
            "tracks": [{"name": name, "packaging": packaging, "isLive": is_live} for name in names],
        },
    ).encode()

    catalog = Catalog.decode(document)
    reencoded = Catalog.decode(catalog.encode())

    assert [track["name"] for track in reencoded.tracks] == names
    assert reencoded == catalog
    assert reencoded.encode() == catalog.encode()
