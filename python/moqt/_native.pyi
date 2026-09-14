"""
Python から `import moqt._native` される拡張モジュール。
"""

from _typeshed import Incomplete
from collections.abc import Sequence
from typing import Any, final

@final
class _CoreEvent:
    """
    Sans I/O facade が Python 側へ返すイベント。
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
    def stream_id(self, /) -> int |None:
        """
        対象データストリームの ID。
        """
    @property
    def track_alias(self, /) -> int |None:
        """
        受信した data stream の Track Alias (object イベントのみ)。

        data stream は Request ID ではなく Track Alias で購読を特定するため、
        購読との対応付けに使う。
        """

@final
class _CoreSession:
    """
    1 本の WebTransport session に対応する MoQT Session facade。
    """
    @staticmethod
    def client(implementation: str = "moqt-py") -> _CoreSession:
        """
        client role の MoQT Session を作成する。
        """
    def close(self, /, code: int, reason: str = "internal error") -> list[_CoreEvent]:
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
    def fetch_stop_sending_received(self, /, request_id: int) -> list[_CoreEvent]:
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
    def next_local_request_id(self, /) -> int:
        """
        次の request 用 Request ID を予約する。
        """
    def receive_control(self, /, data: bytes) -> list[_CoreEvent]:
        """
        peer 制御ストリームの断片を投入し、発生したイベントを返す。
        """
    def receive_control_stream_closed(self, /, reset: bool = False, error_code: int |None = None) -> list[_CoreEvent]:
        """
        peer 制御ストリームが終端したことを通知する。

        `reset` が真の場合は RESET_STREAM、偽の場合は FIN として扱う。
        """
    def receive_data_stream(self, /, stream_id: int, data: bytes) -> tuple[list[tuple[int, int, int, str]], list[_CoreEvent]]:
        """
        peer の data stream の断片を投入し、発生したイベントを返す。

        最初の断片に含まれる stream type を状態機械へ通知し、種別に応じたデコーダで
        ヘッダとオブジェクトをデコードする。オブジェクトのペイロードはイベントの
        `data` にそのまま入る。

        返り値は `(オブジェクトの受理結果, イベント列)` の組である。受理結果は
        デコードしたオブジェクトごとに `(stream_id, object_id, payload_length, 受理結果)`
        を並べたリストである。
        """
    def receive_data_stream_closed(self, /, stream_id: int, reset: bool = False, error_code: int |None = None, reliable_size: int |None = None) -> list[_CoreEvent]:
        """
        peer の data stream が終端したことを通知する。
        """
    def receive_datagram(self, /, data: bytes) -> list[_CoreEvent]:
        """
        peer のデータグラムを投入し、発生したイベントを返す。
        """
    def receive_request_stream(self, /, stream_id: int, data: bytes, role: str = "local") -> list[_CoreEvent]:
        """
        request stream の断片を投入し、発生したイベントを返す。

        `role` はストリームをどちら側が開始したかを表す。

        - `"local"`: 自側が開始した request への応答である
        - `"peer"`: peer が開始した request である

        応答メッセージはワイヤに Request ID を含まないため、この区別は I/O 層
        (Python 側) が保持する。
        """
    def receive_request_stream_closed(self, /, stream_id: int, reset: bool = False, error_code: int |None = None, reliable_size: int |None = None) -> list[_CoreEvent]:
        """
        peer の request stream が終端したことを通知する。
        """
    def recv_data_stream_stop_sending(self, /, stream_id: int) -> list[_CoreEvent]:
        """
        peer が受信ストリームへ STOP_SENDING を送ったことを通知する。
        """
    def register_local_request_stream(self, /, stream_id: int, request_id: int) -> None:
        """
        自側が開始した request stream を登録する。

        MoQT の応答メッセージはワイヤに Request ID を含まないため、Python 側が
        `send_request` イベントでストリームを開いた直後にこの対応を登録する。
        """
    def register_peer_request_stream(self, /, stream_id: int) -> None:
        """
        peer が開始した request stream を登録する。

        応答を返す前に登録すると、以降のメッセージを応答として処理できる。
        """
    def report_mid_object_fin(self, /, stream_id: int) -> list[_CoreEvent]:
        """
        オブジェクトの受信途中でストリームが終端したことを通知する。
        """
    def reset_outgoing_data_stream(self, /, stream_id: int, error_code: int) -> list[_CoreEvent]:
        """
        送信済みのデータストリームを reset する。
        """
    def role(self, /) -> str:
        """
        自側の役割を返す。
        """
    def send_data_stream_closed(self, /, stream_id: int, reset: bool = False, error_code: int |None = None, reliable_size: int |None = None) -> list[_CoreEvent]:
        """
        送信済みのデータストリームを終了する。
        """
    def send_data_stream_stop_sending(self, /, stream_id: int) -> list[_CoreEvent]:
        """
        自側が受信ストリームへ STOP_SENDING を送ったことを通知する。
        """
    def send_fetch(self, /, namespace: Sequence[Sequence[int]], track_name: Sequence[int], parameters: Any) -> list[_CoreEvent]:
        """
        FETCH を送信する。
        """
    def send_fetch_data_stream_closed(self, /, stream_id: int) -> list[_CoreEvent]:
        """
        fetch ストリームを終了する。
        """
    def send_fetch_header(self, /, stream_id: int, request_id: int) -> list[_CoreEvent]:
        """
        送信する fetch ストリームを登録する。
        """
    def send_fetch_object(self, /, stream_id: int) -> list[_CoreEvent]:
        """
        fetch ストリームへオブジェクトを書き込むことを通知する。
        """
    def send_fetch_ok(self, /, request_id: int, end_of_track: bool, end_location: tuple[int, int], parameters: Any, track_properties: Any) -> list[_CoreEvent]:
        """
        FETCH_OK を送信する。
        """
    def send_fetch_stop_sending(self, /, request_id: int) -> list[_CoreEvent]:
        """
        FETCH の STOP_SENDING を送信する。
        """
    def send_fill_fetch_header(self, /, stream_id: int, request_id: int) -> list[_CoreEvent]:
        """
        送信する fill fetch ストリームを登録する。
        """
    def send_goaway(self, /, new_session_uri: Sequence[int], timeout: int) -> list[_CoreEvent]:
        """
        GOAWAY を送信する。
        """
    def send_goaway_on_request_stream(self, /, request_id: int, new_session_uri: Sequence[int], timeout: int) -> list[_CoreEvent]:
        """
        request stream 上に GOAWAY を送信する。
        """
    def send_namespace(self, /, request_id: int, suffix: Sequence[Sequence[int]]) -> list[_CoreEvent]:
        """
        NAMESPACE を送信する。
        """
    def send_namespace_done(self, /, request_id: int, suffix: Sequence[Sequence[int]]) -> list[_CoreEvent]:
        """
        NAMESPACE_DONE を送信する。
        """
    def send_object_datagram(self, /, request_id: int, group_id: int, object_id: int, properties_data: Sequence[int] |None = None, status: int |None = None) -> tuple[bool, list[_CoreEvent]]:
        """
        オブジェクトデータグラムを送信することを通知する。

        フィルタで破棄される場合は `False` を返す。その場合 Python 側は
        データグラムを送信してはならない。
        """
    def send_padding_datagram(self, /, length: int) -> list[_CoreEvent]:
        """
        パディングデータグラムの送信を要求する。
        """
    def send_padding_stream(self, /, length: int) -> list[_CoreEvent]:
        """
        パディングストリームの送信を要求する。
        """
    def send_publish(self, /, namespace: Sequence[Sequence[int]], track_name: Sequence[int], track_alias: int, parameters: Any, track_properties: Any) -> list[_CoreEvent]:
        """
        PUBLISH を送信する。
        """
    def send_publish_done(self, /, request_id: int, status_code: int, stream_count: int, reason: str) -> list[_CoreEvent]:
        """
        PUBLISH_DONE を送信する。
        """
    def send_publish_done_for_subscription(self, /, request_id: int, status_code: int, reason: str) -> list[_CoreEvent]:
        """
        自側が受け持つ subscription の応答を処理する。

        REQUEST_UPDATE に対して FORWARD などを変更する場合に使う。
        """
    def send_publish_namespace(self, /, namespace: Sequence[Sequence[int]], parameters: Any) -> list[_CoreEvent]:
        """
        PUBLISH_NAMESPACE を送信する。
        """
    def send_publish_skipped(self, /, request_id: int, suffix: Sequence[Sequence[int]], track_name: Sequence[int]) -> list[_CoreEvent]:
        """
        PUBLISH_SKIPPED を送信する。
        """
    def send_publish_state_notify(self, /, request_id: int, parameters: Any) -> list[_CoreEvent]:
        """
        PUBLISH_STATE_NOTIFY を送信する。
        """
    def send_request_error(self, /, request_id: int, error_code: int, retry_interval: int, reason: str, redirect: tuple[Sequence[int], Sequence[Sequence[int]], Sequence[int]] |None = None) -> list[_CoreEvent]:
        """
        REQUEST_ERROR を送信する。

        `redirect` は `(connect_uri, track_namespace, track_name)` のタプルである。
        """
    def send_request_ok(self, /, request_id: int, parameters: Any, track_properties: Any) -> list[_CoreEvent]:
        """
        REQUEST_OK を送信する。
        """
    def send_request_update(self, /, request_id: int, parameters: Any) -> list[_CoreEvent]:
        """
        REQUEST_UPDATE を送信する。
        """
    def send_subgroup_header(self, /, stream_id: int, request_id: int, track_alias: int, group_id: int, subgroup_id: int |None, publisher_priority: int |None = None, has_properties: bool = False, end_of_group: bool = False, first_object: bool = False) -> list[_CoreEvent]:
        """
        送信する subgroup ストリームを登録する。

        実際のバイト列は Python 側が組み立てるため、ここでは状態機械へ登録だけを行う。
        """
    def send_subgroup_object(self, /, stream_id: int, object_id: int, properties_data: Sequence[int] |None = None) -> tuple[bool, list[_CoreEvent]]:
        """
        subgroup ストリームへオブジェクトを書き込むことを通知する。

        フィルタで破棄される場合は `False` を返す。その場合 Python 側は
        バイト列を送信してはならない。
        """
    def send_subscribe(self, /, namespace: Sequence[Sequence[int]], track_name: Sequence[int], parameters: Any) -> list[_CoreEvent]:
        """
        SUBSCRIBE を送信する。
        """
    def send_subscribe_namespace(self, /, prefix: Sequence[Sequence[int]], parameters: Any) -> list[_CoreEvent]:
        """
        SUBSCRIBE_NAMESPACE を送信する。
        """
    def send_subscribe_ok(self, /, request_id: int, track_alias: int, parameters: Any, track_properties: Any) -> list[_CoreEvent]:
        """
        SUBSCRIBE_OK を送信する。
        """
    def send_subscribe_tracks(self, /, prefix: Sequence[Sequence[int]], parameters: Any) -> list[_CoreEvent]:
        """
        SUBSCRIBE_TRACKS を送信する。
        """
    def send_track_status(self, /, namespace: Sequence[Sequence[int]], track_name: Sequence[int], parameters: Any) -> list[_CoreEvent]:
        """
        TRACK_STATUS を送信する。
        """
    @staticmethod
    def server(implementation: str = "moqt-py") -> _CoreSession:
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
    def stop_sending(self, /, request_id: int) -> list[_CoreEvent]:
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
    def tick(self, /, now_ms: int) -> list[_CoreEvent]:
        """
        時間を進めてタイムアウトを判定する。
        """

def classify_data_stream_type(type_id: int) -> str |None:
    """
    stream type の varint が制御ストリームかデータストリームかを判定する。

    データストリームの場合は種別を表す文字列を返す。制御ストリームと未知の値は
    `None` を返す。

    (draft-ietf-moq-transport-21 §6.4.1 (Unidirectional Streams) Table 3)
    """

def is_padding_datagram(data: bytes) -> bool:
    """
    データグラムの種別がパディングかを判定する。

    データグラムは stream type を持たないため、先頭の varint で判定する。
    """

def setup_stream_type() -> int:
    """
    制御ストリームの stream type を返す。
    """

def __getattr__(name: str) -> Incomplete: ...
