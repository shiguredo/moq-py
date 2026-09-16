"""
Python から `import moqt._native` される拡張モジュール。
"""

from _typeshed import Incomplete
from collections.abc import Sequence
from typing import Any, final

@final
class Catalog:
    """
    MSF カタログ。

    draft-ietf-moq-msf-01 §5 (Catalog) の完全カタログである。delta 更新は
    [`DeltaUpdate`] で読み込み、[`Catalog::apply_delta`] で適用する。
    """
    def __eq__(self, other: object, /) -> bool: ...
    def __new__(cls, /) -> Catalog:
        """
        空のカタログを作成する。

        version は対応する MSF バージョン、tracks / publishTracks / initDataList は
        空になる。
        """
    def __repr__(self, /) -> str: ...
    def apply_delta(self, /, text: str, namespace: str |None = None) -> None:
        """
        delta 更新をこのカタログへ適用する。

        `namespace` はカタログトラック自身のネームスペースであり、トラックが
        namespace を省略した場合の継承先として使う (draft-ietf-moq-msf-01 §5.2.2)。

        操作は配列順に適用される。途中で失敗した場合、それまでの操作は取り消されない
        (draft-ietf-moq-msf-01 §5.1.6)。差し替え前の状態を保ちたい場合は、適用前に
        呼び出し側でカタログを複製すること。
        """
    @staticmethod
    def decode(data: bytes) -> Catalog:
        """
        JSON バイト列からカタログを読み込む。

        draft の MUST に違反する文書は `ValueError` になる。delta 更新の文書を
        渡した場合は [`DeltaUpdate`] を使うよう促す `ValueError` になる。
        """
    def encode(self, /) -> bytes:
        """
        カタログを JSON バイト列へ書き出す。

        書き出す前に draft の MUST を検証する。手組みの不正な値は `ValueError` になる。
        """
    @property
    def generated_at(self, /) -> int |None:
        """
        カタログ生成時刻 (ms) (draft-ietf-moq-msf-01 §5.1.2)。
        """
    @property
    def init_data_list(self, /) -> Any:
        """
        初期化データ一覧 (draft-ietf-moq-msf-01 §5.1.7)。
        """
    @property
    def is_complete(self, /) -> bool:
        """
        ブロードキャストが完了しているか (draft-ietf-moq-msf-01 §5.1.3)。
        """
    @staticmethod
    def parse(text: str) -> Catalog:
        """
        JSON 文字列からカタログを読み込む。
        """
    @property
    def publish_tracks(self, /) -> Any:
        """
        publish track 一覧 (draft-ietf-moq-msf-01 §5.1.5)。
        """
    @property
    def tracks(self, /) -> Any:
        """
        トラック一覧 (draft-ietf-moq-msf-01 §5.1.4)。

        draft のフィールド名を持つ辞書のリストとして返す。
        """
    @property
    def version(self, /) -> str:
        """
        MSF バージョン (draft-ietf-moq-msf-01 §5.1.1)。
        """

@final
class DeltaUpdate:
    """
    MSF の delta 更新。

    draft-ietf-moq-msf-01 §5.1.6 (Delta update) の文書である。
    """
    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self, /) -> str: ...
    @staticmethod
    def decode(data: bytes) -> DeltaUpdate:
        """
        JSON バイト列から delta 更新を読み込む。

        完全カタログの文書を渡した場合は [`Catalog`] を使うよう促す `ValueError` になる。
        """
    def encode(self, /) -> bytes:
        """
        delta 更新を JSON バイト列へ書き出す。
        """
    @property
    def generated_at(self, /) -> int |None:
        """
        カタログ生成時刻 (ms) (draft-ietf-moq-msf-01 §5.1.6)。
        """
    @property
    def operations(self, /) -> Any:
        """
        操作列 (draft-ietf-moq-msf-01 §5.1.6)。

        draft のフィールド名を持つ辞書のリストとして返す。操作は配列順に適用される。
        """
    @staticmethod
    def parse(text: str) -> DeltaUpdate:
        """
        JSON 文字列から delta 更新を読み込む。
        """

@final
class Event:
    """
    sans I/O セッション状態機械が返すイベント。

    種別ごとに意味を持つ属性だけが入る。どの属性が有効かは `kind` で決まる。
    """
    def __repr__(self, /) -> str: ...
    @property
    def acceptance(self, /) -> str |None:
        """
        オブジェクトの受理結果 (object イベントのみ)。

        `accepted` / `unknown_track_alias` / `discarded` / `filtered_out` のいずれかである。
        """
    @property
    def code(self, /) -> int |None:
        """
        セッション終了コード、またはストリームのエラーコード。
        """
    @property
    def data(self, /) -> bytes |None:
        """
        制御ストリームへ書き込むバイト列 (send_control のみ)。
        """
    @property
    def fin(self, /) -> bool |None:
        """
        メッセージ送信後にストリームを FIN するか。
        """
    @property
    def group_id(self, /) -> int |None:
        """
        受信した data stream の Group ID (object イベントのみ)。
        """
    @property
    def kind(self, /) -> str:
        """
        イベント種別。
        """
    @property
    def message(self, /) -> dict |None:
        """
        送信すべきメッセージ本体。
        """
    @property
    def message_data(self, /) -> bytes |None:
        """
        メッセージの生バイト列 (Type + Length + Message Body)。

        受信系イベントでは、そのまま peer へ中継できる形のバイト列になる。
        """
    @property
    def object_id(self, /) -> int |None:
        """
        受信したオブジェクトの Object ID (object イベントのみ)。
        """
    @property
    def parameters(self, /) -> dict |None:
        """
        受信したメッセージのパラメータ。
        """
    @property
    def properties(self, /) -> bytes |None:
        """
        受信したオブジェクトの Properties の生バイト (object イベントのみ)。

        `Properties Length (varint) | Properties データ` の形であり、データグラムと
        subgroup のどちらでも同じである。`ObjectProperties.decode` で解釈する。
        """
    @property
    def publisher_priority(self, /) -> int |None:
        """
        受信したデータストリームの Publisher Priority (object イベントのみ)。

        `None` は DEFAULT_PRIORITY bit が立ち、購読の優先度を継承することを示す。
        """
    @property
    def reason(self, /) -> str |None:
        """
        セッション終了理由 (close のみ)。
        """
    @property
    def reliable_size(self, /) -> int |None:
        """
        RESET_STREAM の reliable size。
        """
    @property
    def request_id(self, /) -> int |None:
        """
        対象 request の Request ID。
        """
    @property
    def status(self, /) -> int |None:
        """
        受信したオブジェクトの Object Status (object イベントのみ)。

        ペイロード長 0 のオブジェクトだけが持ち、非 0 長では `None` になる。
        (draft-ietf-moq-transport-21 §11.1.2 (Object Status))
        """
    @property
    def stream_id(self, /) -> int |None:
        """
        対象データストリームの ID。
        """
    @property
    def subgroup_id(self, /) -> int |None:
        """
        受信したオブジェクトを含む subgroup の Subgroup ID (object イベントのみ)。

        ヘッダが Subgroup ID を最初の Object ID として決めるモードでは `None` に
        なる (draft-ietf-moq-transport-21 §11.3.1 (Subgroup Header))。
        """
    @property
    def track_alias(self, /) -> int |None:
        """
        受信した data stream の Track Alias (object イベントのみ)。

        data stream は Request ID ではなく Track Alias で購読を特定するため、
        購読との対応付けに使う。
        """

@final
class EventTimeline:
    """
    MSF イベントタイムライン (draft-ietf-moq-msf-01 §8.1)。

    フォーマットは `[{"t"/"l"/"m": ..., "data": ...}, ...]` である。
    """
    def __eq__(self, other: object, /) -> bool: ...
    def __len__(self, /) -> int: ...
    def __new__(cls, /) -> EventTimeline:
        """
        空のイベントタイムラインを作成する。
        """
    def __repr__(self, /) -> str: ...
    def add_location(self, /, group_id: int, object_id: int, data: str) -> None:
        """
        MOQT Location を指すエントリを追加する ('l')。
        """
    def add_media_pts(self, /, pts_ms: int, data: str) -> None:
        """
        メディア PTS (ms) を指すエントリを追加する ('m')。
        """
    def add_wallclock(self, /, wallclock_ms: int, data: str) -> None:
        """
        ウォールクロック (ms) を指すエントリを追加する ('t')。
        """
    @staticmethod
    def decode(data: bytes) -> EventTimeline:
        """
        JSON バイト列からイベントタイムラインを読み込む。

        gzip で圧縮された入力は自動的に展開する。
        """
    def encode(self, /, gzip: bool = False) -> bytes:
        """
        イベントタイムラインを JSON バイト列へ書き出す。

        各エントリの `data` は単一の JSON object でなければならない。そうでない
        場合は `ValueError` になる。`gzip` が真の場合は gzip で圧縮する。
        """
    @property
    def entries(self, /) -> Any:
        """
        エントリ列を draft のフィールド名を持つ辞書のリストとして返す。
        """

@final
class LocProperties:
    """
    LOC プロパティの集合。

    ワイヤフォーマットは `Properties Length (vi64) | Key-Value-Pairs...` である。
    encode は prop_id の昇順にソートし、delta encoding で ID を圧縮する。
    (draft-ietf-moq-loc-04 §2.3)
    """
    def __eq__(self, other: object, /) -> bool: ...
    def __len__(self, /) -> int: ...
    def __new__(cls, /) -> LocProperties:
        """
        空のプロパティ集合を作成する。
        """
    def __repr__(self, /) -> str: ...
    def add(self, /, prop_id: int, value: Any) -> None:
        """
        プロパティを 1 件追加する。

        偶数 ID は vi64、奇数 ID は長さ付きバイト列で表現する
        (draft-ietf-moq-loc-04 §2.3)。`value` には `int` または `bytes` を渡す。

        この時点では ID と値の型の対応を検査しない。対応が取れていないプロパティは
        `encode()` が `ValueError` で拒否する。不正な入力を意図的に組み立てて
        検証したいテストのために、構築時ではなく encode 時に検査する。
        """
    @property
    def audio_config(self, /) -> bytes |None:
        """
        Audio Config (ID=0x0F): 音声コーデックの設定。

        (draft-ietf-moq-loc-04 §2.3.3.1)
        """
    @property
    def audio_level(self, /) -> int |None:
        """
        Audio Level (ID=0x0C): RFC 6464 の音声レベル (vi64 の下位 8 bit)。

        (draft-ietf-moq-loc-04 §2.3.3.2)
        """
    @staticmethod
    def decode(data: bytes) -> tuple[LocProperties, int]:
        """
        バッファ先頭からプロパティブロックをデコードし `(プロパティ, 消費バイト数)` を返す。

        ブロックの後ろに続くバイト列は消費しない。
        """
    def encode(self, /) -> bytes:
        """
        プロパティブロック全体をエンコードする。

        空の集合は Properties Length = 0 の 1 バイトになる。
        (draft-ietf-moq-transport-21 §11.3.1 (Subgroup Header))
        """
    @property
    def timescale(self, /) -> int |None:
        """
        Timescale (ID=0x08): Timestamp の単位 (1 秒あたりのユニット数)。

        代表的な値は 1000000 (マイクロ秒)、48000、90000 である。
        (draft-ietf-moq-loc-04 §2.3.1.2)
        """
    @property
    def timestamp(self, /) -> int |None:
        """
        Timestamp (ID=0x10): エンコードされたメディアフレームのタイムスタンプ。

        Timescale が無い場合は Unix エポック以降のマイクロ秒、ある場合は
        Timescale 単位のメディア時刻として解釈する。
        (draft-ietf-moq-loc-04 §2.3.1.1)
        """
    def to_dict(self, /) -> dict:
        """
        プロパティを `{prop_id: 値}` の辞書へ変換する。
        """
    @property
    def video_config(self, /) -> bytes |None:
        """
        Video Config (ID=0x0D): ビデオコーデックの設定 (extradata)。

        (draft-ietf-moq-loc-04 §2.3.2.1)
        """
    @property
    def video_frame_marking(self, /) -> bytes |None:
        """
        Video Frame Marking (ID=0x09): RFC 9626 のビデオフレームフラグ。

        内容は解釈せずバイト列として返す。長さは 1-4 バイトである。
        (draft-ietf-moq-loc-04 §2.3.2.2)
        """

@final
class MediaTimeline:
    """
    MSF メディアタイムライン (draft-ietf-moq-msf-01 §7.1)。

    フォーマットは `[[pts_ms, [group_id, object_id], wallclock_ms], ...]` である。
    """
    def __eq__(self, other: object, /) -> bool: ...
    def __len__(self, /) -> int: ...
    def __new__(cls, /) -> MediaTimeline:
        """
        空のメディアタイムラインを作成する。
        """
    def __repr__(self, /) -> str: ...
    def add(self, /, pts_ms: int, group_id: int, object_id: int, wallclock_ms: int) -> None:
        """
        エントリを末尾に追加する。

        `wallclock_ms` が不明な場合は 0 を渡す。
        """
    @staticmethod
    def decode(data: bytes) -> MediaTimeline:
        """
        JSON バイト列からメディアタイムラインを読み込む。

        gzip で圧縮された入力は自動的に展開する。
        """
    def encode(self, /, gzip: bool = False) -> bytes:
        """
        メディアタイムラインを JSON バイト列へ書き出す。

        `gzip` が真の場合は gzip で圧縮する (draft-ietf-moq-msf-01 §7.1)。
        """
    @property
    def entries(self, /) -> list[tuple[int, int, int, int]]:
        """
        エントリ列を `(pts_ms, group_id, object_id, wallclock_ms)` のリストとして返す。
        """

@final
class Message:
    """
    Python 側へ渡す制御メッセージ 1 件。

    メッセージ本体は種別ごとに異なる辞書であり、キーは
    [draft-ietf-moq-transport-21 §9 (Control Messages)](https://datatracker.ietf.org/doc/draft-ietf-moq-transport/)
    の各メッセージが運ぶフィールドに対応する。
    """
    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self, /) -> str: ...
    @property
    def body(self, /) -> dict:
        """
        メッセージ本体。
        """
    @property
    def kind(self, /) -> str:
        """
        メッセージ種別を表す文字列。

        `setup` / `goaway` / `request_ok` / `request_error` / `subscribe` /
        `subscribe_ok` / `request_update` / `publish` / `publish_done` /
        `publish_state_notify` / `fetch` / `fetch_ok` / `track_status` のいずれかである。
        列挙は `moqt.moqt` が扱う制御メッセージの全体であり、relay 専用の
        namespace 発見・告知機構は含まない。
        """
    @property
    def parameters(self, /) -> Any:
        """
        メッセージが運ぶパラメータ。

        パラメータを持たないメッセージでは空の辞書を返す。
        """
    @property
    def raw(self, /) -> bytes:
        """
        デコードに使った生バイト列 (Type + Length + Message Body)。

        そのまま peer へ中継できる形である。
        """
    @property
    def request_id(self, /) -> int |None:
        """
        メッセージが運ぶ Request ID。

        応答メッセージはワイヤに Request ID を含まないため `None` を返す。
        (draft-ietf-moq-transport-21 §9.4 (REQUEST_ERROR))
        """
    @property
    def type_id(self, /) -> int:
        """
        wire 上のメッセージ Type (vi64)。
        """

@final
class ObjectProperties:
    """
    MOQT の Object Properties。

    ワイヤフォーマットは `Properties Length (vi64) | Key-Value-Pairs...` である。
    encode は prop_type の昇順にソートし、delta encoding で型番号を圧縮する。
    (draft-ietf-moq-transport-21 §16.8 (Properties) Table 14)
    """
    def __bytes__(self, /) -> bytes:
        """
        Properties を含むオブジェクトを送信する引数へそのまま渡せるバイト列を返す。

        `moqt.moq.Publication.send_object` と `send_datagram` の `properties_data` は
        この形を受け取る。
        """
    def __eq__(self, other: object, /) -> bool: ...
    def __len__(self, /) -> int: ...
    def __new__(cls, /) -> ObjectProperties:
        """
        空のプロパティ集合を作成する。
        """
    def __repr__(self, /) -> str: ...
    def add(self, /, prop_type: int, value: Any) -> None:
        """
        プロパティを 1 件追加する。

        偶数型は varint として `int` を、奇数型は長さ付きバイト列として `bytes` を渡す。
        この時点では型番号と値の型の対応を検査しない。対応が取れていないプロパティは
        `encode()` が `ValueError` で拒否する。
        """
    @staticmethod
    def decode(data: bytes) -> tuple[ObjectProperties, int]:
        """
        バッファ先頭からプロパティブロックをデコードし `(プロパティ, 消費バイト数)` を返す。

        ブロックの後ろに続くバイト列は消費しない。入れ子の IMMUTABLE_PROPERTIES など
        draft の MUST に違反する入力は `ValueError` になる。
        """
    def encode(self, /) -> bytes:
        """
        プロパティブロック全体をエンコードする。

        空の集合は Properties Length = 0 の 1 バイトになる。
        """
    @property
    def immutable_properties(self, /) -> bytes |None:
        """
        IMMUTABLE_PROPERTIES (0x0B): 途中で変化しないプロパティの入れ子リスト。

        内容は解釈せず生バイト列として返す。
        (draft-ietf-moq-transport-21 §10.7 (Immutable Properties))
        """
    @property
    def object_delivery_timeout(self, /) -> int |None:
        """
        OBJECT_DELIVERY_TIMEOUT (0x02): Object の配送期限 (ms)。

        (draft-ietf-moq-transport-21 §10.2 (OBJECT_DELIVERY_TIMEOUT))
        """
    @property
    def prior_group_id_gap(self, /) -> int |None:
        """
        PRIOR_GROUP_ID_GAP (0x3C): 直前の存在しない Group の個数。

        (draft-ietf-moq-transport-21 §10.8 (Prior Group ID Gap))
        """
    @property
    def prior_object_id_gap(self, /) -> int |None:
        """
        PRIOR_OBJECT_ID_GAP (0x3E): 直前の存在しない Object の個数。

        (draft-ietf-moq-transport-21 §10.9 (Prior Object ID Gap))
        """
    @property
    def subgroup_delivery_timeout(self, /) -> int |None:
        """
        SUBGROUP_DELIVERY_TIMEOUT (0x06): Subgroup の配送期限 (ms)。

        (draft-ietf-moq-transport-21 §10.1 (SUBGROUP_DELIVERY_TIMEOUT))
        """
    def to_dict(self, /) -> dict:
        """
        プロパティを `{prop_type: 値}` の辞書へ変換する。

        未知の型番号も含めてすべて返す。
        """

@final
class Session:
    """
    1 本の MoQT Transport Session に対応する sans I/O セッション状態機械。

    ストリームの実体には触れない。呼び出し側が peer のストリーム種別を判定して
    `receive_*` を呼び、戻り値のイベントに従ってバイト列を送る。
    relay 全体の routing / fan-out / cache / policy は扱わない。
    """
    @staticmethod
    def client(implementation: str = "moqt-py") -> Session:
        """
        client role の MoQT Session を作成する。
        """
    def close(self, /, code: int, reason: str = "internal error") -> list[Event]:
        """
        セッションを閉じる。

        理由はライブラリが 'static な文字列しか受け取らないため、既知の理由だけを
        そのまま渡し、それ以外は internal error として扱う。
        """
    @property
    def established(self, /) -> bool:
        """
        SETUP 交換が完了しているかを返す。
        """
    def fetch_cleanup_ready(self, /, request_id: int) -> bool |None:
        """
        fetch が破棄可能かを返す。
        """
    def fetch_stop_sending_received(self, /, request_id: int) -> list[Event]:
        """
        FETCH の STOP_SENDING を受信したことを通知する。
        """
    def forget_fetch(self, /, request_id: int) -> bool:
        """
        終了済みの fetch を破棄する。
        """
    def forget_subscription(self, /, request_id: int) -> bool:
        """
        終了済みの subscription を破棄する。
        """
    @property
    def last_error(self, /) -> str |None:
        """
        状態機械が通知した直近のエラー理由を返す。

        プロトコル違反の切り分けに使う診断用の値である。
        """
    def next_local_request_id(self, /) -> int:
        """
        次の request 用 Request ID を予約する。
        """
    def receive_control(self, /, data: bytes) -> list[Event]:
        """
        peer 制御ストリームの断片を投入し、発生したイベントを返す。
        """
    def receive_control_stream_closed(self, /, reset: bool = False, error_code: int |None = None) -> list[Event]:
        """
        peer 制御ストリームが終端したことを通知する。

        `reset` が真の場合は RESET_STREAM、偽の場合は FIN として扱う。
        """
    def receive_data_stream(self, /, stream_id: int, data: bytes, stream_type: int |None = None) -> tuple[list[tuple[int, int, int, str]], list[Event]]:
        """
        peer の data stream の断片を投入し、発生したイベントを返す。

        最初の断片に含まれる stream type を状態機械へ通知し、種別に応じたデコーダで
        ヘッダとオブジェクトをデコードする。オブジェクトのペイロードはイベントの
        `data` にそのまま入る。

        返り値は `(オブジェクトの受理結果, イベント列)` の組である。受理結果は
        デコードしたオブジェクトごとに `(stream_id, object_id, payload_length, 受理結果)`
        を並べたリストである。
        """
    def receive_data_stream_closed(self, /, stream_id: int, reset: bool = False, error_code: int |None = None, reliable_size: int |None = None) -> list[Event]:
        """
        peer の data stream が終端したことを通知する。
        """
    def receive_datagram(self, /, data: bytes) -> list[Event]:
        """
        peer のデータグラムを投入し、発生したイベントを返す。

        オブジェクトを受理した場合は、その内容を `object` イベントとして返す。
        """
    def receive_request_stream(self, /, stream_id: int, data: bytes, role: str = "local") -> list[Event]:
        """
        request stream の断片を投入し、発生したイベントを返す。

        `role` はストリームをどちら側が開始したかを表す。

        - `"local"`: 自側が開始した request への応答である
        - `"peer"`: peer が開始した request である

        応答メッセージはワイヤに Request ID を含まないため、この区別は I/O 層
        (Python 側) が保持する。
        """
    def receive_request_stream_closed(self, /, stream_id: int, reset: bool = False, error_code: int |None = None, reliable_size: int |None = None) -> list[Event]:
        """
        peer の request stream が終端したことを通知する。
        """
    def recv_data_stream_stop_sending(self, /, stream_id: int) -> list[Event]:
        """
        peer が受信ストリームへ STOP_SENDING を送ったことを通知する。
        """
    def register_local_request_stream(self, /, stream_id: int, request_id: int) -> None:
        """
        自側が開始した request stream を登録する。

        MoQT の応答メッセージはワイヤに Request ID を含まないため、Python 側が
        `send_request` イベントでストリームを開いた直後にこの対応を登録する。
        """
    def report_mid_object_fin(self, /, stream_id: int) -> list[Event]:
        """
        オブジェクトの受信途中でストリームが終端したことを通知する。
        """
    def reset_outgoing_data_stream(self, /, stream_id: int, error_code: int, reliable_size: int |None = None) -> list[Event]:
        """
        送信済みのデータストリームを reset する。

        `reliable_size` を渡すと RESET_STREAM_AT になり、先頭 `reliable_size` バイトは
        peer へ確実に届ける (draft-ietf-moq-transport-21 §11.3.2 (Subgroup Object))。
        省略した場合は RESET_STREAM になり、未達のデータは破棄される。
        """
    def role(self, /) -> str:
        """
        自側の役割を返す。
        """
    def send_data_stream_closed(self, /, stream_id: int, reset: bool = False, error_code: int |None = None, reliable_size: int |None = None) -> list[Event]:
        """
        送信済みのデータストリームを終了する。
        """
    def send_data_stream_stop_sending(self, /, stream_id: int) -> list[Event]:
        """
        自側が受信ストリームへ STOP_SENDING を送ったことを通知する。
        """
    def send_fetch(self, /, namespace: Sequence[Sequence[int]], track_name: Sequence[int], parameters: Any) -> list[Event]:
        """
        FETCH を送信する。
        """
    def send_fetch_data_stream_closed(self, /, stream_id: int) -> list[Event]:
        """
        fetch ストリームを終了する。
        """
    def send_fetch_header(self, /, stream_id: int, request_id: int) -> list[Event]:
        """
        送信する fetch ストリームを登録する。
        """
    def send_fetch_object(self, /, stream_id: int) -> list[Event]:
        """
        fetch ストリームへオブジェクトを書き込むことを通知する。
        """
    def send_fetch_ok(self, /, request_id: int, end_of_track: bool, end_location: tuple[int, int], parameters: Any, track_properties: Any) -> list[Event]:
        """
        FETCH_OK を送信する。
        """
    def send_fetch_stop_sending(self, /, request_id: int) -> list[Event]:
        """
        FETCH の STOP_SENDING を送信する。
        """
    def send_fill_fetch_header(self, /, stream_id: int, request_id: int) -> list[Event]:
        """
        送信する fill fetch ストリームを登録する。
        """
    def send_goaway(self, /, new_session_uri: Sequence[int], timeout: int) -> list[Event]:
        """
        GOAWAY を送信する。
        """
    def send_goaway_on_request_stream(self, /, request_id: int, new_session_uri: Sequence[int], timeout: int) -> list[Event]:
        """
        request stream 上に GOAWAY を送信する。
        """
    def send_object_datagram(self, /, request_id: int, group_id: int, object_id: int, properties_data: Sequence[int] |None = None, status: int |None = None) -> tuple[bool, list[Event]]:
        """
        オブジェクトデータグラムを送信することを通知する。

        フィルタで破棄される場合は `False` を返す。その場合 Python 側は
        データグラムを送信してはならない。
        """
    def send_padding_datagram(self, /, length: int) -> list[Event]:
        """
        パディングデータグラムの送信を要求する。
        """
    def send_padding_stream(self, /, length: int) -> list[Event]:
        """
        パディングストリームの送信を要求する。
        """
    def send_publish(self, /, namespace: Sequence[Sequence[int]], track_name: Sequence[int], track_alias: int, parameters: Any, track_properties: Any) -> list[Event]:
        """
        PUBLISH を送信する。
        """
    def send_publish_done(self, /, request_id: int, status_code: int, stream_count: int, reason: str) -> list[Event]:
        """
        PUBLISH_DONE を送信する。
        """
    def send_publish_done_for_subscription(self, /, request_id: int, status_code: int, reason: str) -> list[Event]:
        """
        自側が受け持つ subscription の応答を処理する。

        REQUEST_UPDATE に対して FORWARD などを変更する場合に使う。
        """
    def send_publish_state_notify(self, /, request_id: int, parameters: Any) -> list[Event]:
        """
        PUBLISH_STATE_NOTIFY を送信する。
        """
    def send_request_error(self, /, request_id: int, error_code: int, retry_interval: int, reason: str, redirect: tuple[Sequence[int], Sequence[Sequence[int]], Sequence[int]] |None = None) -> list[Event]:
        """
        REQUEST_ERROR を送信する。

        `redirect` は `(connect_uri, track_namespace, track_name)` のタプルである。
        """
    def send_request_ok(self, /, request_id: int, parameters: Any, track_properties: Any) -> list[Event]:
        """
        REQUEST_OK を送信する。
        """
    def send_request_update(self, /, request_id: int, parameters: Any) -> list[Event]:
        """
        REQUEST_UPDATE を送信する。
        """
    def send_subgroup_header(self, /, stream_id: int, request_id: int, track_alias: int, group_id: int, subgroup_id: int |None, publisher_priority: int |None = None, has_properties: bool = False, end_of_group: bool = False, first_object: bool = False) -> list[Event]:
        """
        送信する subgroup ストリームを登録する。

        実際のバイト列は Python 側が組み立てるため、ここでは状態機械へ登録だけを行う。
        """
    def send_subgroup_object(self, /, stream_id: int, object_id: int, properties_data: Sequence[int] |None = None) -> tuple[bool, list[Event]]:
        """
        subgroup ストリームへオブジェクトを書き込むことを通知する。

        フィルタで破棄される場合は `False` を返す。その場合 Python 側は
        バイト列を送信してはならない。
        """
    def send_subscribe(self, /, namespace: Sequence[Sequence[int]], track_name: Sequence[int], parameters: Any) -> list[Event]:
        """
        SUBSCRIBE を送信する。
        """
    def send_subscribe_ok(self, /, request_id: int, track_alias: int, parameters: Any, track_properties: Any) -> list[Event]:
        """
        SUBSCRIBE_OK を送信する。
        """
    def send_track_status(self, /, namespace: Sequence[Sequence[int]], track_name: Sequence[int], parameters: Any) -> list[Event]:
        """
        TRACK_STATUS を送信する。
        """
    @staticmethod
    def server(implementation: str = "moqt-py") -> Session:
        """
        server role の MoQT Session を作成する。
        """
    def set_control_message_timeout_ms(self, /, timeout_ms: int |None) -> None:
        """
        制御メッセージのタイムアウト (ms) を設定する。
        """
    def set_data_stream_timeout_ms(self, /, timeout_ms: int |None) -> None:
        """
        データストリームのタイムアウト (ms) を設定する。
        """
    def start(self, /) -> bytes:
        """
        自側制御ストリームの stream type prefix と SETUP を返す。
        """
    def state(self, /) -> str:
        """
        現在のセッション状態を返す。
        """
    def stop_sending(self, /, request_id: int) -> list[Event]:
        """
        subscription を終了する (subscriber 側の STOP_SENDING)。
        """
    def subscription_cleanup_ready(self, /, request_id: int) -> bool |None:
        """
        subscription が破棄可能かを返す。
        """
    def subscription_track_alias(self, /, request_id: int) -> int |None:
        """
        request_id に対応する subscription の track alias を返す。

        alias は SUBSCRIBE_OK の受信後に確定する。
        """
    def tick(self, /, now_ms: int) -> list[Event]:
        """
        時間を進めてタイムアウトを判定する。
        """

@final
class TrackProperties:
    """
    MOQT の Track Properties。

    Track 単位で決まるプロパティである。SUBSCRIBE_OK / FETCH_OK / PUBLISH が運ぶ
    (draft-ietf-moq-transport-21 §16.8 (Properties) Table 14)。
    """
    def __eq__(self, other: object, /) -> bool: ...
    def __len__(self, /) -> int: ...
    def __new__(cls, /) -> TrackProperties:
        """
        空のプロパティ集合を作成する。
        """
    def __repr__(self, /) -> str: ...
    def add(self, /, prop_type: int, value: Any) -> None:
        """
        プロパティを 1 件追加する。

        偶数型は varint として `int` を、奇数型は長さ付きバイト列として `bytes` を渡す。
        """
    @property
    def default_publisher_group_order(self, /) -> int |None:
        """
        DEFAULT_PUBLISHER_GROUP_ORDER (0x22): 既定の Group Order。

        省略時は `None` になる。draft の既定値 Ascending (0x1) は適用しない
        (draft-ietf-moq-transport-21 §10.5 (Default Publisher Group Order))。
        """
    @property
    def default_publisher_priority(self, /) -> int |None:
        """
        DEFAULT_PUBLISHER_PRIORITY (0x0E): 既定の Publisher Priority。

        省略時は `None` になる。draft の既定値 128 は適用しない
        (draft-ietf-moq-transport-21 §10.4 (Default Publisher Priority))。
        """
    @property
    def dynamic_groups(self, /) -> int |None:
        """
        DYNAMIC_GROUPS (0x30): Group が動的に決まるか。

        (draft-ietf-moq-transport-21 §10.6 (Dynamic Groups))
        """
    @property
    def has_unknown_mandatory(self, /) -> bool:
        """
        未知の必須プロパティを含むか。

        必須の範囲は `MANDATORY_TRACK_PROPERTY_MIN` から `MANDATORY_TRACK_PROPERTY_MAX`
        である (draft-ietf-moq-transport-21 §16.8 (Properties) Table 14)。未知の必須
        プロパティを含む Track は扱えないため、アプリは購読を拒否できる。
        """
    @property
    def object_delivery_timeout(self, /) -> int |None:
        """
        OBJECT_DELIVERY_TIMEOUT (0x02): Object の配送期限 (ms)。
        """
    @property
    def subgroup_delivery_timeout(self, /) -> int |None:
        """
        SUBGROUP_DELIVERY_TIMEOUT (0x06): Subgroup の配送期限 (ms)。
        """
    def to_dict(self, /) -> dict:
        """
        プロパティを `{prop_type: 値}` の辞書へ変換する。

        辞書は Session の `send_*` に渡す `track_properties` 引数と同じ形である。
        未知の型番号も含めてすべて返す。
        """

@final
class Uri:
    """
    パース済みの MSF URI (draft-ietf-moq-msf-01 §11.1)。

    `moqt://` URI の fragment (`msf:...`) をパースした結果である。percent-decode は
    行わず、値をそのまま保持する。
    """
    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self, /) -> str: ...
    @property
    def authority(self, /) -> str:
        """
        authority (host + 任意の port)。
        """
    def c4m_tokens(self, /) -> list[str]:
        """
        c4m パラメータの値を出現順に返す。
        """
    def connection_types(self, /) -> list[str]:
        """
        connection パラメータが要求する接続種別を出現順に返す。

        値は `quic` または `webtransport` である。
        """
    def location_ranges(self, /) -> list[dict]:
        """
        location-range パラメータを辞書のリストとして返す。

        各辞書は `start_group_id` / `start_object_id` / `end_group_id` /
        `end_object_id` を持つ。省略された要素は `None` になる。
        """
    def mediatime_ranges(self, /) -> list[tuple[int, int |None]]:
        """
        mediatime-range パラメータを `(開始 ms, 終了 ms)` のリストとして返す。
        """
    @property
    def namespace(self, /) -> list:
        """
        track-identifier を分解したネームスペース。
        """
    def parameter_values(self, /, name: str) -> list[str]:
        """
        指定名のパラメータ値を出現順に返す。
        """
    @property
    def parameters(self, /) -> list[tuple[str, str]]:
        """
        fragment パラメータ列を `(名前, 値)` のリストとして返す。出現順である。
        """
    @staticmethod
    def parse(uri: str) -> Uri:
        """
        MSF URI をパースする。

        draft-ietf-moq-msf-01 §11.1 の
        `msf-uri = "moqt://" authority path-abempty [ "?" query ] "#" msf-fragment`
        に従う。scheme は case-insensitive である。
        """
    @property
    def path(self, /) -> str:
        """
        path (先頭の `/` を含む。無い場合は空文字列)。
        """
    @property
    def query(self, /) -> str |None:
        """
        query (`?` 以降。無い場合は `None`)。
        """
    @property
    def track_name(self, /) -> bytes:
        """
        track-identifier を分解した Track 名。
        """
    def wallclock_ranges(self, /) -> list[tuple[int, int |None]]:
        """
        wallclock-range パラメータを `(開始 ms, 終了 ms)` のリストとして返す。

        終了が省略された open range の終了は `None` になる。
        """

def classify_data_stream_type(type_id: int) -> str |None:
    """
    stream type の varint が制御ストリームかデータストリームかを判定する。

    データストリームの場合は種別を表す文字列を返す。制御ストリームと未知の値は
    `None` を返す。

    (draft-ietf-moq-transport-21 §6.4.1 (Unidirectional Streams) Table 3)
    """

def decode_message(data: bytes) -> tuple[Message, int]:
    """
    制御メッセージを 1 件デコードする。

    返り値は `(メッセージ, 消費バイト数)` である。`data` の先頭が制御メッセージの
    途中で切れている場合と、メッセージとして不正な場合は `ValueError` を送出する。

    制御メッセージは Type (vi64) + Length (u16 big-endian) + Message Body で構成される
    (draft-ietf-moq-transport-21 §9 (Control Messages))。
    """

def decode_parameter(param_type: int, value: bytes) -> Any:
    """
    パラメータの値部分をデコードして Python の値へ変換する。

    `Event.parameters` と `Message.parameters` が返す辞書の値は、パラメータ 1 件分の
    エンコード済みバイト列である。この関数で型に応じた値へ解釈する。
    偶数型は `int`、長さ付きバイト列は `bytes`、`LARGEST_OBJECT` は
    `(group_id, object_id)`、`AUTHORIZATION_TOKEN` は辞書、
    `FILL_PARAMETERS` は入れ子の辞書になる。

    解釈できないバイト列は `ValueError` になる。
    (draft-ietf-moq-transport-21 §9.20 (Control Message Parameters))
    """

def decode_varint(data: bytes) -> tuple[int, int]:
    """
    先頭の vi64 をデコードし `(値, 消費バイト数)` を返す。

    非最小エンコーディングも受理する。バイト列が途中で切れている場合は
    `ValueError` を送出する。
    """

def decode_varint_prefix(data: bytes) -> tuple[int, int] |None:
    """
    先頭の vi64 をデコードし `(値, 消費バイト数)` を返す。

    バイト列が途中で切れている場合は `None` を返し、続きの到着を待つ。
    非最小エンコーディングも受理する。
    """

def encode_varint(value: int) -> bytes:
    """
    vi64 をエンコードする (draft-ietf-moq-transport-21 §8.1 (Variable-Length Integers))。

    最小バイト数の表現を返す。
    """

def is_padding_datagram(data: bytes) -> bool:
    """
    データグラムの種別がパディングかを判定する。

    データグラムは stream type を持たないため、先頭の varint で判定する。
    """

def parse_fragment_pairs(fragment: str) -> list[tuple[str, str]]:
    """
    fragment を `&` 区切りのパラメータ列へ分解する
    (draft-ietf-moq-msf-01 §11.1)。

    `msf:` prefix の検証は行わず、`&` 区切りの `名前=値` を取り出すだけである。
    """

def parse_msf_fragment(fragment: str) -> tuple[Any, bytes, list[tuple[str, str]]]:
    """
    MSF fragment (`msf:` prefix 付き) を namespace と Track 名とパラメータへ分解する
    (draft-ietf-moq-msf-01 §11.1 (URL construction and interpretation))。

    `Uri.parse` を通さない入力を扱う。返り値は
    `(namespace, track_name, [(名前, 値), ...])` である。
    """

def parse_name(text: str) -> tuple[Any, bytes]:
    """
    MSF の Track 識別子 (`namespace--track` 形式) を namespace と Track 名へ分解する
    (draft-ietf-moq-transport-21 §8.8 (Representing Namespace and Track Names))。
    """

def resolve_catalog_variables(document: bytes, fragment: str) -> bytes:
    """
    カタログの変数参照を fragment の値で解決する
    (draft-ietf-moq-msf-01 §5.4 (Catalog variables))。
    """

def resolve_timeline_template(template: list, n: int) -> tuple[int, int, int, int] |None:
    """
    MSF の media timeline template から n 番目のエントリを計算する
    (draft-ietf-moq-msf-01 §7.4.1)。

    `template` は `Catalog.tracks` の `template` 配列である。値が負の整数または
    配列でない場合は `ValueError` になる。計算が overflow する場合は `None` を返す。
    """

def serialize_name(namespace: Sequence[Sequence[int]], track_name: bytes) -> str:
    """
    namespace と Track 名を MSF の Track 識別子 (`namespace--track` 形式) へ変換する。
    """

def setup_stream_type() -> int:
    """
    制御ストリームの stream type を返す。
    """

def __getattr__(name: str) -> Incomplete: ...
