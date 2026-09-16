"""Track を配信する側の公開 API。

SUBSCRIBE_OK で確立した配信と、PUBLISH で確立した配信の両方を扱う。
`moqt.moq.server` と `moqt.moq.client` が同じ形で参照する。

`_runtime` だけに依存し、client / server のどちらからも読み込めるようにする。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from moqt import moqt
from moqt.moq._runtime import Runtime

# PUBLISH_DONE の既定コード
# (draft-ietf-moq-transport-21 §16.11.3 (PUBLISH_DONE Codes))
DEFAULT_PUBLISH_DONE_CODE: int = moqt.PUBLISH_DONE_TRACK_ENDED


@dataclass(slots=True)
class Publication:
    """配信中の Track。"""

    request_id: int
    """PUBLISH または SUBSCRIBE の Request ID。"""

    track_alias: int
    """通知した Track Alias。"""

    namespace: tuple[bytes, ...]
    """Track Namespace。"""

    track_name: bytes
    """Track 名。"""

    runtime: Runtime
    """送信に使うランタイム。"""

    parameters: dict[int, object] = field(default_factory=dict)
    """配信を確立した応答が運んだパラメータ。

    キーはパラメータ型、値はエンコード済みバイト列である。SUBSCRIBE_OK と
    REQUEST_OK はどちらも publisher が購読条件を確定する値 (EXPIRES /
    LARGEST_OBJECT / GROUP_ORDER / DEFAULT_PUBLISHER_PRIORITY) を運ぶ
    (draft-ietf-moq-transport-21 §9.20 (Control Message Parameters))。
    """

    track_properties: dict[int, object] = field(default_factory=dict)
    """配信を確立した応答が運んだ Track Properties。

    キーは Track Property 型、値は偶数型なら `int`、奇数型なら `bytes` である
    (draft-ietf-moq-transport-21 §8.4 (Track and Object Properties))。
    """

    _group_ids: dict[int, int] = field(default_factory=dict)

    async def send_object(
        self,
        group_id: int,
        object_id: int,
        payload: bytes,
        *,
        subgroup_id: int | None = None,
        subgroup_id_mode: str | None = None,
        publisher_priority: int | None = None,
        end_of_group: bool = False,
        status: int | None = None,
        properties_data: bytes | None = None,
    ) -> None:
        """subgroup ストリームでオブジェクトを送信する。

        `status` に `moqt.moqt.OBJECT_STATUS_END_OF_GROUP` や
        `moqt.moqt.OBJECT_STATUS_END_OF_TRACK` を渡すと、その Location 以降に
        オブジェクトが無いことを通知する。このとき `payload` は空でなければならない
        (draft-ietf-moq-transport-21 §11.1.2 (Object Status))。

        `subgroup_id_mode` は Subgroup ID のエンコードモードであり、
        `moqt.moq.SUBGROUP_ID_MODE_ZERO` / `SUBGROUP_ID_MODE_FIRST_OBJECT_ID` /
        `SUBGROUP_ID_MODE_EXPLICIT` のいずれかを渡す。省略した場合は `subgroup_id` を
        渡せば `explicit`、渡さなければ `zero` になる。`first_object_id` を選ぶと
        Subgroup ID フィールドを送らず、このストリームの最初の Object ID が
        Subgroup ID になる
        (draft-ietf-moq-transport-21 §11.3.1 (Subgroup Header))。

        `properties_data` には `moqt.moqt.ObjectProperties` の encode 結果を渡す。
        Properties の有無は subgroup ヘッダで固定されるため、同じ subgroup の
        最初のオブジェクトで決める
        (draft-ietf-moq-transport-21 §11.3.1 (Subgroup Header))。
        """
        await self.runtime.send_subgroup_object(
            self.request_id,
            self.track_alias,
            group_id,
            object_id,
            payload,
            subgroup_id=subgroup_id,
            subgroup_id_mode=subgroup_id_mode,
            publisher_priority=publisher_priority,
            end_of_group=end_of_group,
            status=status,
            properties_data=properties_data,
        )

    async def send_datagram(
        self,
        group_id: int,
        object_id: int,
        payload: bytes,
        *,
        publisher_priority: int | None = None,
        properties_data: bytes | None = None,
        status: int | None = None,
    ) -> None:
        """オブジェクトデータグラムを送信する。

        `publisher_priority` を省略すると DEFAULT_PRIORITY bit が立ち、購読を確立した
        制御メッセージで指定された優先度を継承する。受信側では
        `MoqtObject.publisher_priority` が `None` になる
        (draft-ietf-moq-transport-21 §11.2.1 (Object Datagram))。

        `status` の扱いは `send_object` と同じである。

        データグラムの合計サイズが `moqt.moqt.MAX_DATAGRAM_SIZE` を超える場合は警告を
        記録する。上限は経路 MTU に依存し、超えたデータグラムは通知なく破棄される
        (draft-ietf-moq-transport-21 §11.2.1 (Object Datagram))。大きいオブジェクトは
        subgroup ストリームで送ること。
        """
        await self.runtime.send_object_datagram(
            self.request_id,
            group_id,
            object_id,
            payload,
            publisher_priority,
            properties_data,
            status,
        )

    async def send_publish_state_notify(
        self,
        parameters: dict[int, object] | None = None,
    ) -> None:
        """PUBLISH_STATE_NOTIFY を送る。

        応答は不要であり、購読側のクレジットも消費しない
        (draft-ietf-moq-transport-21 §9.10 (PUBLISH_STATE_NOTIFY))。
        """
        await self.runtime.send_publish_state_notify(self.request_id, parameters)

    async def close(
        self,
        status_code: int = DEFAULT_PUBLISH_DONE_CODE,
        reason: str = "",
    ) -> None:
        """配信を終了する。

        PUBLISH_DONE を送り、送信中の subgroup ストリームも終了する。
        """
        await self.runtime.finish_subgroup(self.request_id)
        await self.runtime.send_publish_done(self.request_id, status_code, reason)

    async def reset_subgroup(self, error_code: int = moqt.STREAM_CANCELLED) -> None:
        """送信中の subgroup ストリームを reset する。

        送信済みのオブジェクトは破棄される
        (draft-ietf-moq-transport-21 §16.11.4 (Stream Reset Codes))。
        """
        await self.runtime.reset_subgroup(self.request_id, error_code)

    async def reset_subgroup_at(
        self,
        reliable_size: int,
        error_code: int = moqt.STREAM_CANCELLED,
    ) -> None:
        """送信中の subgroup ストリームを RESET_STREAM_AT で reset する。

        先頭 `reliable_size` バイトは peer へ確実に届き、残りは破棄される
        (draft-ietf-moq-transport-21 §11.3.2 (Subgroup Object))。
        `reliable_size` は stream type と subgroup ヘッダを含む送信済みバイト数である。
        """
        await self.runtime.reset_subgroup_at(self.request_id, reliable_size, error_code)


__all__ = [
    "DEFAULT_PUBLISH_DONE_CODE",
    "Publication",
]
