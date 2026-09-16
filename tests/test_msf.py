"""`moqt.msf` のカタログとタイムラインの codec テスト。"""

import json

import pytest
from moqt import msf
from moqt.msf import (
    Accessibility,
    AuthInfo,
    Buffers,
    Catalog,
    CloneTrack,
    DeltaUpdate,
    EventTimeline,
    InitData,
    MediaTimeline,
    RemoveTrack,
    Template,
    Track,
    Uri,
)

# このライブラリが対応する MSF のバージョン (draft-ietf-moq-msf-01 §5.1.1)。
SUPPORTED_VERSION = "draft-01"

# 最小のカタログ。tracks は必須である (draft-ietf-moq-msf-01 §5.1.4)。
MINIMAL_CATALOG = '{"version":"draft-01","tracks":[]}'

# トラック 1 件を持つカタログ。
ONE_TRACK_CATALOG = (
    '{"version":"draft-01","tracks":[{"name":"video","packaging":"loc","isLive":true}]}'
)

# トラックを 1 件追加する delta 更新。
ADD_AUDIO = (
    '{"deltaUpdate":[{"op":"add","tracks":[{"name":"audio","packaging":"loc","isLive":true}]}]}'
)


def test_msf_version_matches_the_supported_draft() -> None:
    """
    対応する MSF のバージョンが draft-01 であることを確認する。

    カタログの version と一致しない文書は decode が拒否する。
    """
    assert msf.MSF_VERSION == SUPPORTED_VERSION


def test_catalog_track_name_is_catalog() -> None:
    """
    カタログを配信する Track 名が `catalog` であることを確認する。

    (draft-ietf-moq-msf-01 §4.1)
    """
    assert msf.CATALOG_TRACK_NAME == b"catalog"


def test_catalog_round_trips_through_json() -> None:
    """
    カタログが decode と encode で往復することを確認する。

    トラックのフィールド名は draft の表記のままで読める。
    """
    catalog = Catalog.decode(ONE_TRACK_CATALOG.encode())

    assert catalog.version == SUPPORTED_VERSION
    assert catalog.is_complete is False
    assert catalog.generated_at is None
    assert catalog.tracks == [{"name": "video", "packaging": "loc", "isLive": True}]
    assert catalog.encode() == ONE_TRACK_CATALOG.encode()


def test_catalog_reports_optional_members_as_empty() -> None:
    """
    publishTracks と initDataList が省略されたカタログで空列を返すことを確認する。

    draft-ietf-moq-msf-01 §5.1.5 と §5.1.7 は空配列の省略を許すため、
    読み出し側は欠落を空列として扱う必要がある。
    """
    catalog = Catalog.parse(MINIMAL_CATALOG)

    assert catalog.publish_tracks == []
    assert catalog.init_data_list == []


def test_catalog_reads_optional_members() -> None:
    """
    publishTracks と initDataList を持つカタログを読めることを確認する。

    (draft-ietf-moq-msf-01 §5.1.5 (Publish tracks) / §5.1.7 (Initialization Data List))
    """
    catalog = Catalog.parse(
        '{"version":"draft-01","tracks":[],'
        '"publishTracks":[{"name":"p","packaging":"loc","isLive":true}],'
        '"initDataList":[{"id":"init","type":"inline","data":"AAAA"}]}'
    )

    assert catalog.publish_tracks == [{"name": "p", "packaging": "loc", "isLive": True}]
    assert catalog.init_data_list == [{"id": "init", "type": "inline", "data": "AAAA"}]


def test_catalog_accepts_surrogate_pair_escapes() -> None:
    """
    BMP 外の文字をサロゲートペアでエスケープしたカタログを読めることを確認する。

    `json.dumps` の既定値は BMP 外の文字を 12 文字のサロゲートペアへエスケープする。
    JSON のパーサはこれを結合して 1 文字として読まなければならない
    (RFC 8259 §7 (String))。
    """
    name = "\U00010000"
    document = json.dumps(
        {
            "version": SUPPORTED_VERSION,
            "tracks": [{"name": name, "packaging": "loc", "isLive": True}],
        }
    )
    assert "\\ud800\\udc00" in document

    catalog = Catalog.parse(document)

    assert catalog.tracks == [{"name": name, "packaging": "loc", "isLive": True}]


def test_catalog_rejects_an_unsupported_version() -> None:
    """
    対応しない version のカタログを拒否することを確認する。

    (draft-ietf-moq-msf-01 §5.1.1 (MSF version))
    """
    with pytest.raises(ValueError, match="unsupported MSF catalog version: draft-99"):
        Catalog.parse('{"version":"draft-99","tracks":[]}')


def test_catalog_rejects_a_delta_update_document() -> None:
    """
    delta 更新の文書を `Catalog` で読もうとした場合に拒否することを確認する。

    完全カタログと delta 更新は別の型で扱う。
    """
    with pytest.raises(ValueError, match="the document is a delta update"):
        Catalog.parse(ADD_AUDIO)


def test_catalog_apply_delta_adds_a_track() -> None:
    """
    delta 更新を適用してトラックを追加できることを確認する。

    操作は配列順に適用される (draft-ietf-moq-msf-01 §5.1.6 (Delta update))。
    """
    catalog = Catalog.parse(ONE_TRACK_CATALOG)

    catalog.apply_delta(ADD_AUDIO)

    assert [track["name"] for track in catalog.tracks] == ["video", "audio"]


def test_catalog_apply_delta_rejects_a_full_catalog_document() -> None:
    """
    delta 更新を期待する `apply_delta` に完全カタログを渡した場合に拒否することを確認する。
    """
    catalog = Catalog.parse(MINIMAL_CATALOG)

    with pytest.raises(ValueError, match="expected a delta update document"):
        catalog.apply_delta(ONE_TRACK_CATALOG)


def test_catalog_apply_delta_rejects_add_to_a_complete_catalog() -> None:
    """
    isComplete が真のカタログへのトラック追加を拒否することを確認する。

    draft-ietf-moq-msf-01 §5.1.3 (Is Complete): "no new tracks will be added to
    the catalog" である。
    """
    catalog = Catalog.parse('{"version":"draft-01","isComplete":true,"tracks":[]}')

    with pytest.raises(ValueError, match="catalog is complete"):
        catalog.apply_delta(ADD_AUDIO)


def test_catalog_apply_delta_finds_tracks_by_the_catalog_namespace() -> None:
    """
    カタログのネームスペースを省略したトラック参照を解決することを確認する。

    draft-ietf-moq-msf-01 §5.2.2 (Track namespace): トラックが namespace を
    省略した場合はカタログトラック自身のネームスペースを継承する。
    """
    catalog = Catalog.parse(
        '{"version":"draft-01","tracks":'
        '[{"name":"video","namespace":"ns","packaging":"loc","isLive":true}]}'
    )
    remove = '{"deltaUpdate":[{"op":"remove","tracks":[{"name":"video"}]}]}'

    catalog.apply_delta(remove, "ns")

    assert catalog.tracks == []


def test_catalog_apply_delta_reports_a_track_that_cannot_be_resolved() -> None:
    """
    ネームスペースを与えなければ解決できないトラック参照を拒否することを確認する。

    同じカタログでも `namespace` を渡すかどうかで参照の解決結果が変わる。
    """
    catalog = Catalog.parse(
        '{"version":"draft-01","tracks":'
        '[{"name":"video","namespace":"ns","packaging":"loc","isLive":true}]}'
    )
    remove = '{"deltaUpdate":[{"op":"remove","tracks":[{"name":"video"}]}]}'

    with pytest.raises(ValueError, match="delta remove: track 'video' not found"):
        catalog.apply_delta(remove)


def test_catalog_apply_delta_leaves_earlier_operations_applied_on_failure() -> None:
    """
    delta 更新が途中で失敗しても先行する操作が取り消されないことを確認する。

    draft-ietf-moq-msf-01 §5.1.6 (Delta update) の操作は逐次適用され、
    ロールバックは規定されていない。適用前の状態を保ちたい場合の判断材料になる。
    """
    catalog = Catalog.parse(MINIMAL_CATALOG)
    # 1 件目の add は成功し、2 件目の remove は存在しないトラックを指す
    delta = (
        '{"deltaUpdate":['
        '{"op":"add","tracks":[{"name":"video","packaging":"loc","isLive":true}]},'
        '{"op":"remove","tracks":[{"name":"missing"}]}]}'
    )

    with pytest.raises(ValueError, match="delta remove: track 'missing' not found"):
        catalog.apply_delta(delta)

    assert [track["name"] for track in catalog.tracks] == ["video"]


def test_delta_update_reads_its_operations() -> None:
    """
    delta 更新の操作列を draft のフィールド名で読み出せることを確認する。

    操作の種別は `op`、対象のトラックは `tracks` に入る。
    """
    delta = DeltaUpdate.parse(ADD_AUDIO)

    assert delta.operations == {
        "deltaUpdate": [
            {"op": "add", "tracks": [{"name": "audio", "packaging": "loc", "isLive": True}]}
        ]
    }
    assert delta.generated_at is None


def test_delta_update_rejects_a_full_catalog_document() -> None:
    """
    完全カタログの文書を `DeltaUpdate` で読もうとした場合に拒否することを確認する。
    """
    with pytest.raises(ValueError, match="the document is a full catalog"):
        DeltaUpdate.parse(MINIMAL_CATALOG)


def test_delta_update_rejects_an_empty_operation_list() -> None:
    """
    操作が 1 件も無い delta 更新を拒否することを確認する。

    (draft-ietf-moq-msf-01 §5.1.6 (Delta update))
    """
    with pytest.raises(ValueError, match="delta update MUST contain at least one operation"):
        DeltaUpdate.parse('{"deltaUpdate":[]}')


def test_catalog_builds_tracks_without_json() -> None:
    """
    JSON 文字列を経由せずにトラックを組み立ててカタログを encode できることを確認する。

    draft は name / packaging / isLive を必須とし、残りを任意のフィールドとして定義する
    (draft-ietf-moq-msf-01 §5.2 (Track Object Fields))。
    """
    catalog = Catalog()
    catalog.generated_at = 1234
    track = Track("video", "loc", True)
    track.role = "video"
    track.codec = "av01"
    track.bitrate = 1_000_000
    catalog.add_track(track)

    # encode した JSON を読み直し、draft のフィールド名で値が入っていることを確認する
    encoded = Catalog.decode(catalog.encode())

    assert encoded.generated_at == 1234
    assert encoded.is_complete is False
    assert encoded.tracks == [
        {
            "name": "video",
            "packaging": "loc",
            "isLive": True,
            "role": "video",
            "codec": "av01",
            "bitrate": 1_000_000,
        }
    ]


def test_catalog_builds_a_media_timeline_track() -> None:
    """
    depends と mimeType を持つ mediatimeline トラックを組み立てられることを確認する。

    draft-ietf-moq-msf-01 §7.2 (Media Timeline Catalog requirements) は mediatimeline の
    トラックに depends と mimeType "application/json" を要求する。テンプレートは
    §5.2.15 (Template) の 6 要素の JSON 配列として書き出される。
    """
    catalog = Catalog()
    track = Track("timeline", "mediatimeline", True)
    track.depends = ["video"]
    track.mime_type = "application/json"
    track.template = Template(1000, 33, 1, 0, 1, 2, 5000, 33)
    catalog.add_track(track)

    encoded = Catalog.decode(catalog.encode())

    assert encoded.tracks == [
        {
            "name": "timeline",
            "packaging": "mediatimeline",
            "isLive": True,
            "depends": ["video"],
            "template": [1000, 33, [1, 0], [1, 2], 5000, 33],
            "mimeType": "application/json",
        }
    ]


def test_catalog_builds_a_track_with_buffers_and_init_data() -> None:
    """
    バッファと初期化データを持つトラックを組み立てられることを確認する。

    initRef は initDataList の id を指さなければならない
    (draft-ietf-moq-msf-01 §5.2.13 (Initialization reference))。
    """
    catalog = Catalog()
    track = Track("video", "loc", True)
    track.buffers = Buffers(target=100, min=50, max=200)
    track.init_ref = "init"
    catalog.add_track(track)
    catalog.add_init_data(InitData("init", "AAAA"))

    encoded = Catalog.decode(catalog.encode())

    assert encoded.tracks == [
        {
            "name": "video",
            "packaging": "loc",
            "isLive": True,
            "buffers": {"target": 100, "min": 50, "max": 200},
            "initRef": "init",
        }
    ]
    assert encoded.init_data_list == [{"id": "init", "type": "inline", "data": "AAAA"}]


def test_catalog_builds_a_track_with_auth_info_and_accessibility() -> None:
    """
    認可情報と accessibility 記述子を持つトラックを組み立てられることを確認する。

    authInfo の値は scheme 固有の JSON 値であり、そのまま書き出される
    (draft-ietf-moq-msf-01 §5.2.42 (Authorization Info) / §5.2.44 (Accessibility))。
    """
    catalog = Catalog()
    track = Track("video", "loc", True)
    track.auth_info = [AuthInfo("bearer", b'{"token":"abc"}')]
    track.accessibility = [Accessibility("urn:example:scheme", "value")]
    catalog.add_track(track)

    encoded = Catalog.decode(catalog.encode())

    assert encoded.tracks[0]["authInfo"] == {"bearer": {"token": "abc"}}
    assert encoded.tracks[0]["accessibility"] == [
        {"scheme": "urn:example:scheme", "value": "value"}
    ]


def test_catalog_builds_a_publish_track() -> None:
    """
    publishTracks へトラックを追加できることを確認する。

    (draft-ietf-moq-msf-01 §5.1.5 (Publish tracks))
    """
    catalog = Catalog()
    catalog.add_publish_track(Track("publish", "loc", True))

    encoded = Catalog.decode(catalog.encode())

    assert encoded.tracks == []
    assert encoded.publish_tracks == [{"name": "publish", "packaging": "loc", "isLive": True}]


def test_catalog_rejects_an_unknown_packaging() -> None:
    """
    draft が定めない packaging を持つトラックの追加を拒否することを確認する。

    draft-ietf-moq-msf-01 §5.2.4 (Packaging) Table 4 が許容値を定める。
    """
    catalog = Catalog()

    with pytest.raises(ValueError, match="unknown MSF packaging 'bogus'"):
        catalog.add_track(Track("video", "bogus", True))


def test_catalog_rejects_event_type_outside_an_event_timeline() -> None:
    """
    packaging が eventtimeline でないトラックの eventType を encode 時に拒否することを確認する。

    draft-ietf-moq-msf-01 §5.2.5 (Event timeline type): "This field MUST NOT be used
    if the packaging value is not \"eventtimeline\"."
    """
    catalog = Catalog()
    track = Track("video", "loc", True)
    track.event_type = "com.example.event"
    catalog.add_track(track)

    with pytest.raises(ValueError, match="eventType MUST NOT be used"):
        catalog.encode()


def test_catalog_rejects_target_latency_and_buffers_together() -> None:
    """
    targetLatency と buffers を同時に持つトラックを encode 時に拒否することを確認する。

    draft-ietf-moq-msf-01 §5.2.8 (Target latency) / §5.2.9 (Buffers) はどちらか一方
    だけを許す。
    """
    catalog = Catalog()
    track = Track("video", "loc", True)
    track.target_latency = 100
    track.buffers = Buffers(target=100)
    catalog.add_track(track)

    with pytest.raises(ValueError, match="targetLatency and buffers MUST NOT be present together"):
        catalog.encode()


def test_catalog_rejects_a_track_duration_on_a_live_track() -> None:
    """
    ライブトラックの trackDuration を encode 時に拒否することを確認する。

    (draft-ietf-moq-msf-01 §5.2.35 (Track duration))
    """
    catalog = Catalog()
    track = Track("video", "loc", True)
    track.track_duration = 10_000
    catalog.add_track(track)

    with pytest.raises(ValueError, match="trackDuration MUST NOT be included"):
        catalog.encode()


def test_catalog_rejects_a_media_timeline_track_without_depends() -> None:
    """
    depends を持たない mediatimeline トラックを encode 時に拒否することを確認する。

    (draft-ietf-moq-msf-01 §7.2 (Media Timeline Catalog requirements))
    """
    catalog = Catalog()
    catalog.add_track(Track("timeline", "mediatimeline", True))

    with pytest.raises(ValueError, match="mediatimeline track MUST have a 'depends' attribute"):
        catalog.encode()


def test_catalog_rejects_a_duplicated_track_name() -> None:
    """
    同じネームスペースで名前が重複するトラックを encode 時に拒否することを確認する。

    draft-ietf-moq-msf-01 §5.2.3 (Track name): "Within the catalog, track names MUST
    be unique per namespace."
    """
    catalog = Catalog()
    catalog.add_track(Track("video", "loc", True))
    catalog.add_track(Track("video", "loc", True))

    with pytest.raises(ValueError, match="duplicate track name 'video'"):
        catalog.encode()


def test_catalog_rejects_an_init_ref_without_init_data() -> None:
    """
    initDataList に無い id を指す initRef を encode 時に拒否することを確認する。

    (draft-ietf-moq-msf-01 §5.2.13 (Initialization reference))
    """
    catalog = Catalog()
    track = Track("video", "loc", True)
    track.init_ref = "missing"
    catalog.add_track(track)

    with pytest.raises(ValueError, match="initRef 'missing' does not match any initDataList id"):
        catalog.encode()


def test_catalog_rejects_an_auth_info_value_that_is_not_a_json_value() -> None:
    """
    JSON として解釈できない authInfo の値を encode 時に拒否することを確認する。

    値は scheme 固有の単独の JSON 値でなければならない
    (draft-ietf-moq-msf-01 §5.2.42 (Authorization Info))。
    """
    invalid_json = Catalog()
    track = Track("video", "loc", True)
    track.auth_info = [AuthInfo("bearer", b"not json")]
    invalid_json.add_track(track)

    with pytest.raises(ValueError, match="JSON parse error"):
        invalid_json.encode()

    # UTF-8 でない値も JSON 値にはなり得ない
    invalid_utf8 = Catalog()
    track = Track("video", "loc", True)
    track.auth_info = [AuthInfo("bearer", b"\xff")]
    invalid_utf8.add_track(track)

    with pytest.raises(ValueError, match="authInfo value is not valid UTF-8"):
        invalid_utf8.encode()


def test_template_resolves_entries() -> None:
    """
    テンプレートから n 番目のエントリを計算できることを確認する。

    式は `resolve_timeline_template` と同じである (draft-ietf-moq-msf-01 §7.4.1)。
    """
    template = Template(1000, 33, 1, 0, 1, 2, 5000, 33)

    assert template.resolve_entry(0) == (1000, 1, 0, 5000)
    assert template.resolve_entry(3) == (1099, 4, 6, 5099)
    # overflow する計算は `None` になる
    overflow = Template(2**64 - 1, 2**64 - 1, 0, 0, 0, 0, 0, 0)
    assert overflow.resolve_entry(2) is None


def test_delta_update_builds_operations_without_json() -> None:
    """
    JSON 文字列を経由せずに delta 更新の操作列を組み立てて encode できることを確認する。

    操作は追加した順に配列へ並ぶ (draft-ietf-moq-msf-01 §5.1.6 (Delta update))。
    """
    delta = DeltaUpdate()
    delta.generated_at = 99
    delta.add_tracks([Track("audio", "loc", True)])
    delta.remove_tracks([RemoveTrack("video")])
    delta.clone_tracks([CloneTrack("video-low", "video")])

    encoded = DeltaUpdate.decode(delta.encode())

    assert encoded.generated_at == 99
    assert encoded.operations == {
        "deltaUpdate": [
            {"op": "add", "tracks": [{"name": "audio", "packaging": "loc", "isLive": True}]},
            {"op": "remove", "tracks": [{"name": "video"}]},
            {"op": "clone", "tracks": [{"name": "video-low", "parentName": "video"}]},
        ],
        "generatedAt": 99,
    }


def test_delta_update_adds_removes_and_clones_tracks_in_a_catalog() -> None:
    """
    組み立てた delta 更新をカタログへ適用できることを確認する。

    操作は配列順に適用されるため、複製は親トラックが残っているうちに行う
    (draft-ietf-moq-msf-01 §5.1.6 (Delta update))。複製したトラックは親の属性を継承する。
    """
    catalog = Catalog()
    catalog.add_track(Track("video", "loc", True))
    catalog.add_track(Track("audio", "loc", True))

    delta = DeltaUpdate()
    delta.clone_tracks([CloneTrack("video-low", "video")])
    delta.remove_tracks([RemoveTrack("audio")])
    delta.add_tracks([Track("text", "loc", True)])

    catalog.apply_delta_update(delta)

    assert [track["name"] for track in catalog.tracks] == ["video", "video-low", "text"]
    assert catalog.tracks[1] == {"name": "video-low", "packaging": "loc", "isLive": True}
    # 適用後のカタログも encode でき、同じ内容が読み直せる
    assert Catalog.decode(catalog.encode()).tracks == catalog.tracks


def test_delta_update_clone_overrides_an_inherited_attribute() -> None:
    """
    複製で再定義した属性が親の値を上書きすることを確認する。

    draft-ietf-moq-msf-01 §5.1.6 (Delta update): "Attributes redefined in the track
    object override inherited values."
    """
    catalog = Catalog()
    parent = Track("video", "loc", True)
    parent.label = "main"
    catalog.add_track(parent)

    clone = CloneTrack("video-low", "video")
    clone.label = "low"
    delta = DeltaUpdate()
    delta.clone_tracks([clone])

    catalog.apply_delta_update(delta)

    assert catalog.tracks[1]["label"] == "low"
    assert catalog.tracks[0]["label"] == "main"


def test_delta_update_finds_a_track_by_the_catalog_namespace() -> None:
    """
    カタログのネームスペースを省略した参照を namespace で解決することを確認する。

    draft-ietf-moq-msf-01 §5.2.2 (Track namespace): トラックが namespace を省略した
    場合はカタログトラック自身のネームスペースを継承する。
    """
    catalog = Catalog()
    track = Track("video", "loc", True)
    track.namespace = "ns"
    catalog.add_track(track)

    delta = DeltaUpdate()
    delta.remove_tracks([RemoveTrack("video")])

    catalog.apply_delta_update(delta, "ns")

    assert catalog.tracks == []


def test_delta_update_rejects_an_empty_operation_list_on_encode() -> None:
    """
    操作を 1 つも追加していない delta 更新の encode を拒否することを確認する。

    (draft-ietf-moq-msf-01 §5.3 (Delta updates))
    """
    with pytest.raises(ValueError, match="delta update MUST contain at least one operation"):
        DeltaUpdate().encode()


def test_delta_update_rejects_an_unknown_packaging() -> None:
    """
    draft が定めない packaging を持つトラックの追加を拒否することを確認する。

    (draft-ietf-moq-msf-01 §5.2.4 (Packaging) Table 4)
    """
    delta = DeltaUpdate()

    with pytest.raises(ValueError, match="unknown MSF packaging 'bogus'"):
        delta.add_tracks([Track("audio", "bogus", True)])


def test_delta_update_rejects_a_clone_of_a_missing_parent() -> None:
    """
    存在しない親トラックを指す複製の適用を拒否することを確認する。

    (draft-ietf-moq-msf-01 §5.1.6 (Delta update))
    """
    catalog = Catalog()
    delta = DeltaUpdate()
    delta.clone_tracks([CloneTrack("video-low", "missing")])

    with pytest.raises(ValueError, match="delta clone: parent track 'missing' not found"):
        catalog.apply_delta_update(delta)


def test_delta_update_rejects_an_add_to_a_complete_catalog() -> None:
    """
    isComplete が真のカタログへのトラック追加を拒否することを確認する。

    draft-ietf-moq-msf-01 §5.1.3 (Is Complete): "no new tracks will be added to
    the catalog"
    """
    catalog = Catalog()
    catalog.is_complete = True
    delta = DeltaUpdate()
    delta.add_tracks([Track("audio", "loc", True)])

    with pytest.raises(ValueError, match="catalog is complete"):
        catalog.apply_delta_update(delta)


def test_media_timeline_round_trips() -> None:
    """
    メディアタイムラインが encode と decode で往復することを確認する。

    フォーマットは `[[pts_ms, [group_id, object_id], wallclock_ms], ...]` である。
    (draft-ietf-moq-msf-01 §7.1 (Media Timeline track payload))
    """
    timeline = MediaTimeline()
    timeline.add(1000, 1, 2, 0)
    timeline.add(1040, 1, 3, 1234)

    encoded = timeline.encode()
    decoded = MediaTimeline.decode(encoded)

    assert encoded == b"[[1000,[1,2],0],[1040,[1,3],1234]]"
    assert decoded.entries == [(1000, 1, 2, 0), (1040, 1, 3, 1234)]
    assert len(decoded) == 2
    assert decoded == timeline


def test_media_timeline_round_trips_through_gzip() -> None:
    """
    gzip で圧縮したメディアタイムラインを decode が自動展開することを確認する。

    (draft-ietf-moq-msf-01 §7.1 (Media Timeline track payload))
    """
    timeline = MediaTimeline()
    timeline.add(1000, 1, 2, 0)

    compressed = timeline.encode(gzip=True)

    # gzip の magic number で圧縮されていることを確認する (RFC 1952 §2.3.1)。
    assert compressed[:2] == b"\x1f\x8b"
    assert MediaTimeline.decode(compressed).entries == [(1000, 1, 2, 0)]


def test_media_timeline_rejects_a_malformed_document() -> None:
    """
    メディアタイムラインとして不正な JSON を拒否することを確認する。

    エントリは 3 要素の配列でなければならない。
    """
    with pytest.raises(ValueError, match="invalid MSF catalog"):
        MediaTimeline.decode(b"[[1000]]")


def test_event_timeline_round_trips() -> None:
    """
    イベントタイムラインが encode と decode で往復することを確認する。

    エントリは `l` (Location) / `t` (wallclock ms) / `m` (media PTS ms) の
    いずれか 1 つと `data` を持つ。
    (draft-ietf-moq-msf-01 §8.1 (Event Timeline data format))
    """
    timeline = EventTimeline()
    timeline.add_location(1, 2, '{"kind":"a"}')
    timeline.add_wallclock(99, '{"kind":"b"}')
    timeline.add_media_pts(5, '{"kind":"c"}')

    encoded = timeline.encode()
    decoded = EventTimeline.decode(encoded)

    assert decoded.entries == [
        {"l": [1, 2], "data": {"kind": "a"}},
        {"t": 99, "data": {"kind": "b"}},
        {"m": 5, "data": {"kind": "c"}},
    ]
    assert len(decoded) == 3
    assert decoded == timeline


def test_event_timeline_round_trips_through_gzip() -> None:
    """
    gzip で圧縮したイベントタイムラインを decode が自動展開することを確認する。
    """
    timeline = EventTimeline()
    timeline.add_wallclock(99, '{"kind":"b"}')

    compressed = timeline.encode(gzip=True)

    assert compressed[:2] == b"\x1f\x8b"
    assert EventTimeline.decode(compressed).entries == [{"t": 99, "data": {"kind": "b"}}]


def test_event_timeline_rejects_data_that_is_not_a_json_object() -> None:
    """
    エントリの `data` が JSON object でない場合に拒否することを確認する。

    (draft-ietf-moq-msf-01 §8.1 (Event Timeline data format))
    """
    timeline = EventTimeline()
    timeline.add_location(1, 2, "null")

    with pytest.raises(ValueError, match="event data MUST be a JSON object"):
        timeline.encode()


def test_uri_splits_the_track_identifier() -> None:
    """
    MSF URI の fragment からネームスペースと Track 名を取り出すことを確認する。

    ネームスペースの要素は `-`、Track 名は `--` で区切る。
    (draft-ietf-moq-msf-01 §11.1.2 (MSF Namespace-Name String Encoding))
    """
    uri = Uri.parse("moqt://example.com/live#msf:room-1--video")

    assert uri.authority == "example.com"
    assert uri.path == "/live"
    assert uri.query is None
    assert uri.namespace == [b"room", b"1"]
    assert uri.track_name == b"video"


def test_uri_keeps_the_query_separate_from_the_fragment() -> None:
    """
    `?` 以降の query が fragment と混ざらないことを確認する。

    (draft-ietf-moq-msf-01 §11.1 (URL construction and interpretation))
    """
    uri = Uri.parse("moqt://example.com/live?token=abc#msf:ns--video")

    assert uri.query == "token=abc"
    assert uri.namespace == [b"ns"]
    assert uri.track_name == b"video"


def test_uri_reads_reserved_fragment_parameters() -> None:
    """
    fragment の予約パラメータを型付きで取り出せることを確認する。

    draft-ietf-moq-msf-01 §11.1.1 (Reserved fragment parameters) の
    connection / wallclock-range / mediatime-range / location-range を検証する。
    """
    uri = Uri.parse(
        "moqt://example.com/live#msf:ns--video"
        "&connection=wt&connection=q"
        "&wallclock-range=10-20&mediatime-range=30-&location-range=1.2-3"
    )

    assert uri.parameters[0] == ("connection", "wt")
    assert uri.connection_types() == ["webtransport", "quic"]
    assert uri.wallclock_ranges() == [(10, 20)]
    # 終端を省略した range は open range として `None` になる。
    assert uri.mediatime_ranges() == [(30, None)]
    assert uri.location_ranges() == [
        {"start_group_id": 1, "start_object_id": 2, "end_group_id": 3, "end_object_id": None}
    ]
    assert uri.parameter_values("connection") == ["wt", "q"]


def test_uri_rejects_a_uri_without_a_fragment() -> None:
    """
    fragment を持たない URI を拒否することを確認する。

    `#` 以降が無いと Track を特定できない。
    """
    with pytest.raises(ValueError, match="MSF URI is missing a fragment"):
        Uri.parse("moqt://example.com/live")


def test_uri_rejects_a_scheme_other_than_moqt() -> None:
    """
    `moqt://` 以外の scheme を拒否することを確認する。

    (draft-ietf-moq-msf-01 §11.1 (URL construction and interpretation))
    """
    with pytest.raises(ValueError, match="MSF URI must use the 'moqt://' scheme"):
        Uri.parse("https://example.com/live#msf:ns--video")


def test_uri_rejects_a_fragment_without_the_msf_prefix() -> None:
    """
    fragment が `msf:` で始まらない場合に拒否することを確認する。
    """
    with pytest.raises(ValueError, match="MSF fragment must start with 'msf:'"):
        Uri.parse("moqt://example.com/live#ns--video")


def test_uri_rejects_a_track_identifier_without_a_separator() -> None:
    """
    Track 名の区切り (`--`) が無い識別子を拒否することを確認する。

    区切りが無いとネームスペースと Track 名を分けられない。
    """
    with pytest.raises(ValueError, match="MissingSeparator"):
        Uri.parse("moqt://example.com/live#msf:video")


def test_parse_fragment_pairs_keeps_the_order_and_duplicates() -> None:
    """
    fragment を `&` 区切りのパラメータ列として分解することを確認する。

    同名のパラメータは出現順に並ぶ。
    (draft-ietf-moq-msf-01 §11.1.1 (Reserved fragment parameters))
    """
    assert msf.parse_fragment_pairs("a=1&b=2&a=3") == [("a", "1"), ("b", "2"), ("a", "3")]


def test_resolve_catalog_variables_substitutes_fragment_values() -> None:
    """
    カタログ中の変数参照を fragment のパラメータで置き換えることを確認する。

    (draft-ietf-moq-msf-01 §5.4 (Catalog variables))
    """
    document = (
        b'{"version":"draft-01","tracks":[{"name":"%name%","packaging":"loc","isLive":true}]}'
    )

    resolved = msf.resolve_catalog_variables(document, "msf:ns--catalog&name=video")

    assert b'"name":"video"' in resolved
    assert Catalog.decode(resolved).tracks == [
        {"name": "video", "packaging": "loc", "isLive": True}
    ]


def test_resolve_timeline_template_computes_entries() -> None:
    """
    media timeline template から n 番目のエントリを計算することを確認する。

    式は `media_time = start_media_time + delta_media_time * n`、
    `group_id = start_group_id + delta_group_id * n`、
    `object_id = start_object_id + delta_object_id * n`、
    `wallclock = start_wallclock + delta_wallclock * n` である
    (draft-ietf-moq-msf-01 §7.4.1)。
    """
    template = [1000, 33, [1, 0], [1, 2], 5000, 33]

    assert msf.resolve_timeline_template(template, 0) == (1000, 1, 0, 5000)
    assert msf.resolve_timeline_template(template, 3) == (1099, 4, 6, 5099)


def test_resolve_timeline_template_rejects_a_short_array() -> None:
    """要素数が 6 でない template を拒否することを確認する。"""
    with pytest.raises(ValueError, match="6 elements"):
        msf.resolve_timeline_template([1000, 33], 0)


def test_resolve_timeline_template_reports_overflow_as_none() -> None:
    """計算が overflow する場合は `None` を返すことを確認する。"""
    template = [2**64 - 1, 2**64 - 1, [0, 0], [0, 0], 0, 0]

    assert msf.resolve_timeline_template(template, 2) is None


def test_parse_msf_fragment_splits_the_identifier_and_parameters() -> None:
    """
    `msf:` prefix 付きの fragment を namespace と Track 名とパラメータへ分解する。

    (draft-ietf-moq-msf-01 §11.1 (URL construction and interpretation))
    """
    namespace, track_name, parameters = msf.parse_msf_fragment("msf:room-1--video&x=1&y=2")

    assert namespace == [b"room", b"1"]
    assert track_name == b"video"
    assert parameters == [("x", "1"), ("y", "2")]


def test_parse_msf_fragment_rejects_a_missing_prefix() -> None:
    """`msf:` prefix の無い入力を拒否することを確認する。"""
    with pytest.raises(ValueError, match="msf:"):
        msf.parse_msf_fragment("room-1--video")


def test_parse_name_and_serialize_name_round_trip() -> None:
    """
    Track 識別子と namespace + Track 名が相互変換できることを確認する。

    namespace の区切りは `-`、namespace と Track 名の境界は `--` である
    (draft-ietf-moq-transport-21 §8.8 (Representing Namespace and Track Names))。
    """
    namespace, track_name = msf.parse_name("room-1--video")

    assert namespace == [b"room", b"1"]
    assert track_name == b"video"
    assert msf.serialize_name(namespace, track_name) == "room-1--video"


def test_serialize_name_escapes_a_dot() -> None:
    """リテラルでないバイトを `.` と 16 進 2 桁でエスケープすることを確認する。

    (draft-ietf-moq-transport-21 §8.8 (Representing Namespace and Track Names))
    """
    assert msf.serialize_name([b"room.1"], b"video") == "room.2e1--video"
    assert msf.parse_name("room.2e1--video") == ([b"room.1"], b"video")


def test_parse_name_rejects_a_triple_hyphen() -> None:
    """境界が 2 連続でない入力を拒否することを確認する。"""
    with pytest.raises(ValueError, match="invalid Track name"):
        msf.parse_name("room---video")
