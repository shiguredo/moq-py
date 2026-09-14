//! moqt-rs の Python バインディング。
//!
//! I/O は Python の webtransport-py が担当し、このモジュールは MoQT の SETUP
//! 制御ストリームを処理する Sans I/O facade だけを公開する。

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use shiguredo_moqt::error::MessageError;
use shiguredo_moqt::message::ControlMessage;
use shiguredo_moqt::parameter::{
    SETUP_OPTION_MOQT_IMPLEMENTATION, SetupOption, SetupOptionValue, SetupOptions,
};
use shiguredo_moqt::session::core::Session;
use shiguredo_moqt::session::types::{SessionEvent, Transport};
use shiguredo_moqt::stream::encode_control_stream_setup;
use shiguredo_moqt::varint;

/// Python から 1 回に渡せる制御ストリームデータの上限。
///
/// draft-ietf-moq-transport-21 の制御メッセージ長は u16 で表現されるため、
/// 1 メッセージの本文は最大 65535 バイトになる。未完成のメッセージを保持しても
/// 状態機械へ渡すのは完成したメッセージだけであり、この上限を超えるのは
/// peer が壊れたストリームを送り続けている場合に限られる。
const MAX_CONTROL_BUFFER_BYTES: usize = 128 * 1024;

/// `buf` の先頭から vi64 をデコードし `(値, 消費バイト数)` を返す。
///
/// 制御ストリーム先頭の stream type は vi64 である
/// (draft-ietf-moq-transport-21 §6.4.1 (Unidirectional Streams))。
/// バイト列が途中で切れている場合は `None` を返し、続きの到着を待つ。
fn decode_varint_prefix(buf: &[u8]) -> Result<Option<(u64, usize)>, MessageError> {
    match varint::decode(buf) {
        Ok((value, consumed)) => Ok(Some((value, consumed))),
        Err(MessageError::UnexpectedEof) => Ok(None),
        Err(error) => Err(error),
    }
}

/// `buf` の先頭から完成した制御メッセージを 1 件デコードし `(メッセージ, 消費バイト数)` を返す。
///
/// 制御メッセージは Type (vi64) + Length (u16 big-endian) + Message Body で構成される
/// (draft-ietf-moq-transport-21 §9 (Control Messages))。Type と Length を先に読み、
/// 本文全体が揃っている場合だけ `ControlMessage::decode` へ渡す。揃っていない場合は
/// `None` を返し、続きの到着を待つ。
fn decode_control_message(buf: &[u8]) -> Result<Option<(ControlMessage, usize)>, MessageError> {
    let Some((_, type_len)) = decode_varint_prefix(buf)? else {
        return Ok(None);
    };
    if buf.len() < type_len + 2 {
        return Ok(None);
    }
    let body_len = (usize::from(buf[type_len]) << 8) | usize::from(buf[type_len + 1]);
    if buf.len() < type_len + 2 + body_len {
        return Ok(None);
    }

    let (message, consumed) = ControlMessage::decode(buf)?;
    Ok(Some((message, consumed)))
}

/// Sans I/O facade が Python 側へ返すイベント。
#[pyclass(name = "_CoreEvent", frozen)]
pub(crate) struct CoreEvent {
    kind: &'static str,
    data: Option<Vec<u8>>,
    code: Option<u64>,
    reason: Option<String>,
}

impl CoreEvent {
    fn established() -> Self {
        Self {
            kind: "established",
            data: None,
            code: None,
            reason: None,
        }
    }

    fn send_control(data: Vec<u8>) -> Self {
        Self {
            kind: "send_control",
            data: Some(data),
            code: None,
            reason: None,
        }
    }

    fn close(code: u64, reason: &str) -> Self {
        Self {
            kind: "close",
            data: None,
            code: Some(code),
            reason: Some(reason.to_string()),
        }
    }
}

#[pymethods]
impl CoreEvent {
    #[getter]
    fn kind(&self) -> &'static str {
        self.kind
    }

    #[getter]
    fn data(&self, py: Python<'_>) -> Option<Py<PyBytes>> {
        self.data
            .as_ref()
            .map(|data| PyBytes::new(py, data).unbind())
    }

    #[getter]
    fn code(&self) -> Option<u64> {
        self.code
    }

    #[getter]
    fn reason(&self) -> Option<&str> {
        self.reason.as_deref()
    }
}

/// 1 本の WebTransport session に対応する MoQT Session facade。
#[pyclass(name = "_CoreSession")]
pub(crate) struct CoreSession {
    session: Session,
    /// peer 制御ストリームから受信済みで、まだデコードできていないバイト列。
    ///
    /// MoQT の制御ストリームは stream type varint に続けて制御メッセージが並ぶ。
    /// WebTransport の受信 fragment 境界はメッセージ境界と一致しないため、
    /// 完成したメッセージだけを取り出せるまでここへ蓄積する。
    control_buffer: Vec<u8>,
    peer_control_stream_type_received: bool,
    started: bool,
    established: bool,
}

impl CoreSession {
    fn new(client: bool, implementation: &str) -> PyResult<Self> {
        if implementation.is_empty() {
            return Err(PyValueError::new_err("implementation must not be empty"));
        }
        if implementation.len() > u16::MAX as usize {
            return Err(PyValueError::new_err(format!(
                "implementation is too long: expected at most {} bytes, got {} bytes",
                u16::MAX,
                implementation.len()
            )));
        }

        let mut options = SetupOptions::new();
        options.push(SetupOption {
            // draft-ietf-moq-transport-21 §9.1.5 (MOQT_IMPLEMENTATION)。
            // draft 由来の値であり、将来の改訂で変更される可能性がある。
            option_type: SETUP_OPTION_MOQT_IMPLEMENTATION,
            value: SetupOptionValue::Bytes(implementation.as_bytes().to_vec()),
        });

        let session = if client {
            Session::new_client(Transport::WebTransport, options)
        } else {
            Session::new_server(Transport::WebTransport, options)
        }
        .map_err(|error| PyRuntimeError::new_err(error.to_string()))?;

        Ok(Self {
            session,
            control_buffer: Vec::new(),
            peer_control_stream_type_received: false,
            started: false,
            established: false,
        })
    }

    fn drain_events(&mut self) -> PyResult<Vec<CoreEvent>> {
        let mut events = Vec::new();
        while let Some(event) = self.session.poll_event() {
            match event {
                SessionEvent::SendControl(message) => {
                    let data = message
                        .encode()
                        .map_err(|error| PyRuntimeError::new_err(error.to_string()))?;
                    events.push(CoreEvent::send_control(data));
                }
                SessionEvent::Established => {
                    self.established = true;
                    events.push(CoreEvent::established());
                }
                SessionEvent::CloseSession(error) => {
                    events.push(CoreEvent::close(error.code, error.reason));
                }
                other => {
                    return Err(PyRuntimeError::new_err(format!(
                        "unsupported session event during minimum SETUP implementation: {other:?}"
                    )));
                }
            }
        }
        Ok(events)
    }
}

#[pymethods]
impl CoreSession {
    /// client role の MoQT Session を作成する。
    #[staticmethod]
    #[pyo3(signature = (implementation="moqt-py"))]
    fn client(implementation: &str) -> PyResult<Self> {
        Self::new(true, implementation)
    }

    /// server role の MoQT Session を作成する。
    #[staticmethod]
    #[pyo3(signature = (implementation="moqt-py"))]
    fn server(implementation: &str) -> PyResult<Self> {
        Self::new(false, implementation)
    }

    /// 自側制御ストリームの stream type prefix と SETUP を返す。
    fn start(&mut self, py: Python<'_>) -> PyResult<Py<PyBytes>> {
        if self.started {
            return Err(PyRuntimeError::new_err("session has already started"));
        }

        let message = match self.session.poll_event() {
            Some(SessionEvent::SendControl(ControlMessage::Setup(setup))) => {
                ControlMessage::Setup(setup)
            }
            Some(other) => {
                return Err(PyRuntimeError::new_err(format!(
                    "expected initial SETUP event, got {other:?}"
                )));
            }
            None => return Err(PyRuntimeError::new_err("initial SETUP event is missing")),
        };
        let data = encode_control_stream_setup(&message)
            .map_err(|error| PyRuntimeError::new_err(error.to_string()))?;
        self.started = true;
        Ok(PyBytes::new(py, &data).unbind())
    }

    /// peer 制御ストリームの断片を投入し、発生したイベントを返す。
    fn receive_control(&mut self, data: &[u8]) -> PyResult<Vec<CoreEvent>> {
        if !self.started {
            return Err(PyRuntimeError::new_err("session has not started"));
        }

        let next_len = self
            .control_buffer
            .len()
            .checked_add(data.len())
            .ok_or_else(|| PyValueError::new_err("control stream buffer length overflow"))?;
        if next_len > MAX_CONTROL_BUFFER_BYTES {
            return Err(PyValueError::new_err(format!(
                "control stream buffer is too large: expected at most {MAX_CONTROL_BUFFER_BYTES} bytes, got {next_len} bytes"
            )));
        }
        self.control_buffer.extend_from_slice(data);

        if !self.peer_control_stream_type_received {
            let Some((stream_type, consumed)) = decode_varint_prefix(&self.control_buffer)
                .map_err(|error| PyRuntimeError::new_err(error.to_string()))?
            else {
                return Ok(Vec::new());
            };
            self.control_buffer.drain(..consumed);
            self.session
                .recv_control_stream_type(stream_type)
                .map_err(|error| PyRuntimeError::new_err(error.to_string()))?;
            self.peer_control_stream_type_received = true;
        }

        while let Some((message, consumed)) = decode_control_message(&self.control_buffer)
            .map_err(|error| PyRuntimeError::new_err(error.to_string()))?
        {
            self.control_buffer.drain(..consumed);
            self.session
                .recv_control(message)
                .map_err(|error| PyRuntimeError::new_err(error.to_string()))?;
        }

        self.drain_events()
    }

    /// SETUP 交換が完了しているかを返す。
    #[getter]
    fn established(&self) -> bool {
        self.established
    }
}

/// Python から `import moqt._native` される拡張モジュール。
#[pymodule(gil_used = false, name = "_native")]
mod _native {
    #[pymodule_export]
    use crate::{CoreEvent, CoreSession};
}
