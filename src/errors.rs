//! ライブラリのエラーを Python の例外へ変換する。
//!
//! 例外型は失敗の種類で分ける。
//!
//! - `RuntimeError`: セッションの状態や操作が原因の失敗
//! - `ValueError`: 呼び出し側が渡したバイト列や値そのものが不正である失敗

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;

/// セッションの操作が原因の失敗を `RuntimeError` へ変換する。
pub(crate) fn runtime_error(error: impl std::fmt::Display) -> PyErr {
    PyRuntimeError::new_err(error.to_string())
}

/// 入力のバイト列や値そのものが不正である失敗を `ValueError` へ変換する。
pub(crate) fn codec_error(error: impl std::fmt::Display) -> PyErr {
    PyValueError::new_err(error.to_string())
}
