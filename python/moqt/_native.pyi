"""
Python から `import moqt._native` される拡張モジュール。
"""

from typing import final

@final
class _CoreEvent:
    """
    Sans I/O facade が Python 側へ返すイベント。
    """
    @property
    def code(self, /) -> int |None: ...
    @property
    def data(self, /) -> bytes |None: ...
    @property
    def kind(self, /) -> str: ...
    @property
    def reason(self, /) -> str |None: ...

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
    @property
    def established(self, /) -> bool:
        """
        SETUP 交換が完了しているかを返す。
        """
    def receive_control(self, /, data: bytes) -> list[_CoreEvent]:
        """
        peer 制御ストリームの断片を投入し、発生したイベントを返す。
        """
    @staticmethod
    def server(implementation: str = "moqt-py") -> _CoreSession:
        """
        server role の MoQT Session を作成する。
        """
    def start(self, /) -> bytes:
        """
        自側制御ストリームの stream type prefix と SETUP を返す。
        """
