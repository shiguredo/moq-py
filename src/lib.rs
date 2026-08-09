//! moqt-rs の Python バインディング。
//!
//! I/O は Python の webtransport-py が担当し、このモジュールは MoQT の SETUP
//! 制御ストリームを処理する Sans I/O facade だけを公開する。

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use shiguredo_moqt::decoder::MessageDecoder;
use shiguredo_moqt::stream::encode_control_stream_setup;
use shiguredo_moqt::{
    ControlMessage, Session, SessionEvent, SetupOption, SetupOptionValue, SetupOptions, Transport,
};

/// Python から 1 回に渡せる制御ストリームデータの上限。
///
/// draft-ietf-moq-transport-19 の制御メッセージ長は u16 で表現される。
/// partial message と次のメッセージを同時に保持できる余裕を含めて 128 KiB に制限する。
const MAX_CONTROL_BUFFER_BYTES: usize = 128 * 1024;

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
    control_decoder: MessageDecoder,
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
            // draft-ietf-moq-transport-19 §10.3.1.5 (MOQT_IMPLEMENTATION)。
            // draft 由来の値であり、将来の改訂で変更される可能性がある。
            option_type: 0x07,
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
            control_decoder: MessageDecoder::new(),
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

        let buffered_len = self.control_decoder.buffered_len();
        let next_len = buffered_len
            .checked_add(data.len())
            .ok_or_else(|| PyValueError::new_err("control stream buffer length overflow"))?;
        if next_len > MAX_CONTROL_BUFFER_BYTES {
            return Err(PyValueError::new_err(format!(
                "control stream buffer is too large: expected at most {MAX_CONTROL_BUFFER_BYTES} bytes, got {next_len} bytes"
            )));
        }
        self.control_decoder.push(data);

        if !self.peer_control_stream_type_received {
            let Some(stream_type) = self
                .control_decoder
                .try_decode_varint()
                .map_err(|error| PyRuntimeError::new_err(error.to_string()))?
            else {
                return Ok(Vec::new());
            };
            self.session
                .recv_control_stream_type(stream_type)
                .map_err(|error| PyRuntimeError::new_err(error.to_string()))?;
            self.peer_control_stream_type_received = true;
        }

        while let Some(message) = self
            .control_decoder
            .try_decode_message()
            .map_err(|error| PyRuntimeError::new_err(error.to_string()))?
        {
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
