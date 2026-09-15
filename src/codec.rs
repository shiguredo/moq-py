//! MoQT のコーデック層 (`moqt.moqt`)。
//!
//! セッション状態機械を介さずに、制御メッセージと varint を直接扱う。wire の
//! バイト列を組み立てて検証するテストや、実装が送出したバイト列を検査する
//! テストから使う。
//!
//! メッセージのエンコードは状態機械が担う。このモジュールはデコードと、
//! ストリーム種別・データグラム種別の判定だけを公開する。

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};

use shiguredo_moqt::message::ControlMessage;
use shiguredo_moqt::stream::{self, DataStreamType};
use shiguredo_moqt::varint;

use crate::core::{
    control_message_length, decode_varint_prefix as decode_varint_prefix_inner,
    message_body_to_python, message_kind, message_request_id,
};
use crate::errors::{codec_error, runtime_error};

/// Python 側へ渡す制御メッセージ 1 件。
///
/// メッセージ本体は種別ごとに異なる辞書であり、キーは
/// [draft-ietf-moq-transport-21 §9 (Control Messages)](https://datatracker.ietf.org/doc/draft-ietf-moq-transport/)
/// の各メッセージが運ぶフィールドに対応する。
#[pyclass(name = "Message", frozen)]
pub(crate) struct Message {
    /// メッセージ種別を表す文字列。
    kind: &'static str,
    /// wire 上のメッセージ Type (vi64)。
    type_id: u64,
    /// メッセージが運ぶ Request ID。応答メッセージはワイヤに Request ID を
    /// 含まないため `None` になる。
    request_id: Option<u64>,
    /// メッセージ本体。
    body: Py<PyDict>,
    /// デコードに使った生バイト列 (Type + Length + Message Body)。
    raw: Vec<u8>,
}

#[pymethods]
impl Message {
    /// メッセージ種別を表す文字列。
    ///
    /// `setup` / `goaway` / `subscribe` / `subscribe_ok` / `request_ok` /
    /// `request_error` / `request_update` / `publish` / `publish_done` /
    /// `publish_skipped` / `publish_state_notify` / `fetch` / `fetch_ok` /
    /// `track_status` / `publish_namespace` / `namespace` / `namespace_done` /
    /// `subscribe_namespace` / `subscribe_tracks` のいずれかである。
    #[getter]
    fn kind(&self) -> &'static str {
        self.kind
    }

    /// wire 上のメッセージ Type (vi64)。
    #[getter]
    fn type_id(&self) -> u64 {
        self.type_id
    }

    /// メッセージが運ぶ Request ID。
    ///
    /// 応答メッセージはワイヤに Request ID を含まないため `None` を返す。
    /// (draft-ietf-moq-transport-21 §9.4 (REQUEST_ERROR))
    #[getter]
    fn request_id(&self) -> Option<u64> {
        self.request_id
    }

    /// メッセージ本体。
    #[getter]
    fn body(&self, py: Python<'_>) -> Py<PyDict> {
        self.body.clone_ref(py)
    }

    /// メッセージが運ぶパラメータ。
    ///
    /// パラメータを持たないメッセージでは空の辞書を返す。
    #[getter]
    fn parameters(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let body = self.body.bind(py);
        match body.get_item("parameters")? {
            Some(value) => Ok(value.unbind()),
            None => Ok(PyDict::new(py).into_any().unbind()),
        }
    }

    /// デコードに使った生バイト列 (Type + Length + Message Body)。
    ///
    /// そのまま peer へ中継できる形である。
    #[getter]
    fn raw<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.raw)
    }

    fn __repr__(&self, py: Python<'_>) -> PyResult<String> {
        let mut parts = vec![
            format!("kind={}", self.kind),
            format!("type_id={:#x}", self.type_id),
        ];
        if let Some(request_id) = self.request_id {
            parts.push(format!("request_id={request_id}"));
        }
        parts.push(format!("body={}", self.body.bind(py).repr()?));
        Ok(format!("Message({})", parts.join(", ")))
    }

    fn __eq__(&self, py: Python<'_>, other: &Bound<'_, PyAny>) -> PyResult<bool> {
        let Ok(other) = other.cast::<Message>() else {
            return Ok(false);
        };
        let other = other.borrow();
        Ok(self.kind == other.kind
            && self.type_id == other.type_id
            && self.request_id == other.request_id
            && self.raw == other.raw
            && self.body.bind(py).eq(other.body.bind(py))?)
    }
}

/// 制御メッセージを 1 件デコードする。
///
/// 返り値は `(メッセージ, 消費バイト数)` である。`data` の先頭が制御メッセージの
/// 途中で切れている場合と、メッセージとして不正な場合は `ValueError` を送出する。
///
/// 制御メッセージは Type (vi64) + Length (u16 big-endian) + Message Body で構成される
/// (draft-ietf-moq-transport-21 §9 (Control Messages))。
#[pyfunction]
pub(crate) fn decode_message(py: Python<'_>, data: &[u8]) -> PyResult<(Message, usize)> {
    let Some((type_id, _)) = decode_varint_prefix_inner(data).map_err(codec_error)? else {
        return Err(PyValueError::new_err(format!(
            "incomplete control message: the type and length fields do not fit in {} bytes",
            data.len()
        )));
    };
    let Some(length) = control_message_length(data).map_err(codec_error)? else {
        return Err(PyValueError::new_err(format!(
            "incomplete control message: the type and length fields do not fit in {} bytes",
            data.len()
        )));
    };
    if data.len() < length {
        return Err(PyValueError::new_err(format!(
            "incomplete control message: expected {length} bytes, got {} bytes",
            data.len()
        )));
    }

    let (message, consumed) = ControlMessage::decode(data).map_err(codec_error)?;
    let body = message_body_to_python(py, &message)?;
    Ok((
        Message {
            kind: message_kind(&message),
            type_id,
            request_id: message_request_id(&message),
            body,
            raw: data[..consumed].to_vec(),
        },
        consumed,
    ))
}

/// vi64 をエンコードする (draft-ietf-moq-transport-21 §8.1 (Variable-Length Integers))。
///
/// 最小バイト数の表現を返す。
#[pyfunction]
pub(crate) fn encode_varint(value: u64) -> Vec<u8> {
    let mut buf = Vec::new();
    varint::encode(value, &mut buf);
    buf
}

/// 先頭の vi64 をデコードし `(値, 消費バイト数)` を返す。
///
/// 非最小エンコーディングも受理する。バイト列が途中で切れている場合は
/// `ValueError` を送出する。
#[pyfunction]
pub(crate) fn decode_varint(data: &[u8]) -> PyResult<(u64, usize)> {
    varint::decode(data).map_err(codec_error)
}

/// 先頭の vi64 をデコードし `(値, 消費バイト数)` を返す。
///
/// バイト列が途中で切れている場合は `None` を返し、続きの到着を待つ。
/// 非最小エンコーディングも受理する。
#[pyfunction]
pub(crate) fn decode_varint_prefix(data: &[u8]) -> PyResult<Option<(u64, usize)>> {
    decode_varint_prefix_inner(data).map_err(runtime_error)
}

/// stream type の varint が制御ストリームかデータストリームかを判定する。
///
/// データストリームの場合は種別を表す文字列を返す。制御ストリームと未知の値は
/// `None` を返す。
///
/// (draft-ietf-moq-transport-21 §6.4.1 (Unidirectional Streams) Table 3)
#[pyfunction]
pub(crate) fn classify_data_stream_type(type_id: u64) -> Option<&'static str> {
    stream::classify_data_stream_type(type_id).map(|stream_type| match stream_type {
        DataStreamType::Fetch => "fetch",
        DataStreamType::Subgroup => "subgroup",
        DataStreamType::Padding => "padding",
    })
}

/// 制御ストリームの stream type を返す。
#[pyfunction]
pub(crate) fn setup_stream_type() -> u64 {
    stream::SETUP_STREAM_TYPE
}

/// データグラムの種別がパディングかを判定する。
///
/// データグラムは stream type を持たないため、先頭の varint で判定する。
#[pyfunction]
pub(crate) fn is_padding_datagram(data: &[u8]) -> PyResult<bool> {
    match decode_varint_prefix_inner(data).map_err(runtime_error)? {
        Some((type_id, _)) => Ok(type_id == stream::PADDING_DATAGRAM_TYPE),
        None => Ok(false),
    }
}
