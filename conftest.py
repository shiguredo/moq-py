"""moqt-py 自身のテストで使う pytest 設定。

公開している `moqt.moq.testing` の fixture を、利用者と同じ手順で読み込む。
"""

pytest_plugins = ["moqt.moq.testing"]
