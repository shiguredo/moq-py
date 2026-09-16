//! WebTransport の I/O と MoQT の状態機械を接続する facade。
//!
//! I/O は Python の webtransport-py が担当し、このモジュールは MoQT の Sans I/O
//! セッション状態機械を Python から駆動する facade を公開する。
//!
//! # 役割分担
//!
//! - Python 側: ストリームの開設・送信・終了、datagram の送受信、イベントループ
//! - Rust 側: メッセージと data stream のデコード、プロトコル状態機械の駆動
//!
//! Python 側は peer から届いたストリームの種別 (制御・request・data) を判定して
//! 対応する `receive_*` を呼ぶ。Rust 側は自側が送るべきバイト列をイベントとして
//! 返し、ストリームの実体には触れない。
//!
//! `moqt.moqt` が公開する `Session` と `Event` はこのモジュールの [`CoreSession`] と
//! [`CoreEvent`] である。

use std::collections::{HashMap, HashSet};

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};

use crate::errors::{codec_error, runtime_error};
use shiguredo_moqt::decoder::MessageDecoder;
use shiguredo_moqt::error::MessageError;
use shiguredo_moqt::message::common::{Location, TrackNamespace};
use shiguredo_moqt::message::{
    ControlMessage, FetchOk, Publish, PublishDone, PublishStateNotify, Redirect, RequestError,
    RequestOk, RequestUpdate, Subscribe, SubscribeOk, TrackStatus,
};
use shiguredo_moqt::message_parameter::{
    AuthorizationToken, MessageParameter, MessageParameterValue, MessageParameters,
    PARAM_AUTHORIZATION_TOKEN, PARAM_FILL_PARAMETERS, PARAM_FORWARD, PARAM_GROUP_ORDER,
    PARAM_INCLUDE_PROPERTIES, PARAM_LARGEST_OBJECT, PARAM_LOCATION_FILTER,
    PARAM_OBJECT_PROPERTY_FILTER, PARAM_OBJECTID_FILTER, PARAM_PRIORITY_FILTER,
    PARAM_SUBGROUP_FILTER, PARAM_SUBSCRIBER_PRIORITY, PARAM_TRACK_NAMESPACE_PREFIX,
    PARAM_TRACK_PROPERTY_FILTER,
};
use shiguredo_moqt::parameter::{
    SETUP_OPTION_AUTHORIZATION_TOKEN, SETUP_OPTION_MOQT_IMPLEMENTATION, SetupOption,
    SetupOptionValue, SetupOptions,
};
use shiguredo_moqt::session::core::Session;
use shiguredo_moqt::session::types::{
    DEFAULT_PUBLISHER_GROUP_ORDER_ASCENDING, DataStreamId, DatagramAcceptance, RequestKind,
    RequestStreamEnd, SessionEvent, SessionState, TerminationReason, TrackDataAcceptance,
    Transport,
};
use shiguredo_moqt::stream::DataStreamType;
use shiguredo_moqt::stream::datagram::ObjectDatagram;
use shiguredo_moqt::stream::decoder::{
    DecodedFetchEntry, DecodedSubgroupObject, FetchStreamDecoder, SubgroupStreamDecoder,
};
use shiguredo_moqt::stream::encode_control_stream_setup;
use shiguredo_moqt::stream::subgroup::{SubgroupHeader, SubgroupIdMode};
use shiguredo_moqt::track_properties::{TrackProperties, TrackProperty, TrackPropertyValue};
use shiguredo_moqt::varint;

/// ストリーム 1 本あたりに保持する未完成データの上限。
///
/// 制御メッセージの本文は u16 長、サブグループのヘッダとオブジェクトは vi64 長で
/// 表現される。完成したメッセージだけを状態機械へ渡す限りこの上限には達しないため、
/// 上限を超えるのは peer が壊れたストリームを送り続けている場合に限られる。
const MAX_STREAM_BUFFER_BYTES: usize = 128 * 1024;

/// `buf` の先頭から vi64 をデコードし `(値, 消費バイト数)` を返す。
///
/// バイト列が途中で切れている場合は `None` を返し、続きの到着を待つ。
pub(crate) fn decode_varint_prefix(buf: &[u8]) -> Result<Option<(u64, usize)>, MessageError> {
    match varint::decode(buf) {
        Ok((value, consumed)) => Ok(Some((value, consumed))),
        Err(MessageError::UnexpectedEof) => Ok(None),
        Err(error) => Err(error),
    }
}

/// `buf` の先頭にある制御メッセージの全長を返す。
///
/// 制御メッセージは Type (vi64) + Length (u16 big-endian) + Message Body で構成される
/// (draft-ietf-moq-transport-21 §9 (Control Messages))。長さを決めるヘッダが
/// 途中で切れている場合は `None` を返し、続きの到着を待つ。
pub(crate) fn control_message_length(buf: &[u8]) -> Result<Option<usize>, MessageError> {
    let Some((_, type_len)) = decode_varint_prefix(buf)? else {
        return Ok(None);
    };
    if buf.len() < type_len + 2 {
        return Ok(None);
    }
    let body_len = (usize::from(buf[type_len]) << 8) | usize::from(buf[type_len + 1]);
    Ok(Some(type_len + 2 + body_len))
}

/// `buf` の先頭から完成した制御メッセージを 1 件デコードし `(メッセージ, 消費バイト数)` を返す。
///
/// 本文全体が揃っている場合だけ `ControlMessage::decode` へ渡す。揃っていない場合は
/// `None` を返し、続きの到着を待つ。
pub(crate) fn decode_control_message(
    buf: &[u8],
) -> Result<Option<(ControlMessage, usize)>, MessageError> {
    match control_message_length(buf)? {
        Some(length) if buf.len() >= length => {
            let (message, consumed) = ControlMessage::decode(buf)?;
            Ok(Some((message, consumed)))
        }
        _ => Ok(None),
    }
}

/// ストリーム ID ごとの受信バッファ。
///
/// WebTransport の受信 fragment 境界はメッセージ境界と一致しないため、完成した
/// メッセージだけを取り出せるまでバイト列を蓄積する。
#[derive(Default)]
struct StreamBuffers {
    buffers: HashMap<u64, Vec<u8>>,
}

impl StreamBuffers {
    /// 断片を追加し、上限を超える場合はエラーを返す。
    fn push(&mut self, stream_id: u64, data: &[u8]) -> PyResult<()> {
        let buffer = self.buffers.entry(stream_id).or_default();
        let next_len = buffer
            .len()
            .checked_add(data.len())
            .ok_or_else(|| PyValueError::new_err("stream buffer length overflow"))?;
        if next_len > MAX_STREAM_BUFFER_BYTES {
            return Err(PyValueError::new_err(format!(
                "stream buffer is too large: expected at most {MAX_STREAM_BUFFER_BYTES} bytes, got {next_len} bytes for stream {stream_id}"
            )));
        }
        buffer.extend_from_slice(data);
        Ok(())
    }

    /// バッファへの参照を返す。
    fn get(&self, stream_id: u64) -> &[u8] {
        self.buffers.get(&stream_id).map_or(&[], Vec::as_slice)
    }

    /// デコード済みのバイト列をバッファの先頭から取り除く。
    fn consume(&mut self, stream_id: u64, consumed: usize) {
        if let Some(buffer) = self.buffers.get_mut(&stream_id) {
            buffer.drain(..consumed);
        }
    }

    /// バッファを破棄する。
    fn remove(&mut self, stream_id: u64) {
        self.buffers.remove(&stream_id);
    }
}

/// Python 側から渡された Track Namespace をライブラリの型へ変換する。
pub(crate) fn track_namespace_from_python(fields: Vec<Vec<u8>>) -> PyResult<TrackNamespace> {
    TrackNamespace::new(fields).map_err(|error| PyValueError::new_err(error.to_string()))
}

/// Python 側から渡された reason phrase をライブラリの型へ変換する。
fn reason_from_python(reason: &str) -> PyResult<shiguredo_moqt::message::ReasonPhrase> {
    shiguredo_moqt::message::ReasonPhrase::new(reason)
        .map_err(|error| PyValueError::new_err(error.to_string()))
}

/// パラメータ型に応じて Python 側の値をライブラリの型へ変換する。
///
/// `LOCATION_FILTER` などの構造化された値は、ライブラリが解釈できる
/// エンコード済みバイト列として受け取る。
pub(crate) fn parameter_value_from_python(
    param_type: u64,
    value: &Bound<'_, PyAny>,
) -> PyResult<MessageParameterValue> {
    match param_type {
        // uint8 で表現するパラメータ (draft-ietf-moq-transport-21 §9.20)
        PARAM_FORWARD
        | PARAM_SUBSCRIBER_PRIORITY
        | PARAM_GROUP_ORDER
        | PARAM_INCLUDE_PROPERTIES => Ok(MessageParameterValue::Uint8(value.extract::<u8>()?)),
        // Length-prefixed バイト列で表現するパラメータ
        PARAM_LOCATION_FILTER
        | PARAM_SUBGROUP_FILTER
        | PARAM_OBJECTID_FILTER
        | PARAM_PRIORITY_FILTER
        | PARAM_OBJECT_PROPERTY_FILTER
        | PARAM_TRACK_PROPERTY_FILTER => Ok(MessageParameterValue::LengthPrefixed(
            value.extract::<Vec<u8>>()?,
        )),
        // Track Namespace で表現するパラメータ
        PARAM_TRACK_NAMESPACE_PREFIX => Ok(MessageParameterValue::TrackNamespacePrefix(
            track_namespace_from_python(value.extract::<Vec<Vec<u8>>>()?)?,
        )),
        // Location (Group ID + Object ID) で表現するパラメータ
        PARAM_LARGEST_OBJECT => {
            let (group, object) = value.extract::<(u64, u64)>()?;
            Ok(MessageParameterValue::Location { group, object })
        }
        // AUTHORIZATION_TOKEN (draft-ietf-moq-transport-21 §8.9)
        PARAM_AUTHORIZATION_TOKEN => {
            let (token_type, token_value) = value.extract::<(u64, Vec<u8>)>()?;
            Ok(MessageParameterValue::AuthorizationToken(
                AuthorizationToken::UseValue {
                    token_type,
                    token_value,
                },
            ))
        }
        // FILL_PARAMETERS の内側パラメータ群 (draft-ietf-moq-transport-21 §9.20.16)
        PARAM_FILL_PARAMETERS => Ok(MessageParameterValue::FillParameters(
            message_parameters_from_python(value)?,
        )),
        // それ以外は vi64 として扱う
        _ => Ok(MessageParameterValue::VarInt(value.extract::<u64>()?)),
    }
}

/// Python 側の辞書から `MessageParameters` を構築する。
///
/// キーはパラメータ型、値は型ごとの表現である。AUTHORIZATION_TOKEN は複数回
/// 指定できるため、値にリストを渡した場合は同じ型を複数回追加する。
pub(crate) fn message_parameters_from_python(
    value: &Bound<'_, PyAny>,
) -> PyResult<MessageParameters> {
    let dict = value.cast::<PyDict>()?;
    let mut parameters = MessageParameters::new();
    for (key, item) in dict.iter() {
        let param_type = key.extract::<u64>()?;

        if param_type == PARAM_AUTHORIZATION_TOKEN
            && let Ok(tokens) = item.cast::<PyList>()
        {
            for token in tokens.iter() {
                parameters.push(MessageParameter {
                    param_type,
                    value: parameter_value_from_python(param_type, &token)?,
                });
            }
            continue;
        }

        parameters.push(MessageParameter {
            param_type,
            value: parameter_value_from_python(param_type, &item)?,
        });
    }
    Ok(parameters)
}

/// `MessageParameters` を Python 側の辞書へ変換する。
///
/// 値はパラメータ 1 件分のエンコード済みバイト列である。Python 側で解釈する
/// 場合は `_decode_parameter` を使う。
pub(crate) fn message_parameters_to_python(
    py: Python<'_>,
    parameters: &MessageParameters,
) -> PyResult<Py<PyDict>> {
    let dict = PyDict::new(py);
    for parameter in parameters.as_slice() {
        let value = PyBytes::new(py, &encode_parameter_value(parameter)).into_any();
        // AUTHORIZATION_TOKEN は複数回出現できるためリストで保持する
        if parameter.param_type == PARAM_AUTHORIZATION_TOKEN {
            match dict.get_item(parameter.param_type)? {
                Some(existing) => existing.cast::<PyList>()?.append(value)?,
                None => {
                    let list = PyList::empty(py);
                    list.append(value)?;
                    dict.set_item(parameter.param_type, list)?;
                }
            }
            continue;
        }
        dict.set_item(parameter.param_type, value)?;
    }
    Ok(dict.unbind())
}

/// パラメータ 1 件を値部分だけのバイト列へエンコードする。
///
/// `MessageParameterValue` のエンコード関数は公開されていないため、
/// パラメータ 1 件だけのリストを encode / decode して取り出す。
fn encode_parameter_value(parameter: &MessageParameter) -> Vec<u8> {
    let mut parameters = MessageParameters::new();
    parameters.push(parameter.clone());
    let mut buf = Vec::new();
    if parameters.encode(&mut buf).is_err() {
        return Vec::new();
    }
    // 先頭はパラメータ数 (vi64)、続いて KVP の delta key (vi64) である。
    // パラメータ 1 件だけのリストでは delta key は 0 になる
    let mut offset = 0;
    for _ in 0..2 {
        match varint::decode(&buf[offset..]) {
            Ok((_, consumed)) => offset += consumed,
            Err(_) => return Vec::new(),
        }
    }
    buf[offset..].to_vec()
}

/// Python 側の辞書から `TrackProperties` を構築する。
fn track_properties_from_python(value: &Bound<'_, PyAny>) -> PyResult<TrackProperties> {
    let dict = value.cast::<PyDict>()?;
    let mut properties = TrackProperties::new();
    for (key, item) in dict.iter() {
        let prop_type = key.extract::<u64>()?;
        // 偶数型は varint、奇数型は長さ付きバイト列で表現する
        let prop_value = if prop_type % 2 == 1 {
            TrackPropertyValue::Bytes(item.extract::<Vec<u8>>()?)
        } else {
            TrackPropertyValue::VarInt(item.extract::<u64>()?)
        };
        properties.push(TrackProperty {
            prop_type,
            value: prop_value,
        });
    }
    Ok(properties)
}

/// `TrackProperties` を Python 側の辞書へ変換する。
fn track_properties_to_python(
    py: Python<'_>,
    properties: &TrackProperties,
) -> PyResult<Py<PyDict>> {
    let dict = PyDict::new(py);
    for property in properties.as_slice() {
        match &property.value {
            TrackPropertyValue::VarInt(value) => dict.set_item(property.prop_type, *value)?,
            TrackPropertyValue::Bytes(value) => {
                dict.set_item(property.prop_type, PyBytes::new(py, value))?
            }
        }
    }
    Ok(dict.unbind())
}

/// Track Namespace を Python 側のフィールド列へ変換する。
pub(crate) fn track_namespace_to_python(
    py: Python<'_>,
    namespace: &TrackNamespace,
) -> PyResult<Py<PyList>> {
    let fields = PyList::empty(py);
    for field in namespace.fields() {
        fields.append(PyBytes::new(py, field))?;
    }
    Ok(fields.unbind())
}

/// パラメータ 1 件分のエンコード済みバイト列をデコードする。
///
/// `value` はパラメータの値部分だけのバイト列である。型と値形式の対応は
/// パラメータ型ごとに決まっているため、型を付けた 1 件のリストとしてデコードする。
/// (draft-ietf-moq-transport-21 §9.20 (Control Message Parameters))
pub(crate) fn decode_parameter_entry(param_type: u64, value: &[u8]) -> PyResult<MessageParameter> {
    // パラメータ 1 件だけのリストを組み立てる。KVP の型は直前の型との差分であり、
    // 先頭の直前の型は 0 である (draft-ietf-moq-transport-21 §8.3 (Key-Value-Pair Structure))
    let mut buf = Vec::new();
    varint::encode(1, &mut buf);
    varint::encode(param_type, &mut buf);
    buf.extend_from_slice(value);
    let (parameters, _consumed) = MessageParameters::decode(&buf).map_err(codec_error)?;
    parameters
        .as_slice()
        .first()
        .cloned()
        .ok_or_else(|| PyValueError::new_err("parameter could not be decoded"))
}

/// パラメータ 1 件を Python 側の値へ変換する。
///
/// `value` はパラメータの値部分だけのバイト列である。
pub(crate) fn decode_parameter_to_python(
    py: Python<'_>,
    param_type: u64,
    value: &[u8],
) -> PyResult<Py<PyAny>> {
    let parameter = decode_parameter_entry(param_type, value)?;
    parameter_value_to_python(py, &parameter.value)
}

/// パラメータの値を Python 側の値へ変換する。
///
/// `MessageParameterValue` をそのまま解釈した結果を返す。アプリは
/// `Event.parameters` の生バイトをこの関数で解釈する。
pub(crate) fn parameter_value_to_python(
    py: Python<'_>,
    value: &MessageParameterValue,
) -> PyResult<Py<PyAny>> {
    match value {
        MessageParameterValue::VarInt(value) => Ok(value.into_pyobject(py)?.into_any().unbind()),
        MessageParameterValue::Uint8(value) => Ok(value.into_pyobject(py)?.into_any().unbind()),
        MessageParameterValue::LengthPrefixed(value) => {
            Ok(PyBytes::new(py, value).into_any().unbind())
        }
        MessageParameterValue::Location { group, object } => {
            Ok((*group, *object).into_pyobject(py)?.into_any().unbind())
        }
        MessageParameterValue::TrackNamespacePrefix(namespace) => {
            Ok(track_namespace_to_python(py, namespace)?.into_any())
        }
        MessageParameterValue::AuthorizationToken(token) => {
            let dict = PyDict::new(py);
            let (kind, token_type, token_value) = match token {
                AuthorizationToken::UseValue {
                    token_type,
                    token_value,
                } => ("use_value", Some(*token_type), Some(token_value.as_slice())),
                AuthorizationToken::UseAlias { alias } => ("use_alias", Some(*alias), None),
                AuthorizationToken::Register {
                    alias,
                    token_type,
                    token_value,
                } => {
                    let _ = alias;
                    ("register", Some(*token_type), Some(token_value.as_slice()))
                }
                AuthorizationToken::Delete { alias } => ("delete", Some(*alias), None),
            };
            dict.set_item("kind", kind)?;
            match token_type {
                Some(value) => dict.set_item("token_type", value)?,
                None => dict.set_item("token_type", py.None())?,
            }
            match token_value {
                Some(value) => dict.set_item("token_value", PyBytes::new(py, value))?,
                None => dict.set_item("token_value", py.None())?,
            }
            Ok(dict.into_any().unbind())
        }
        MessageParameterValue::FillParameters(parameters) => {
            Ok(message_parameters_to_python(py, parameters)?.into_any())
        }
    }
}

/// メッセージ種別を表す文字列を返す。
pub(crate) fn message_kind(message: &ControlMessage) -> &'static str {
    match message {
        ControlMessage::Setup(_) => "setup",
        ControlMessage::Goaway(_) => "goaway",
        ControlMessage::RequestOk(_) => "request_ok",
        ControlMessage::RequestError(_) => "request_error",
        ControlMessage::Subscribe(_) => "subscribe",
        ControlMessage::SubscribeOk(_) => "subscribe_ok",
        ControlMessage::RequestUpdate(_) => "request_update",
        ControlMessage::Publish(_) => "publish",
        ControlMessage::PublishDone(_) => "publish_done",
        ControlMessage::PublishStateNotify(_) => "publish_state_notify",
        ControlMessage::Fetch(_) => "fetch",
        ControlMessage::FetchOk(_) => "fetch_ok",
        ControlMessage::TrackStatus(_) => "track_status",
    }
}

/// メッセージが運ぶ Request ID を返す。
///
/// 応答メッセージはワイヤに Request ID を含まないため `None` を返す。
pub(crate) fn message_request_id(message: &ControlMessage) -> Option<u64> {
    match message {
        ControlMessage::Subscribe(m) => Some(m.request_id),
        ControlMessage::RequestUpdate(m) => Some(m.request_id),
        ControlMessage::Publish(m) => Some(m.request_id),
        ControlMessage::Fetch(m) => Some(m.request_id),
        ControlMessage::TrackStatus(m) => Some(m.request_id),
        _ => None,
    }
}

/// `RequestKind` を Python 側の文字列へ変換する。
fn request_kind_to_python(kind: RequestKind) -> &'static str {
    match kind {
        RequestKind::Subscribe => "subscribe",
        RequestKind::Publish => "publish",
        RequestKind::Fetch => "fetch",
        RequestKind::TrackStatus => "track_status",
    }
}

/// `TerminationReason` を Python 側の辞書へ変換する。
fn termination_reason_to_python(
    py: Python<'_>,
    reason: &TerminationReason,
) -> PyResult<Py<PyDict>> {
    let dict = PyDict::new(py);
    match reason {
        TerminationReason::PeerStreamFin => {
            dict.set_item("kind", "peer_stream_fin")?;
        }
        TerminationReason::PeerStreamReset { error_code } => {
            dict.set_item("kind", "peer_stream_reset")?;
            dict.set_item("error_code", *error_code)?;
        }
        TerminationReason::LocalCancel => {
            dict.set_item("kind", "local_cancel")?;
        }
        TerminationReason::SupersededByPublish { new_request_id } => {
            dict.set_item("kind", "superseded_by_publish")?;
            dict.set_item("new_request_id", *new_request_id)?;
        }
        TerminationReason::MalformedTrack { reason } => {
            dict.set_item("kind", "malformed_track")?;
            dict.set_item("reason", *reason)?;
        }
    }
    Ok(dict.unbind())
}

/// `TrackDataAcceptance` を Python 側の文字列へ変換する。
fn track_data_acceptance_to_python(acceptance: TrackDataAcceptance) -> &'static str {
    match acceptance {
        TrackDataAcceptance::Accepted => "accepted",
        TrackDataAcceptance::UnknownTrackAlias => "unknown_track_alias",
        TrackDataAcceptance::Discarded => "discarded",
        TrackDataAcceptance::FilteredOut => "filtered_out",
    }
}

/// REQUEST_ERROR の内容を辞書へ書き出す。
fn request_error_to_python(
    py: Python<'_>,
    dict: &Bound<'_, PyDict>,
    error: &RequestError,
) -> PyResult<()> {
    dict.set_item("error_code", error.error_code)?;
    dict.set_item("retry_interval", error.retry_interval)?;
    dict.set_item("reason", error.reason.as_str())?;
    match &error.redirect {
        Some(redirect) => {
            let value = PyDict::new(py);
            value.set_item("connect_uri", PyBytes::new(py, &redirect.connect_uri))?;
            value.set_item(
                "track_namespace",
                track_namespace_to_python(py, &redirect.track_namespace)?,
            )?;
            value.set_item("track_name", PyBytes::new(py, &redirect.track_name))?;
            dict.set_item("redirect", value)?;
        }
        None => dict.set_item("redirect", py.None())?,
    }
    Ok(())
}

/// Setup Options を Python 側の辞書へ変換する。
///
/// キーは Setup Option Type、値は偶数型なら `int`、奇数型なら `bytes` である。
/// AUTHORIZATION_TOKEN (0x03) は Token 構造を持つため辞書になり、SETUP では
/// 複数指定できるためリストで返す。
/// (draft-ietf-moq-transport-21 §9.1 (SETUP) / §16.4 (Setup Options))
///
/// `SetupOptions` は列挙 API を持たないため、エンコード結果を走査する。
fn setup_options_to_python(py: Python<'_>, options: &SetupOptions) -> PyResult<Py<PyDict>> {
    let dict = PyDict::new(py);

    // AUTHORIZATION_TOKEN は Token 構造であり生バイト列では意味を成さないため、
    // moqt-rs の accessor から取り出して Token の辞書にする
    let tokens = options.authorization_tokens();
    if !tokens.is_empty() {
        let list = PyList::empty(py);
        for token in tokens {
            let token = parameter_value_to_python(
                py,
                &MessageParameterValue::AuthorizationToken(token.clone()),
            )?;
            list.append(token)?;
        }
        dict.set_item(SETUP_OPTION_AUTHORIZATION_TOKEN, list)?;
    }

    // SETUP の本体は delta-key エンコードされた KVP 列である
    // (draft-ietf-moq-transport-21 §9.1 (SETUP))。偶数型は varint、
    // 奇数型は長さ付きバイト列として読む
    let mut buf = Vec::new();
    options.encode(&mut buf).map_err(runtime_error)?;

    let mut offset = 0;
    let mut previous_type = 0u64;
    while offset < buf.len() {
        let (delta, consumed) = varint::decode(&buf[offset..]).map_err(codec_error)?;
        offset += consumed;
        let option_type = previous_type
            .checked_add(delta)
            .ok_or_else(|| codec_error("setup option type overflow"))?;
        previous_type = option_type;

        if option_type.is_multiple_of(2) {
            let (value, consumed) = varint::decode(&buf[offset..]).map_err(codec_error)?;
            offset += consumed;
            dict.set_item(option_type, value)?;
            continue;
        }

        let (length, consumed) = varint::decode(&buf[offset..]).map_err(codec_error)?;
        offset += consumed;
        let length =
            usize::try_from(length).map_err(|_| codec_error("setup option length is too large"))?;
        let end = offset
            .checked_add(length)
            .ok_or_else(|| codec_error("setup option length is too large"))?;
        if buf.len() < end {
            return Err(codec_error(
                "setup option length exceeds the remaining bytes",
            ));
        }
        // AUTHORIZATION_TOKEN は Token 構造として別途取り出し済みである
        if option_type != SETUP_OPTION_AUTHORIZATION_TOKEN {
            dict.set_item(option_type, PyBytes::new(py, &buf[offset..end]))?;
        }
        offset = end;
    }

    Ok(dict.unbind())
}

/// Python 側の値から Setup Option の値を作る。
///
/// 偶数型の Setup Option は varint、奇数型は長さ付きバイト列で表現する
/// (draft-ietf-moq-transport-21 §16.4 (Setup Options))。AUTHORIZATION_TOKEN (0x03)
/// だけは Token 構造を持つ
/// (draft-ietf-moq-transport-21 §9.1.4 (AUTHORIZATION TOKEN))。
fn setup_option_value_from_python(
    option_type: u64,
    value: &Bound<'_, PyAny>,
) -> PyResult<SetupOptionValue> {
    if option_type == SETUP_OPTION_AUTHORIZATION_TOKEN {
        return Ok(SetupOptionValue::AuthorizationToken(
            authorization_token_from_python(value)?,
        ));
    }
    if option_type.is_multiple_of(2) {
        return Ok(SetupOptionValue::VarInt(value.extract::<u64>()?));
    }
    Ok(SetupOptionValue::Bytes(value.extract::<Vec<u8>>()?))
}

/// Python 側の値から AUTHORIZATION_TOKEN の Token 構造を作る。
///
/// `kind` で種別を指定する辞書と、`(token_type, token_value)` のタプルを受け付ける。
/// 種別は draft-ietf-moq-transport-21 §8.9 (Authorization Token Compression) の
/// DELETE / REGISTER / USE_ALIAS / USE_VALUE である。
fn authorization_token_from_python(value: &Bound<'_, PyAny>) -> PyResult<AuthorizationToken> {
    let Ok(dict) = value.cast::<PyDict>() else {
        // Alias を使わない USE_VALUE はタプルでも指定できる
        let (token_type, token_value) = value.extract::<(u64, Vec<u8>)>()?;
        return Ok(AuthorizationToken::UseValue {
            token_type,
            token_value,
        });
    };

    let kind = token_str_item(dict, "kind")?;
    match kind.as_str() {
        "delete" => Ok(AuthorizationToken::Delete {
            alias: token_varint_item(dict, "alias")?,
        }),
        "register" => Ok(AuthorizationToken::Register {
            alias: token_varint_item(dict, "alias")?,
            token_type: token_varint_item(dict, "token_type")?,
            token_value: token_bytes_item(dict, "token_value")?,
        }),
        "use_alias" => Ok(AuthorizationToken::UseAlias {
            alias: token_varint_item(dict, "alias")?,
        }),
        "use_value" => Ok(AuthorizationToken::UseValue {
            token_type: token_varint_item(dict, "token_type")?,
            token_value: token_bytes_item(dict, "token_value")?,
        }),
        other => Err(PyValueError::new_err(format!(
            "unknown authorization token kind: {other}"
        ))),
    }
}

/// AUTHORIZATION_TOKEN の辞書から必須の種別名を取り出す。
fn token_str_item(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<String> {
    let item = dict
        .get_item(key)?
        .ok_or_else(|| PyValueError::new_err(format!("authorization token requires {key}")))?;
    item.extract::<String>()
}

/// AUTHORIZATION_TOKEN の辞書から必須の varint 値を取り出す。
fn token_varint_item(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<u64> {
    let item = dict
        .get_item(key)?
        .ok_or_else(|| PyValueError::new_err(format!("authorization token requires {key}")))?;
    item.extract::<u64>()
}

/// AUTHORIZATION_TOKEN の辞書から必須のバイト列を取り出す。
fn token_bytes_item(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Vec<u8>> {
    let item = dict
        .get_item(key)?
        .ok_or_else(|| PyValueError::new_err(format!("authorization token requires {key}")))?;
    item.extract::<Vec<u8>>()
}

/// 制御メッセージを Python 側の辞書へ変換する。
pub(crate) fn message_body_to_python(
    py: Python<'_>,
    message: &ControlMessage,
) -> PyResult<Py<PyDict>> {
    let dict = PyDict::new(py);
    match message {
        ControlMessage::Setup(setup) => {
            dict.set_item("options", setup_options_to_python(py, &setup.options)?)?;
        }
        ControlMessage::Goaway(goaway) => {
            dict.set_item("new_session_uri", PyBytes::new(py, &goaway.new_session_uri))?;
            dict.set_item("timeout", goaway.timeout)?;
        }
        ControlMessage::RequestOk(RequestOk {
            parameters,
            track_properties,
        }) => {
            dict.set_item("parameters", message_parameters_to_python(py, parameters)?)?;
            dict.set_item(
                "track_properties",
                track_properties_to_python(py, track_properties)?,
            )?;
        }
        ControlMessage::RequestError(error) => request_error_to_python(py, &dict, error)?,
        ControlMessage::Subscribe(Subscribe {
            request_id,
            track_namespace,
            track_name,
            parameters,
        }) => {
            dict.set_item("request_id", *request_id)?;
            dict.set_item(
                "track_namespace",
                track_namespace_to_python(py, track_namespace)?,
            )?;
            dict.set_item("track_name", PyBytes::new(py, track_name))?;
            dict.set_item("parameters", message_parameters_to_python(py, parameters)?)?;
        }
        ControlMessage::SubscribeOk(SubscribeOk {
            track_alias,
            parameters,
            track_properties,
        }) => {
            dict.set_item("track_alias", *track_alias)?;
            dict.set_item("parameters", message_parameters_to_python(py, parameters)?)?;
            dict.set_item(
                "track_properties",
                track_properties_to_python(py, track_properties)?,
            )?;
        }
        ControlMessage::RequestUpdate(RequestUpdate {
            request_id,
            parameters,
        }) => {
            dict.set_item("request_id", *request_id)?;
            dict.set_item("parameters", message_parameters_to_python(py, parameters)?)?;
        }
        ControlMessage::Publish(Publish {
            request_id,
            track_namespace,
            track_name,
            track_alias,
            parameters,
            track_properties,
        }) => {
            dict.set_item("request_id", *request_id)?;
            dict.set_item(
                "track_namespace",
                track_namespace_to_python(py, track_namespace)?,
            )?;
            dict.set_item("track_name", PyBytes::new(py, track_name))?;
            dict.set_item("track_alias", *track_alias)?;
            dict.set_item("parameters", message_parameters_to_python(py, parameters)?)?;
            dict.set_item(
                "track_properties",
                track_properties_to_python(py, track_properties)?,
            )?;
        }
        ControlMessage::PublishDone(PublishDone {
            status_code,
            stream_count,
            reason,
        }) => {
            dict.set_item("status_code", *status_code)?;
            dict.set_item("stream_count", *stream_count)?;
            dict.set_item("reason", reason.as_str())?;
        }
        ControlMessage::PublishStateNotify(PublishStateNotify { parameters }) => {
            dict.set_item("parameters", message_parameters_to_python(py, parameters)?)?;
        }
        ControlMessage::Fetch(fetch) => {
            dict.set_item("request_id", fetch.request_id)?;
            dict.set_item(
                "track_namespace",
                track_namespace_to_python(py, &fetch.track_namespace)?,
            )?;
            dict.set_item("track_name", PyBytes::new(py, &fetch.track_name))?;
            dict.set_item(
                "parameters",
                message_parameters_to_python(py, &fetch.parameters)?,
            )?;
        }
        ControlMessage::FetchOk(FetchOk {
            end_of_track,
            end_location,
            parameters,
            track_properties,
        }) => {
            dict.set_item("end_of_track", *end_of_track != 0)?;
            dict.set_item(
                "end_location",
                (end_location.group_id, end_location.object_id),
            )?;
            dict.set_item("parameters", message_parameters_to_python(py, parameters)?)?;
            dict.set_item(
                "track_properties",
                track_properties_to_python(py, track_properties)?,
            )?;
        }
        ControlMessage::TrackStatus(TrackStatus {
            request_id,
            track_namespace,
            track_name,
            parameters,
        }) => {
            dict.set_item("request_id", *request_id)?;
            dict.set_item(
                "track_namespace",
                track_namespace_to_python(py, track_namespace)?,
            )?;
            dict.set_item("track_name", PyBytes::new(py, track_name))?;
            dict.set_item("parameters", message_parameters_to_python(py, parameters)?)?;
        }
    }
    Ok(dict.unbind())
}

/// sans I/O セッション状態機械が返すイベント。
///
/// 種別ごとに意味を持つ属性だけが入る。どの属性が有効かは `kind` で決まる。
#[pyclass(name = "Event", frozen)]
pub(crate) struct CoreEvent {
    kind: &'static str,
    data: Option<Vec<u8>>,
    code: Option<u64>,
    reason: Option<String>,
    /// 対象 request の Request ID (request 系イベントのみ)。
    request_id: Option<u64>,
    /// 対象データストリームの ID (data stream 系イベントのみ)。
    stream_id: Option<u64>,
    /// RESET_STREAM の reliable size (RESET_STREAM_AT の場合のみ)。
    reliable_size: Option<u64>,
    /// メッセージを送信した後にストリームを FIN するか。
    fin: Option<bool>,
    /// メッセージ本体 (送信系は送信内容、受信系は受信内容)。
    message: Option<Py<PyDict>>,
    /// メッセージの生バイト列 (Type + Length + Message Body)。
    ///
    /// 受信系イベントではそのまま peer へ中継できる形で保持する。
    message_data: Option<Vec<u8>>,
    /// オブジェクトの受理結果 (object イベントのみ)。
    acceptance: Option<&'static str>,
    /// 受信したオブジェクトの Object ID (object イベントのみ)。
    object_id: Option<u64>,
    /// 受信した data stream の Track Alias (object イベントのみ)。
    track_alias: Option<u64>,
    /// 受信した data stream の Group ID (object イベントのみ)。
    group_id: Option<u64>,
    /// 受信したオブジェクトの Object Status (object イベントのみ)。
    ///
    /// ペイロード長 0 のオブジェクトだけが持ち、非 0 長では `None` になる
    /// (draft-ietf-moq-transport-21 §11.1.2 (Object Status))。
    status: Option<u64>,
    /// 受信したオブジェクトの Properties の生バイト (object イベントのみ)。
    ///
    /// `Properties Length (varint) | Properties データ` の形である。データグラムと
    /// subgroup のどちらでも同じ形であり、アプリは `ObjectProperties.decode` で解釈する
    /// (draft-ietf-moq-transport-21 §16.8 (Properties) Table 14)。
    properties: Option<Vec<u8>>,
    /// 受信したデータストリームの Publisher Priority (object イベントのみ)。
    ///
    /// `None` は DEFAULT_PRIORITY bit が立ち、購読の優先度を継承することを示す
    /// (draft-ietf-moq-transport-21 §11.3.1 (Subgroup Header))。
    publisher_priority: Option<u8>,
    /// 受信したオブジェクトを含む subgroup の Subgroup ID (object イベントのみ)。
    ///
    /// ヘッダが Subgroup ID を最初の Object ID として決めるモードでは `None` になる。
    subgroup_id: Option<u64>,
    /// 受信したメッセージのパラメータ。
    parameters: Option<Py<PyDict>>,
}

impl CoreEvent {
    fn simple(kind: &'static str) -> Self {
        Self {
            kind,
            data: None,
            code: None,
            reason: None,
            request_id: None,
            stream_id: None,
            reliable_size: None,
            fin: None,
            message: None,
            message_data: None,
            acceptance: None,
            object_id: None,
            track_alias: None,
            group_id: None,
            status: None,
            properties: None,
            publisher_priority: None,
            subgroup_id: None,
            parameters: None,
        }
    }

    fn established() -> Self {
        Self::simple("established")
    }

    /// 受信したオブジェクトを表すイベントを作る。
    ///
    /// `request_id` はストリームを所有する subscription / fetch の Request ID である。
    /// どの subscription のオブジェクトかを Python 側が判別するために使う。
    /// 受信したオブジェクトを表すイベントを作る。
    ///
    /// `request_id` はストリームを所有する subscription / fetch の Request ID である。
    /// どの subscription のオブジェクトかを Python 側が判別するために使う。
    ///
    /// `parts` の `properties` はデータグラムと subgroup で同じ形にする。
    /// `publisher_priority` と `subgroup_id` はデータストリームのヘッダが運ぶ値であり、
    /// データグラムでは `None` になる。
    fn object(stream_id: Option<u64>, parts: ObjectEventParts) -> Self {
        Self {
            stream_id,
            object_id: Some(parts.object_id),
            data: Some(parts.payload),
            acceptance: Some(parts.acceptance),
            track_alias: parts.track_alias,
            group_id: parts.group_id,
            status: parts.status,
            properties: parts.properties,
            publisher_priority: parts.publisher_priority,
            subgroup_id: parts.subgroup_id,
            ..Self::simple("object")
        }
    }

    fn send_control(data: Vec<u8>) -> Self {
        Self {
            data: Some(data),
            ..Self::simple("send_control")
        }
    }

    fn close(code: u64, reason: &str) -> Self {
        Self {
            code: Some(code),
            reason: Some(reason.to_string()),
            ..Self::simple("close")
        }
    }

    fn with_message(
        kind: &'static str,
        message: Py<PyDict>,
        message_data: Vec<u8>,
        request_id: Option<u64>,
        parameters: Option<Py<PyDict>>,
    ) -> Self {
        Self {
            request_id,
            message: Some(message),
            message_data: Some(message_data),
            parameters,
            ..Self::simple(kind)
        }
    }

    /// 送信元のストリーム ID を設定する。
    fn on_stream(mut self, stream_id: u64) -> Self {
        self.stream_id = Some(stream_id);
        self
    }
}

#[pymethods]
impl CoreEvent {
    /// イベント種別。
    #[getter]
    fn kind(&self) -> &'static str {
        self.kind
    }

    /// 制御ストリームへ書き込むバイト列 (send_control のみ)。
    #[getter]
    fn data(&self, py: Python<'_>) -> Option<Py<PyBytes>> {
        self.data
            .as_ref()
            .map(|data| PyBytes::new(py, data).unbind())
    }

    /// セッション終了コード、またはストリームのエラーコード。
    #[getter]
    fn code(&self) -> Option<u64> {
        self.code
    }

    /// セッション終了理由 (close のみ)。
    #[getter]
    fn reason(&self) -> Option<&str> {
        self.reason.as_deref()
    }

    /// 対象 request の Request ID。
    #[getter]
    fn request_id(&self) -> Option<u64> {
        self.request_id
    }

    /// 対象データストリームの ID。
    #[getter]
    fn stream_id(&self) -> Option<u64> {
        self.stream_id
    }

    /// オブジェクトの受理結果 (object イベントのみ)。
    ///
    /// `accepted` / `unknown_track_alias` / `discarded` / `filtered_out` のいずれかである。
    #[getter]
    fn acceptance(&self) -> Option<&'static str> {
        self.acceptance
    }

    /// 受信したオブジェクトの Object ID (object イベントのみ)。
    #[getter]
    fn object_id(&self) -> Option<u64> {
        self.object_id
    }

    /// 受信した data stream の Track Alias (object イベントのみ)。
    ///
    /// data stream は Request ID ではなく Track Alias で購読を特定するため、
    /// 購読との対応付けに使う。
    #[getter]
    fn track_alias(&self) -> Option<u64> {
        self.track_alias
    }

    /// 受信した data stream の Group ID (object イベントのみ)。
    #[getter]
    fn group_id(&self) -> Option<u64> {
        self.group_id
    }

    /// 受信したオブジェクトの Object Status (object イベントのみ)。
    ///
    /// ペイロード長 0 のオブジェクトだけが持ち、非 0 長では `None` になる。
    /// (draft-ietf-moq-transport-21 §11.1.2 (Object Status))
    #[getter]
    fn status(&self) -> Option<u64> {
        self.status
    }

    /// 受信したオブジェクトの Properties の生バイト (object イベントのみ)。
    ///
    /// `Properties Length (varint) | Properties データ` の形であり、データグラムと
    /// subgroup のどちらでも同じである。`ObjectProperties.decode` で解釈する。
    #[getter]
    fn properties(&self, py: Python<'_>) -> Option<Py<PyBytes>> {
        self.properties
            .as_ref()
            .map(|value| PyBytes::new(py, value).unbind())
    }

    /// 受信したデータストリームの Publisher Priority (object イベントのみ)。
    ///
    /// `None` は DEFAULT_PRIORITY bit が立ち、購読の優先度を継承することを示す。
    #[getter]
    fn publisher_priority(&self) -> Option<u8> {
        self.publisher_priority
    }

    /// 受信したオブジェクトを含む subgroup の Subgroup ID (object イベントのみ)。
    ///
    /// ヘッダが Subgroup ID を最初の Object ID として決めるモードでは `None` に
    /// なる (draft-ietf-moq-transport-21 §11.3.1 (Subgroup Header))。
    #[getter]
    fn subgroup_id(&self) -> Option<u64> {
        self.subgroup_id
    }

    /// RESET_STREAM の reliable size。
    #[getter]
    fn reliable_size(&self) -> Option<u64> {
        self.reliable_size
    }

    /// メッセージ送信後にストリームを FIN するか。
    #[getter]
    fn fin(&self) -> Option<bool> {
        self.fin
    }

    /// 送信すべきメッセージ本体。
    #[getter]
    fn message(&self, py: Python<'_>) -> Option<Py<PyDict>> {
        self.message.as_ref().map(|message| message.clone_ref(py))
    }

    /// メッセージの生バイト列 (Type + Length + Message Body)。
    ///
    /// 受信系イベントでは、そのまま peer へ中継できる形のバイト列になる。
    #[getter]
    fn message_data(&self, py: Python<'_>) -> Option<Py<PyBytes>> {
        self.message_data
            .as_ref()
            .map(|data| PyBytes::new(py, data).unbind())
    }

    /// 受信したメッセージのパラメータ。
    #[getter]
    fn parameters(&self, py: Python<'_>) -> Option<Py<PyDict>> {
        self.parameters
            .as_ref()
            .map(|parameters| parameters.clone_ref(py))
    }

    fn __repr__(&self) -> String {
        format!("Event(kind={:?})", self.kind)
    }
}

/// request stream をどちら側が開始したか。
///
/// MoQT の応答メッセージはワイヤに Request ID を含まないため、ストリームと
/// Request ID の対応は I/O 層が保持する
/// (draft-ietf-moq-transport-21 §9.4 (REQUEST_ERROR) の Request ID 省略)。
#[derive(Debug)]
enum RequestStreamRole {
    /// 自側が開始した request のストリーム。応答はこの Request ID で処理する。
    Local { request_id: u64 },
    /// peer が開始した request のストリーム。最初のメッセージが運んだ Request ID を保持する。
    Peer { request_id: u64 },
}

/// peer から受信中の data stream のデコーダ。
///
/// stream type は最初の断片で確定するため、デコーダは種別ごとに用意する。
enum DataStreamDecoder {
    Subgroup(SubgroupStreamDecoder),
    Fetch(Box<FetchStreamDecoder>),
    /// padding stream はバイト列を読み捨てるだけである
    /// (draft-ietf-moq-transport-21 §11.5.1 (Padding Streams))。
    Padding,
}

/// 受信したオブジェクトのイベントを組み立てる値。
///
/// データストリームとデータグラムで共通の値をまとめる。
#[derive(Debug, Clone)]
struct ObjectEventParts {
    /// Object ID。
    object_id: u64,
    /// ペイロード。
    payload: Vec<u8>,
    /// 受理結果。
    acceptance: &'static str,
    /// Track Alias。データグラムと subgroup はヘッダが運ぶ。
    track_alias: Option<u64>,
    /// Group ID。
    group_id: Option<u64>,
    /// Object Status。ペイロード長 0 のオブジェクトだけが持つ。
    status: Option<u64>,
    /// Properties の生バイト。
    properties: Option<Vec<u8>>,
    /// Publisher Priority。データストリームだけが持つ。
    publisher_priority: Option<u8>,
    /// Subgroup ID。データストリームだけが持つ。
    subgroup_id: Option<u64>,
}

/// 受信中のデータストリームのヘッダ情報。
///
/// subgroup ヘッダはストリームごとに 1 度だけ届き、以降のオブジェクトがその値を
/// 引き継ぐ。オブジェクトのイベントへ載せるために保持する。
#[derive(Debug, Clone, Copy)]
struct DataHeaderInfo {
    /// Track Alias。
    track_alias: u64,
    /// Group ID。
    group_id: u64,
    /// Publisher Priority。`None` は購読の優先度を継承する。
    publisher_priority: Option<u8>,
    /// Subgroup ID。`None` はヘッダが Subgroup ID を持たない。
    subgroup_id: Option<u64>,
}

/// 1 本の MoQT Transport Session に対応する sans I/O セッション状態機械。
///
/// ストリームの実体には触れない。呼び出し側が peer のストリーム種別を判定して
/// `receive_*` を呼び、戻り値のイベントに従ってバイト列を送る。
/// relay 全体の routing / fan-out / cache / policy は扱わない。
#[pyclass(name = "Session")]
pub(crate) struct CoreSession {
    session: Session,
    /// 制御ストリームの受信バッファ (stream type のデコード用)。
    control_decoder: MessageDecoder,
    peer_control_stream_type_received: bool,
    /// request stream ごとの受信バッファ。
    request_buffers: StreamBuffers,
    /// request stream の役割と、自側が開始した request の Request ID。
    request_streams: HashMap<u64, RequestStreamRole>,
    /// data stream ごとの受信バッファ。
    data_buffers: StreamBuffers,
    /// stream type を通知済みの data stream と、そのデコーダ。
    data_decoders: HashMap<u64, DataStreamDecoder>,
    /// 受信中のデータストリームのヘッダ情報 (subgroup ヘッダで確定する)。
    data_headers: HashMap<u64, DataHeaderInfo>,
    /// ペイロードの到着を待っている subgroup オブジェクト (stream_id 索引)。
    ///
    /// WebTransport の受信 fragment 境界はオブジェクト境界と一致しない。オブジェクトの
    /// ヘッダだけが届いた時点でデコーダはペイロード消費待ちになり、次のオブジェクトを
    /// 読めなくなるため、ヘッダをデコード済みのオブジェクトをここへ保留する。
    pending_subgroup_objects: HashMap<u64, DecodedSubgroupObject>,
    /// ペイロードの到着を待っている fetch オブジェクト (stream_id 索引)。
    pending_fetch_entries: HashMap<u64, DecodedFetchEntry>,
    /// peer が SETUP で宣言した Setup Option。
    ///
    /// 状態機械は peer の Setup Options をそのまま保持しないため、受信時に控える。
    /// SETUP を受信していない場合は `None`。
    peer_setup_options: Option<SetupOptions>,
    /// 状態機械が通知した直近のエラー理由 (診断用)。
    ///
    /// ライブラリがプロトコル違反を検出するとセッションを閉じるイベントを
    /// 発行する。その理由を Python 側から参照できるように保持する。
    last_error: Option<String>,
    /// stream type の varint を消費済みのデータストリーム。
    ///
    /// MoQT の単方向ストリームは先頭に stream type を持つ
    /// (draft-ietf-moq-transport-21 §6.4.1 (Unidirectional Streams))。
    data_stream_types_received: HashSet<u64>,
    /// ヘッダをデコード済みのデータストリーム。
    data_headers_decoded: HashSet<u64>,
    started: bool,
    established: bool,
}

impl CoreSession {
    fn new(
        client: bool,
        implementation: &str,
        setup_options: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Self> {
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

        // アプリが指定した Setup Option を追加する。MOQT_IMPLEMENTATION は
        // `implementation` 引数が担うため、二重に指定させない
        if let Some(setup_options) = setup_options {
            for (key, item) in setup_options.iter() {
                let option_type = key.extract::<u64>()?;
                if option_type == SETUP_OPTION_MOQT_IMPLEMENTATION {
                    return Err(PyValueError::new_err(
                        "MOQT_IMPLEMENTATION is specified by the implementation argument",
                    ));
                }
                options.push(SetupOption {
                    option_type,
                    value: setup_option_value_from_python(option_type, &item)?,
                });
            }
        }

        let session = if client {
            Session::new_client(Transport::WebTransport, options)
        } else {
            Session::new_server(Transport::WebTransport, options)
        }
        .map_err(runtime_error)?;

        Ok(Self {
            session,
            control_decoder: MessageDecoder::new(),
            peer_control_stream_type_received: false,
            request_buffers: StreamBuffers::default(),
            request_streams: HashMap::new(),
            data_buffers: StreamBuffers::default(),
            data_decoders: HashMap::new(),
            data_headers: HashMap::new(),
            pending_subgroup_objects: HashMap::new(),
            pending_fetch_entries: HashMap::new(),
            peer_setup_options: None,
            last_error: None,
            data_stream_types_received: HashSet::new(),
            data_headers_decoded: HashSet::new(),
            started: false,
            established: false,
        })
    }

    /// データストリームの種別を状態機械へ通知し、デコーダを用意する。
    fn ensure_data_decoder(
        &mut self,
        stream_id: u64,
        stream_type: Option<u64>,
    ) -> PyResult<DataStreamType> {
        let Some(stream_type) = stream_type else {
            return Err(PyValueError::new_err(format!(
                "stream type is required for the first fragment of data stream {stream_id}"
            )));
        };
        let stream_type = self
            .session
            .recv_data_stream_type(DataStreamId(stream_id), stream_type)
            .map_err(runtime_error)?;
        // 種別ごとのデコーダを用意する。padding stream は読み捨てるだけである
        let decoder = match stream_type {
            DataStreamType::Subgroup => DataStreamDecoder::Subgroup(SubgroupStreamDecoder::new()),
            DataStreamType::Fetch => DataStreamDecoder::Fetch(Box::new(
                FetchStreamDecoder::new_with_group_order(DEFAULT_PUBLISHER_GROUP_ORDER_ASCENDING)
                    .map_err(runtime_error)?,
            )),
            DataStreamType::Padding => DataStreamDecoder::Padding,
        };
        self.data_decoders.insert(stream_id, decoder);
        self.data_stream_types_received.insert(stream_id);
        Ok(stream_type)
    }

    /// 状態機械が発行したイベントをすべて取り出す。
    fn drain_events(&mut self, py: Python<'_>) -> PyResult<Vec<CoreEvent>> {
        self.drain_events_with_data(py, Vec::new())
    }

    /// 状態機械が発行したイベントを取り出す。
    ///
    /// `data` は直前に受信したメッセージのバイト列であり、受信系イベントにのみ付与する。
    fn drain_events_with_data(
        &mut self,
        py: Python<'_>,
        data: Vec<u8>,
    ) -> PyResult<Vec<CoreEvent>> {
        let mut events = Vec::new();
        let mut data = data;
        while let Some(event) = self.session.poll_event() {
            events.push(self.convert_event(py, event, std::mem::take(&mut data))?);
        }
        Ok(events)
    }

    /// `SessionEvent` を Python 側のイベントへ変換する。
    fn convert_event(
        &mut self,
        py: Python<'_>,
        event: SessionEvent,
        data: Vec<u8>,
    ) -> PyResult<CoreEvent> {
        match event {
            SessionEvent::SendControl(message) => {
                let data = message.encode().map_err(runtime_error)?;
                Ok(CoreEvent::send_control(data))
            }
            SessionEvent::SendRequest {
                request_id,
                message,
            } => Ok(CoreEvent::with_message(
                "send_request",
                message_body_to_python(py, &message)?,
                message.encode().map_err(runtime_error)?,
                Some(request_id),
                None,
            )),
            SessionEvent::SendOnStream {
                request_id,
                message,
                fin,
            } => Ok(CoreEvent {
                fin: Some(fin),
                ..CoreEvent::with_message(
                    "send_on_stream",
                    message_body_to_python(py, &message)?,
                    message.encode().map_err(runtime_error)?,
                    Some(request_id),
                    None,
                )
            }),
            SessionEvent::Established => {
                self.established = true;
                Ok(CoreEvent::established())
            }
            SessionEvent::CloseSession(error) => {
                self.established = false;
                self.last_error = Some(format!("{:#x} {}", error.code, error.reason));
                Ok(CoreEvent::close(error.code, error.reason))
            }
            SessionEvent::RequestOkReceived {
                request_id,
                request_kind,
                parameters,
            } => {
                let body = PyDict::new(py);
                body.set_item("request_kind", request_kind_to_python(request_kind))?;
                Ok(CoreEvent::with_message(
                    "request_ok",
                    body.unbind(),
                    data.clone(),
                    Some(request_id),
                    Some(message_parameters_to_python(py, &parameters)?),
                ))
            }
            SessionEvent::FetchOkReceived {
                request_id,
                end_location,
                end_of_track,
            } => {
                let body = PyDict::new(py);
                body.set_item("end_of_track", end_of_track)?;
                body.set_item(
                    "end_location",
                    (end_location.group_id, end_location.object_id),
                )?;
                Ok(CoreEvent::with_message(
                    "fetch_ok",
                    body.unbind(),
                    data.clone(),
                    Some(request_id),
                    None,
                ))
            }
            SessionEvent::PublishDoneReceived {
                request_id,
                status_code,
                stream_count,
                reason,
            } => {
                let body = PyDict::new(py);
                body.set_item("status_code", status_code)?;
                body.set_item("stream_count", stream_count)?;
                body.set_item("reason", reason.as_str())?;
                Ok(CoreEvent::with_message(
                    "publish_done",
                    body.unbind(),
                    data.clone(),
                    Some(request_id),
                    None,
                ))
            }
            SessionEvent::RequestErrorReceived {
                request_id,
                error_code,
                retry_interval,
                reason,
                redirect,
            } => {
                let body = PyDict::new(py);
                body.set_item("error_code", error_code)?;
                body.set_item("retry_interval", retry_interval)?;
                body.set_item("reason", reason.as_str())?;
                match &redirect {
                    Some(redirect) => {
                        let value = PyDict::new(py);
                        value.set_item("connect_uri", PyBytes::new(py, &redirect.connect_uri))?;
                        value.set_item(
                            "track_namespace",
                            track_namespace_to_python(py, &redirect.track_namespace)?,
                        )?;
                        value.set_item("track_name", PyBytes::new(py, &redirect.track_name))?;
                        body.set_item("redirect", value)?;
                    }
                    None => body.set_item("redirect", py.None())?,
                }
                Ok(CoreEvent::with_message(
                    "request_error",
                    body.unbind(),
                    data.clone(),
                    Some(request_id),
                    None,
                ))
            }
            SessionEvent::RequestUpdateReceived {
                request_id,
                parameters,
            } => Ok(CoreEvent::with_message(
                "request_update",
                PyDict::new(py).unbind(),
                data.clone(),
                Some(request_id),
                Some(message_parameters_to_python(py, &parameters)?),
            )),
            SessionEvent::PublishStateNotifyReceived {
                request_id,
                parameters,
            } => Ok(CoreEvent::with_message(
                "publish_state_notify",
                PyDict::new(py).unbind(),
                data.clone(),
                Some(request_id),
                Some(message_parameters_to_python(py, &parameters)?),
            )),
            SessionEvent::RequestTerminated {
                request_id,
                kind,
                reason,
            } => {
                let body = PyDict::new(py);
                body.set_item("request_kind", request_kind_to_python(kind))?;
                body.set_item("reason", termination_reason_to_python(py, &reason)?)?;
                Ok(CoreEvent::with_message(
                    "request_terminated",
                    body.unbind(),
                    data.clone(),
                    Some(request_id),
                    None,
                ))
            }
            SessionEvent::GoawayReceived {
                new_session_uri,
                timeout,
                on_request_stream,
            } => {
                let body = PyDict::new(py);
                body.set_item("new_session_uri", PyBytes::new(py, &new_session_uri))?;
                body.set_item("timeout", timeout)?;
                Ok(CoreEvent::with_message(
                    "goaway",
                    body.unbind(),
                    data.clone(),
                    on_request_stream,
                    None,
                ))
            }
            SessionEvent::SendPaddingStream { length } => {
                let body = PyDict::new(py);
                body.set_item("length", length)?;
                Ok(CoreEvent::with_message(
                    "send_padding_stream",
                    body.unbind(),
                    data.clone(),
                    None,
                    None,
                ))
            }
            SessionEvent::SendPaddingDatagram { length } => {
                let body = PyDict::new(py);
                body.set_item("length", length)?;
                Ok(CoreEvent::with_message(
                    "send_padding_datagram",
                    body.unbind(),
                    data.clone(),
                    None,
                    None,
                ))
            }
            SessionEvent::ResetDataStream {
                stream_id,
                error_code,
                reliable_size,
            } => Ok(CoreEvent {
                stream_id: Some(stream_id.0),
                code: Some(error_code),
                reliable_size,
                ..CoreEvent::simple("reset_data_stream")
            }),
            SessionEvent::OpenFillFetchStream { request_id } => Ok(CoreEvent::with_message(
                "open_fill_fetch_stream",
                PyDict::new(py).unbind(),
                data.clone(),
                Some(request_id),
                None,
            )),
            SessionEvent::ResetRequestStream {
                request_id,
                error_code,
            } => Ok(CoreEvent {
                code: Some(error_code),
                ..CoreEvent::with_message(
                    "reset_request_stream",
                    PyDict::new(py).unbind(),
                    data.clone(),
                    Some(request_id),
                    None,
                )
            }),
            SessionEvent::StopSendingRequestStream {
                request_id,
                error_code,
            } => Ok(CoreEvent {
                code: Some(error_code),
                ..CoreEvent::with_message(
                    "stop_sending_request_stream",
                    PyDict::new(py).unbind(),
                    data.clone(),
                    Some(request_id),
                    None,
                )
            }),
        }
    }
}

/// 受信したオブジェクト 1 件の情報。
///
/// `(stream_id, object_id, payload_length, 受理結果)` の組である。
type DecodedObjectInfo = (u64, u64, u64, &'static str);

/// `RequestStreamEnd` を組み立てる。
fn request_stream_end(
    reset: bool,
    error_code: Option<u64>,
    reliable_size: Option<u64>,
) -> PyResult<RequestStreamEnd> {
    if !reset {
        return Ok(RequestStreamEnd::Fin);
    }
    let Some(error_code) = error_code else {
        return Err(PyValueError::new_err(
            "error_code is required when reset is true",
        ));
    };
    Ok(RequestStreamEnd::Reset {
        error_code,
        reliable_size,
    })
}

#[pymethods]
impl CoreSession {
    /// client role の MoQT Session を作成する。
    ///
    /// `setup_options` は Setup Option Type をキーにした辞書である。偶数型は `int`、
    /// 奇数型は `bytes`、AUTHORIZATION_TOKEN は Token の辞書またはそのリストを渡す。
    /// MOQT_IMPLEMENTATION は `implementation` 引数が担うため指定できない
    /// (draft-ietf-moq-transport-21 §16.4 (Setup Options))。
    #[staticmethod]
    #[pyo3(signature = (implementation="moqt-py", setup_options=None))]
    fn client(implementation: &str, setup_options: Option<&Bound<'_, PyDict>>) -> PyResult<Self> {
        Self::new(true, implementation, setup_options)
    }

    /// server role の MoQT Session を作成する。
    ///
    /// 引数の意味は `client` と同じである。
    #[staticmethod]
    #[pyo3(signature = (implementation="moqt-py", setup_options=None))]
    fn server(implementation: &str, setup_options: Option<&Bound<'_, PyDict>>) -> PyResult<Self> {
        Self::new(false, implementation, setup_options)
    }

    /// peer が SETUP で宣言した Setup Option を返す。
    ///
    /// キーは Setup Option Type、値は偶数型なら `int`、奇数型なら `bytes` である。
    /// AUTHORIZATION_TOKEN は Token の辞書のリストになる。SETUP を受信していない
    /// 場合は空の辞書を返す。
    /// (draft-ietf-moq-transport-21 §9.1 (SETUP) / §16.4 (Setup Options))
    fn peer_setup_options(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        match &self.peer_setup_options {
            Some(options) => setup_options_to_python(py, options),
            None => Ok(PyDict::new(py).unbind()),
        }
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
        let data = encode_control_stream_setup(&message).map_err(runtime_error)?;
        self.started = true;
        Ok(PyBytes::new(py, &data).unbind())
    }

    /// peer 制御ストリームの断片を投入し、発生したイベントを返す。
    fn receive_control(&mut self, py: Python<'_>, data: &[u8]) -> PyResult<Vec<CoreEvent>> {
        if !self.started {
            return Err(PyRuntimeError::new_err("session has not started"));
        }

        self.control_decoder.push(data);
        if !self.peer_control_stream_type_received {
            let Some(stream_type) = self
                .control_decoder
                .try_decode_varint()
                .map_err(runtime_error)?
            else {
                return Ok(Vec::new());
            };
            self.session
                .recv_control_stream_type(stream_type)
                .map_err(runtime_error)?;
            self.peer_control_stream_type_received = true;
        }

        while let Some(message) = self
            .control_decoder
            .try_decode_message()
            .map_err(runtime_error)?
        {
            // peer の SETUP は状態機械がそのままは保持しないため、Python から
            // 参照できるよう受信時に控える (draft-ietf-moq-transport-21 §9.1 (SETUP))
            if let ControlMessage::Setup(setup) = &message {
                self.peer_setup_options = Some(setup.options.clone());
            }
            if let Err(error) = self.session.recv_control(message) {
                return Err(runtime_error(error));
            }
        }

        self.drain_events(py)
    }

    /// peer 制御ストリームが終端したことを通知する。
    ///
    /// `reset` が真の場合は RESET_STREAM、偽の場合は FIN として扱う。
    #[pyo3(signature = (reset=false, error_code=None))]
    fn receive_control_stream_closed(
        &mut self,
        py: Python<'_>,
        reset: bool,
        error_code: Option<u64>,
    ) -> PyResult<Vec<CoreEvent>> {
        let end = request_stream_end(reset, error_code, None)?;
        self.session
            .recv_control_stream_closed(end)
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// 自側が開始した request stream を登録する。
    ///
    /// MoQT の応答メッセージはワイヤに Request ID を含まないため、Python 側が
    /// `send_request` イベントでストリームを開いた直後にこの対応を登録する。
    fn register_local_request_stream(&mut self, stream_id: u64, request_id: u64) {
        self.request_streams
            .insert(stream_id, RequestStreamRole::Local { request_id });
    }

    /// request stream の断片を投入し、発生したイベントを返す。
    ///
    /// `role` はストリームをどちら側が開始したかを表す。
    ///
    /// - `"local"`: 自側が開始した request への応答である
    /// - `"peer"`: peer が開始した request である
    ///
    /// 応答メッセージはワイヤに Request ID を含まないため、この区別は I/O 層
    /// (Python 側) が保持する。
    #[pyo3(signature = (stream_id, data, role="local"))]
    fn receive_request_stream(
        &mut self,
        py: Python<'_>,
        stream_id: u64,
        data: &[u8],
        role: &str,
    ) -> PyResult<Vec<CoreEvent>> {
        self.request_buffers.push(stream_id, data)?;

        let mut events = Vec::new();
        while let Some((message, consumed)) =
            decode_control_message(self.request_buffers.get(stream_id)).map_err(runtime_error)?
        {
            // 受信したメッセージはそのまま中継できるよう生バイト列も保持する
            let raw = self.request_buffers.get(stream_id)[..consumed].to_vec();
            self.request_buffers.consume(stream_id, consumed);
            match role {
                "peer" => {
                    if self.request_streams.contains_key(&stream_id) {
                        // 2 通目以降は既存 request へのメッセージとして処理する。
                        //
                        // REQUEST_UPDATE は独立した Request ID を消費するため、wire の
                        // Request ID は対象 request のものと一致しない。対象 request は
                        // 「同じ bidi stream 上で送る」ことで識別されるため、ストリームに
                        // 紐付けた Request ID を状態機械へ渡す
                        // (draft-ietf-moq-transport-21 §6.4.2.1 (Request ID) / §9.5 (REQUEST_UPDATE))。
                        let stream_request_id = match self.request_streams.get(&stream_id) {
                            Some(RequestStreamRole::Peer { request_id }) => *request_id,
                            _ => message_request_id(&message).ok_or_else(|| {
                                PyValueError::new_err(format!(
                                    "request stream {stream_id} carries a message without a request id"
                                ))
                            })?,
                        };
                        self.session
                            .recv_stream_message(stream_request_id, message)
                            .map_err(runtime_error)?;
                    } else {
                        // 最初のメッセージは状態機械がイベントを発行しないため、
                        // ここで Python 側へ渡す
                        let kind = message_kind(&message);
                        let body = message_body_to_python(py, &message)?;
                        let request_id = message_request_id(&message);
                        self.session.recv_request(message).map_err(runtime_error)?;
                        // ストリームの終端を状態機械へ通知するときに Request ID が要るため、
                        // 最初のメッセージが運んだ値を保持する
                        let stream_request_id = request_id.ok_or_else(|| {
                            PyValueError::new_err(format!(
                                "request stream {stream_id} carries a message without a request id"
                            ))
                        })?;
                        self.request_streams.insert(
                            stream_id,
                            RequestStreamRole::Peer {
                                request_id: stream_request_id,
                            },
                        );
                        events.push(
                            CoreEvent::with_message(kind, body, raw, request_id, None)
                                .on_stream(stream_id),
                        );
                        events.extend(self.drain_events(py)?);
                        continue;
                    }
                }
                _ => {
                    let request_id = match self.request_streams.get(&stream_id) {
                        Some(RequestStreamRole::Local { request_id }) => *request_id,
                        _ => message_request_id(&message).ok_or_else(|| {
                            PyValueError::new_err(format!(
                                "response on stream {stream_id} has no known request id"
                            ))
                        })?,
                    };
                    if let Err(error) = self.session.recv_stream_message(request_id, message) {
                        return Err(runtime_error(error));
                    }
                }
            }
            events.extend(self.drain_events(py)?);
        }

        Ok(events)
    }

    /// peer の request stream が終端したことを通知する。
    #[pyo3(signature = (stream_id, reset=false, error_code=None, reliable_size=None))]
    fn receive_request_stream_closed(
        &mut self,
        py: Python<'_>,
        stream_id: u64,
        reset: bool,
        error_code: Option<u64>,
        reliable_size: Option<u64>,
    ) -> PyResult<Vec<CoreEvent>> {
        self.request_buffers.remove(stream_id);
        // 状態機械は Request ID で request を識別する。ストリーム ID をそのまま渡すと
        // 未知の Request ID として PROTOCOL_VIOLATION になる
        let Some(role) = self.request_streams.remove(&stream_id) else {
            // 自側が把握していないストリームの終端は状態機械も知らないため通知しない
            return self.drain_events(py);
        };
        let request_id = match role {
            RequestStreamRole::Local { request_id } | RequestStreamRole::Peer { request_id } => {
                request_id
            }
        };

        let end = request_stream_end(reset, error_code, reliable_size)?;
        self.session
            .recv_request_stream_closed(request_id, end)
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// peer の data stream の断片を投入し、発生したイベントを返す。
    ///
    /// 最初の断片に含まれる stream type を状態機械へ通知し、種別に応じたデコーダで
    /// ヘッダとオブジェクトをデコードする。オブジェクトのペイロードはイベントの
    /// `data` にそのまま入る。
    ///
    /// 返り値は `(オブジェクトの受理結果, イベント列)` の組である。受理結果は
    /// デコードしたオブジェクトごとに `(stream_id, object_id, payload_length, 受理結果)`
    /// を並べたリストである。
    #[pyo3(signature = (stream_id, data, stream_type=None))]
    fn receive_data_stream(
        &mut self,
        py: Python<'_>,
        stream_id: u64,
        data: &[u8],
        stream_type: Option<u64>,
    ) -> PyResult<(Vec<DecodedObjectInfo>, Vec<CoreEvent>)> {
        self.data_buffers.push(stream_id, data)?;

        // 最初の断片に含まれる stream type を状態機械へ通知する。
        // stream type の varint は I/O 層が取り除き、種別だけを渡す
        if !self.data_stream_types_received.contains(&stream_id) {
            let stream_type = self.ensure_data_decoder(stream_id, stream_type)?;
            let _ = stream_type;
        }

        let buffered = self.data_buffers.get(stream_id).to_vec();
        self.data_buffers.remove(stream_id);

        let mut objects = Vec::new();
        let mut events = Vec::new();
        let stream = DataStreamId(stream_id);
        match self.data_decoders.get_mut(&stream_id) {
            Some(DataStreamDecoder::Subgroup(decoder)) => {
                decoder.push(&buffered);
                if !self.data_headers_decoded.contains(&stream_id)
                    && let Some(header) = decoder.try_decode_header().map_err(runtime_error)?
                {
                    self.session
                        .recv_subgroup_header(stream, &header)
                        .map_err(runtime_error)?;
                    // Subgroup ID を持たないモードではヘッダからは決まらない。Zero は 0、
                    // FirstObjectId は最初のオブジェクト ID になるため、ここでは `None`
                    // としてアプリへ渡す
                    // (draft-ietf-moq-transport-21 §11.3.1 (Subgroup Header))。
                    let subgroup_id = match header.subgroup_id {
                        SubgroupIdMode::Zero => Some(0),
                        SubgroupIdMode::Explicit(id) => Some(id),
                        SubgroupIdMode::FirstObjectId => None,
                    };
                    self.data_headers.insert(
                        stream_id,
                        DataHeaderInfo {
                            track_alias: header.track_alias,
                            group_id: header.group_id,
                            publisher_priority: header.publisher_priority,
                            subgroup_id,
                        },
                    );
                    self.data_headers_decoded.insert(stream_id);
                }
                // ヘッダが揃うまでオブジェクトはデコードできない
                if !self.data_headers_decoded.contains(&stream_id) {
                    events.extend(self.drain_events(py)?);
                    return Ok((objects, events));
                }
                loop {
                    // ペイロードの到着を待っているオブジェクトを先に処理する。
                    // デコーダがペイロード消費待ちの間は次のオブジェクトを読めない
                    let object = match self.pending_subgroup_objects.remove(&stream_id) {
                        Some(object) => object,
                        None => match decoder.try_decode_object().map_err(runtime_error)? {
                            Some(object) => object,
                            None => break,
                        },
                    };
                    // ペイロードが揃っていない場合は保留して続きの到着を待つ。
                    // payload_length が 0 のオブジェクトはデコーダにペイロードが無い
                    let payload = if object.payload_length == 0 {
                        Vec::new()
                    } else {
                        match decoder.try_read_payload() {
                            Some(payload) => payload,
                            None => {
                                self.pending_subgroup_objects.insert(stream_id, object);
                                break;
                            }
                        }
                    };
                    let acceptance = self
                        .session
                        .recv_subgroup_object(stream, &object)
                        .map_err(runtime_error)?;
                    let acceptance = track_data_acceptance_to_python(acceptance);
                    objects.push((
                        stream_id,
                        object.object_id,
                        object.payload_length,
                        acceptance,
                    ));
                    let header = self.data_headers.get(&stream_id).copied();
                    events.push(CoreEvent::object(
                        Some(stream_id),
                        ObjectEventParts {
                            object_id: object.object_id,
                            payload,
                            acceptance,
                            track_alias: header.map(|info| info.track_alias),
                            group_id: header.map(|info| info.group_id),
                            status: object.status,
                            properties: object.properties_bytes.clone(),
                            publisher_priority: header.and_then(|info| info.publisher_priority),
                            subgroup_id: header.and_then(|info| info.subgroup_id),
                        },
                    ));
                }
            }
            Some(DataStreamDecoder::Fetch(decoder)) => {
                decoder.push(&buffered);
                if !self.data_headers_decoded.contains(&stream_id)
                    && let Some(header) = decoder.try_decode_header().map_err(runtime_error)?
                {
                    self.session
                        .recv_fetch_header(stream, &header)
                        .map_err(runtime_error)?;
                    self.data_headers_decoded.insert(stream_id);
                }
                // ヘッダが揃うまでエントリはデコードできない
                if !self.data_headers_decoded.contains(&stream_id) {
                    events.extend(self.drain_events(py)?);
                    return Ok((objects, events));
                }
                loop {
                    // ペイロードの到着を待っているエントリを先に処理する。
                    // デコーダがペイロード消費待ちの間は次のエントリを読めない
                    let entry = match self.pending_fetch_entries.remove(&stream_id) {
                        Some(entry) => entry,
                        None => match decoder.try_decode_entry().map_err(runtime_error)? {
                            Some(entry) => entry,
                            None => break,
                        },
                    };
                    // ペイロードが揃っていない場合は保留して続きの到着を待つ。
                    // payload_length が 0 のオブジェクトはデコーダにペイロードが無い
                    let payload = match entry {
                        DecodedFetchEntry::Object(object) if object.payload_length > 0 => {
                            match decoder.try_read_payload() {
                                Some(payload) => payload,
                                None => {
                                    self.pending_fetch_entries.insert(stream_id, entry);
                                    break;
                                }
                            }
                        }
                        _ => Vec::new(),
                    };
                    self.session
                        .recv_fetch_entry(stream)
                        .map_err(runtime_error)?;
                    match entry {
                        DecodedFetchEntry::Object(object) => {
                            objects.push((
                                stream_id,
                                object.object_id,
                                object.payload_length,
                                "accepted",
                            ));
                            events.push(CoreEvent {
                                stream_id: Some(stream_id),
                                object_id: Some(object.object_id),
                                group_id: Some(object.group_id),
                                data: Some(payload),
                                acceptance: Some("accepted"),
                                // Object Status は FETCH で運ばれるオブジェクトには無い
                                // (draft-ietf-moq-transport-21 §11.1.2 (Object Status))
                                status: None,
                                // FETCH のオブジェクトは Subgroup ID と Publisher Priority を
                                // エントリ自身が運ぶ
                                publisher_priority: Some(object.publisher_priority),
                                subgroup_id: Some(object.subgroup_id),
                                ..CoreEvent::simple("object")
                            });
                        }
                        // End of Range はオブジェクトを運ばないが、範囲の終端を通知する
                        DecodedFetchEntry::EndOfNonExistentRange {
                            group_id,
                            object_id,
                        } => {
                            events.push(CoreEvent {
                                group_id: Some(group_id),
                                object_id: Some(object_id),
                                ..CoreEvent::simple("end_of_non_existent_range")
                            });
                        }
                        DecodedFetchEntry::EndOfUnknownRange {
                            group_id,
                            object_id,
                        } => {
                            events.push(CoreEvent {
                                group_id: Some(group_id),
                                object_id: Some(object_id),
                                ..CoreEvent::simple("end_of_unknown_range")
                            });
                        }
                        DecodedFetchEntry::EndOfTimedOutRange {
                            group_id,
                            object_id,
                        } => {
                            events.push(CoreEvent {
                                group_id: Some(group_id),
                                object_id: Some(object_id),
                                ..CoreEvent::simple("end_of_timed_out_range")
                            });
                        }
                    }
                }
            }
            // padding stream はバイト列を読み捨てる
            Some(DataStreamDecoder::Padding) | None => {}
        }

        events.extend(self.drain_events(py)?);
        Ok((objects, events))
    }

    /// peer の data stream が終端したことを通知する。
    #[pyo3(signature = (stream_id, reset=false, error_code=None, reliable_size=None))]
    fn receive_data_stream_closed(
        &mut self,
        py: Python<'_>,
        stream_id: u64,
        reset: bool,
        error_code: Option<u64>,
        reliable_size: Option<u64>,
    ) -> PyResult<Vec<CoreEvent>> {
        self.data_buffers.remove(stream_id);
        self.data_decoders.remove(&stream_id);
        self.data_headers.remove(&stream_id);
        self.pending_subgroup_objects.remove(&stream_id);
        self.pending_fetch_entries.remove(&stream_id);
        self.data_stream_types_received.remove(&stream_id);
        self.data_headers_decoded.remove(&stream_id);

        let end = request_stream_end(reset, error_code, reliable_size)?;
        self.session
            .recv_data_stream_closed(DataStreamId(stream_id), end)
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// peer のデータグラムを投入し、発生したイベントを返す。
    ///
    /// オブジェクトを受理した場合は、その内容を `object` イベントとして返す。
    fn receive_datagram(&mut self, py: Python<'_>, data: &[u8]) -> PyResult<Vec<CoreEvent>> {
        let acceptance = self.session.recv_datagram(data).map_err(runtime_error)?;
        let mut events = self.drain_events(py)?;
        if let DatagramAcceptance::Object(acceptance) = acceptance {
            let acceptance = track_data_acceptance_to_python(acceptance);
            if acceptance == "accepted" {
                // 受理したデータグラムを復号し、ペイロードと Properties を取り出す
                let (datagram, consumed) = ObjectDatagram::decode(data).map_err(runtime_error)?;
                let payload = data[consumed..].to_vec();
                events.insert(
                    0,
                    CoreEvent::object(
                        None,
                        ObjectEventParts {
                            object_id: datagram.object_id,
                            payload,
                            acceptance,
                            track_alias: Some(datagram.track_alias),
                            group_id: Some(datagram.group_id),
                            status: datagram.status,
                            properties: datagram.properties_data.clone(),
                            // データグラムは subgroup ヘッダを持たないため、
                            // Publisher Priority と Subgroup ID は入らない
                            publisher_priority: None,
                            subgroup_id: None,
                        },
                    ),
                );
            } else {
                events.insert(0, CoreEvent::simple(acceptance));
            }
        }
        Ok(events)
    }

    /// 状態機械が通知した直近のエラー理由を返す。
    ///
    /// プロトコル違反の切り分けに使う診断用の値である。
    #[getter]
    fn last_error(&self) -> Option<&str> {
        self.last_error.as_deref()
    }

    /// SETUP 交換が完了しているかを返す。
    #[getter]
    fn established(&self) -> bool {
        self.established
    }

    /// 現在のセッション状態を返す。
    fn state(&self) -> &'static str {
        match self.session.state() {
            SessionState::LocalSetupSent => "local_setup_sent",
            SessionState::Established => "established",
            SessionState::Closing => "closing",
            SessionState::Closed => "closed",
        }
    }

    /// 自側の役割を返す。
    fn role(&self) -> &'static str {
        match self.session.role() {
            shiguredo_moqt::session::types::Role::Client => "client",
            shiguredo_moqt::session::types::Role::Server => "server",
        }
    }

    /// request_id に対応する subscription の track alias を返す。
    ///
    /// alias は SUBSCRIBE_OK の受信後に確定する。
    fn subscription_track_alias(&self, request_id: u64) -> Option<u64> {
        self.session
            .subscription(request_id)
            .and_then(|subscription| subscription.track_alias)
    }

    /// 終了済みの subscription を破棄する。
    fn forget_subscription(&mut self, request_id: u64) -> bool {
        self.session.forget_subscription(request_id).is_some()
    }

    /// 終了済みの fetch を破棄する。
    fn forget_fetch(&mut self, request_id: u64) -> bool {
        self.session.forget_fetch(request_id).is_some()
    }

    /// subscription が破棄可能かを返す。
    fn subscription_cleanup_ready(&self, request_id: u64) -> Option<bool> {
        self.session.subscription_cleanup_ready(request_id)
    }

    /// fetch が破棄可能かを返す。
    fn fetch_cleanup_ready(&self, request_id: u64) -> Option<bool> {
        self.session.fetch_cleanup_ready(request_id)
    }

    /// 次の request 用 Request ID を予約する。
    fn next_local_request_id(&mut self) -> PyResult<u64> {
        self.session.next_local_request_id().map_err(runtime_error)
    }

    /// セッションを閉じる。
    ///
    /// 理由はライブラリが 'static な文字列しか受け取らないため、既知の理由だけを
    /// そのまま渡し、それ以外は internal error として扱う。
    #[pyo3(signature = (code, reason="internal error"))]
    fn close(&mut self, py: Python<'_>, code: u64, reason: &str) -> PyResult<Vec<CoreEvent>> {
        let reason: &'static str = match reason {
            "no error" => "no error",
            "unauthorized" => "unauthorized",
            "protocol violation" => "protocol violation",
            "going away" => "going away",
            "internal error" => "internal error",
            _ => "internal error",
        };
        self.session.close(code, reason);
        self.drain_events(py)
    }

    /// 時間を進めてタイムアウトを判定する。
    fn tick(&mut self, py: Python<'_>, now_ms: u64) -> PyResult<Vec<CoreEvent>> {
        self.session.tick(now_ms);
        self.drain_events(py)
    }

    /// 制御メッセージのタイムアウト (ms) を設定する。
    fn set_control_message_timeout_ms(&mut self, timeout_ms: Option<u64>) {
        self.session.set_control_message_timeout_ms(timeout_ms);
    }

    /// データストリームのタイムアウト (ms) を設定する。
    fn set_data_stream_timeout_ms(&mut self, timeout_ms: Option<u64>) {
        self.session.set_data_stream_timeout_ms(timeout_ms);
    }

    /// GOAWAY を送信する。
    fn send_goaway(
        &mut self,
        py: Python<'_>,
        new_session_uri: Vec<u8>,
        timeout: u64,
    ) -> PyResult<Vec<CoreEvent>> {
        self.session
            .send_goaway(new_session_uri, timeout)
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// request stream 上に GOAWAY を送信する。
    fn send_goaway_on_request_stream(
        &mut self,
        py: Python<'_>,
        request_id: u64,
        new_session_uri: Vec<u8>,
        timeout: u64,
    ) -> PyResult<Vec<CoreEvent>> {
        self.session
            .send_goaway_on_request_stream(request_id, new_session_uri, timeout)
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// SUBSCRIBE を送信する。
    fn send_subscribe(
        &mut self,
        py: Python<'_>,
        namespace: Vec<Vec<u8>>,
        track_name: Vec<u8>,
        parameters: &Bound<'_, PyAny>,
    ) -> PyResult<Vec<CoreEvent>> {
        let namespace = track_namespace_from_python(namespace)?;
        let parameters = message_parameters_from_python(parameters)?;
        self.session
            .send_subscribe(namespace, track_name, parameters)
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// PUBLISH を送信する。
    fn send_publish(
        &mut self,
        py: Python<'_>,
        namespace: Vec<Vec<u8>>,
        track_name: Vec<u8>,
        track_alias: u64,
        parameters: &Bound<'_, PyAny>,
        track_properties: &Bound<'_, PyAny>,
    ) -> PyResult<Vec<CoreEvent>> {
        let namespace = track_namespace_from_python(namespace)?;
        let parameters = message_parameters_from_python(parameters)?;
        let track_properties = track_properties_from_python(track_properties)?;
        self.session
            .send_publish(
                namespace,
                track_name,
                track_alias,
                parameters,
                track_properties,
            )
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// SUBSCRIBE_OK を送信する。
    fn send_subscribe_ok(
        &mut self,
        py: Python<'_>,
        request_id: u64,
        track_alias: u64,
        parameters: &Bound<'_, PyAny>,
        track_properties: &Bound<'_, PyAny>,
    ) -> PyResult<Vec<CoreEvent>> {
        let parameters = message_parameters_from_python(parameters)?;
        let track_properties = track_properties_from_python(track_properties)?;
        self.session
            .send_subscribe_ok(request_id, track_alias, parameters, track_properties)
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// REQUEST_OK を送信する。
    fn send_request_ok(
        &mut self,
        py: Python<'_>,
        request_id: u64,
        parameters: &Bound<'_, PyAny>,
        track_properties: &Bound<'_, PyAny>,
    ) -> PyResult<Vec<CoreEvent>> {
        let parameters = message_parameters_from_python(parameters)?;
        let track_properties = track_properties_from_python(track_properties)?;
        self.session
            .send_request_ok(request_id, parameters, track_properties)
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// REQUEST_ERROR を送信する。
    ///
    /// `redirect` は `(connect_uri, track_namespace, track_name)` のタプルである。
    #[pyo3(signature = (request_id, error_code, retry_interval, reason, redirect=None))]
    #[allow(clippy::type_complexity)]
    fn send_request_error(
        &mut self,
        py: Python<'_>,
        request_id: u64,
        error_code: u64,
        retry_interval: u64,
        reason: &str,
        redirect: Option<(Vec<u8>, Vec<Vec<u8>>, Vec<u8>)>,
    ) -> PyResult<Vec<CoreEvent>> {
        let reason = reason_from_python(reason)?;
        let redirect = match redirect {
            Some((connect_uri, namespace, track_name)) => Some(Redirect {
                connect_uri,
                track_namespace: track_namespace_from_python(namespace)?,
                track_name,
            }),
            None => None,
        };
        self.session
            .send_request_error(request_id, error_code, retry_interval, reason, redirect)
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// REQUEST_UPDATE を送信する。
    fn send_request_update(
        &mut self,
        py: Python<'_>,
        request_id: u64,
        parameters: &Bound<'_, PyAny>,
    ) -> PyResult<Vec<CoreEvent>> {
        let parameters = message_parameters_from_python(parameters)?;
        self.session
            .send_request_update(request_id, parameters)
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// PUBLISH_STATE_NOTIFY を送信する。
    fn send_publish_state_notify(
        &mut self,
        py: Python<'_>,
        request_id: u64,
        parameters: &Bound<'_, PyAny>,
    ) -> PyResult<Vec<CoreEvent>> {
        let parameters = message_parameters_from_python(parameters)?;
        self.session
            .send_publish_state_notify(request_id, parameters)
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// PUBLISH_DONE を送信する。
    fn send_publish_done(
        &mut self,
        py: Python<'_>,
        request_id: u64,
        status_code: u64,
        stream_count: u64,
        reason: &str,
    ) -> PyResult<Vec<CoreEvent>> {
        let reason = reason_from_python(reason)?;
        self.session
            .send_publish_done(request_id, status_code, stream_count, reason)
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// FETCH を送信する。
    fn send_fetch(
        &mut self,
        py: Python<'_>,
        namespace: Vec<Vec<u8>>,
        track_name: Vec<u8>,
        parameters: &Bound<'_, PyAny>,
    ) -> PyResult<Vec<CoreEvent>> {
        let namespace = track_namespace_from_python(namespace)?;
        let parameters = message_parameters_from_python(parameters)?;
        self.session
            .send_fetch(namespace, track_name, parameters)
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// FETCH_OK を送信する。
    fn send_fetch_ok(
        &mut self,
        py: Python<'_>,
        request_id: u64,
        end_of_track: bool,
        end_location: (u64, u64),
        parameters: &Bound<'_, PyAny>,
        track_properties: &Bound<'_, PyAny>,
    ) -> PyResult<Vec<CoreEvent>> {
        let end_location = Location {
            group_id: end_location.0,
            object_id: end_location.1,
        };
        let parameters = message_parameters_from_python(parameters)?;
        let track_properties = track_properties_from_python(track_properties)?;
        self.session
            .send_fetch_ok(
                request_id,
                u8::from(end_of_track),
                end_location,
                parameters,
                track_properties,
            )
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// TRACK_STATUS を送信する。
    fn send_track_status(
        &mut self,
        py: Python<'_>,
        namespace: Vec<Vec<u8>>,
        track_name: Vec<u8>,
        parameters: &Bound<'_, PyAny>,
    ) -> PyResult<Vec<CoreEvent>> {
        let namespace = track_namespace_from_python(namespace)?;
        let parameters = message_parameters_from_python(parameters)?;
        self.session
            .send_track_status(namespace, track_name, parameters)
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// subscription を終了する (subscriber 側の STOP_SENDING)。
    fn stop_sending(&mut self, py: Python<'_>, request_id: u64) -> PyResult<Vec<CoreEvent>> {
        self.session
            .stop_sending(request_id)
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// FETCH の STOP_SENDING を送信する。
    fn send_fetch_stop_sending(
        &mut self,
        py: Python<'_>,
        request_id: u64,
    ) -> PyResult<Vec<CoreEvent>> {
        self.session
            .send_fetch_stop_sending(request_id)
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// FETCH の STOP_SENDING を受信したことを通知する。
    fn fetch_stop_sending_received(
        &mut self,
        py: Python<'_>,
        request_id: u64,
    ) -> PyResult<Vec<CoreEvent>> {
        self.session
            .fetch_stop_sending_received(request_id)
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// 自側が受け持つ subscription の応答を処理する。
    ///
    /// REQUEST_UPDATE に対して FORWARD などを変更する場合に使う。
    fn send_publish_done_for_subscription(
        &mut self,
        py: Python<'_>,
        request_id: u64,
        status_code: u64,
        reason: &str,
    ) -> PyResult<Vec<CoreEvent>> {
        let stream_count = self
            .session
            .subscription(request_id)
            .map_or(0, |subscription| subscription.stream_counts.published_count);
        let reason = reason_from_python(reason)?;
        self.session
            .send_publish_done(request_id, status_code, stream_count, reason)
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// 送信する subgroup ストリームを登録する。
    ///
    /// 実際のバイト列は Python 側が組み立てるため、ここでは状態機械へ登録だけを行う。
    #[pyo3(signature = (stream_id, request_id, track_alias, group_id, subgroup_id, publisher_priority=None, has_properties=false, end_of_group=false, first_object=false))]
    #[allow(clippy::too_many_arguments)]
    fn send_subgroup_header(
        &mut self,
        py: Python<'_>,
        stream_id: u64,
        request_id: u64,
        track_alias: u64,
        group_id: u64,
        subgroup_id: Option<u64>,
        publisher_priority: Option<u8>,
        has_properties: bool,
        end_of_group: bool,
        first_object: bool,
    ) -> PyResult<Vec<CoreEvent>> {
        let header = SubgroupHeader {
            track_alias,
            group_id,
            subgroup_id: match subgroup_id {
                Some(id) => SubgroupIdMode::Explicit(id),
                None => SubgroupIdMode::Zero,
            },
            publisher_priority,
            has_properties,
            end_of_group,
            first_object,
        };
        self.session
            .send_subgroup_header(DataStreamId(stream_id), request_id, &header)
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// subgroup ストリームへオブジェクトを書き込むことを通知する。
    ///
    /// フィルタで破棄される場合は `False` を返す。その場合 Python 側は
    /// バイト列を送信してはならない。
    #[pyo3(signature = (stream_id, object_id, properties_data=None))]
    fn send_subgroup_object(
        &mut self,
        py: Python<'_>,
        stream_id: u64,
        object_id: u64,
        properties_data: Option<Vec<u8>>,
    ) -> PyResult<(bool, Vec<CoreEvent>)> {
        match self.session.send_subgroup_object(
            DataStreamId(stream_id),
            object_id,
            properties_data.as_deref(),
        ) {
            Ok(()) => Ok((true, self.drain_events(py)?)),
            // ローカルのフィルタで破棄する場合はエラーではなく「送らない」指示である
            Err(shiguredo_moqt::session::types::SendRequestError::LocalFilterMismatch) => {
                Ok((false, Vec::new()))
            }
            Err(error) => Err(runtime_error(error)),
        }
    }

    /// 送信する fetch ストリームを登録する。
    fn send_fetch_header(
        &mut self,
        py: Python<'_>,
        stream_id: u64,
        request_id: u64,
    ) -> PyResult<Vec<CoreEvent>> {
        self.session
            .send_fetch_header(DataStreamId(stream_id), request_id)
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// fetch ストリームへオブジェクトを書き込むことを通知する。
    fn send_fetch_object(&mut self, py: Python<'_>, stream_id: u64) -> PyResult<Vec<CoreEvent>> {
        self.session
            .send_fetch_object(DataStreamId(stream_id))
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// 送信する fill fetch ストリームを登録する。
    fn send_fill_fetch_header(
        &mut self,
        py: Python<'_>,
        stream_id: u64,
        request_id: u64,
    ) -> PyResult<Vec<CoreEvent>> {
        self.session
            .send_fill_fetch_header(DataStreamId(stream_id), request_id)
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// fetch ストリームを終了する。
    fn send_fetch_data_stream_closed(
        &mut self,
        py: Python<'_>,
        stream_id: u64,
    ) -> PyResult<Vec<CoreEvent>> {
        self.session
            .send_fetch_data_stream_closed(DataStreamId(stream_id))
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// 送信済みのデータストリームを終了する。
    #[pyo3(signature = (stream_id, reset=false, error_code=None, reliable_size=None))]
    fn send_data_stream_closed(
        &mut self,
        py: Python<'_>,
        stream_id: u64,
        reset: bool,
        error_code: Option<u64>,
        reliable_size: Option<u64>,
    ) -> PyResult<Vec<CoreEvent>> {
        let end = request_stream_end(reset, error_code, reliable_size)?;
        self.session
            .send_data_stream_closed(DataStreamId(stream_id), end)
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// 送信済みのデータストリームを reset する。
    ///
    /// `reliable_size` を渡すと RESET_STREAM_AT になり、先頭 `reliable_size` バイトは
    /// peer へ確実に届ける (draft-ietf-moq-transport-21 §11.3.2 (Subgroup Object))。
    /// 省略した場合は RESET_STREAM になり、未達のデータは破棄される。
    #[pyo3(signature = (stream_id, error_code, reliable_size=None))]
    fn reset_outgoing_data_stream(
        &mut self,
        py: Python<'_>,
        stream_id: u64,
        error_code: u64,
        reliable_size: Option<u64>,
    ) -> PyResult<Vec<CoreEvent>> {
        match reliable_size {
            Some(reliable_size) => self
                .session
                .reset_outgoing_data_stream_at_with_code(
                    DataStreamId(stream_id),
                    reliable_size,
                    Some(error_code),
                )
                .map_err(runtime_error)?,
            None => self
                .session
                .reset_outgoing_data_stream_with_code(DataStreamId(stream_id), error_code)
                .map_err(runtime_error)?,
        }
        self.drain_events(py)
    }

    /// peer が受信ストリームへ STOP_SENDING を送ったことを通知する。
    fn recv_data_stream_stop_sending(
        &mut self,
        py: Python<'_>,
        stream_id: u64,
    ) -> PyResult<Vec<CoreEvent>> {
        self.session
            .recv_data_stream_stop_sending(DataStreamId(stream_id))
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// 自側が受信ストリームへ STOP_SENDING を送ったことを通知する。
    fn send_data_stream_stop_sending(
        &mut self,
        py: Python<'_>,
        stream_id: u64,
    ) -> PyResult<Vec<CoreEvent>> {
        self.session
            .send_data_stream_stop_sending(DataStreamId(stream_id))
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// オブジェクトの受信途中でストリームが終端したことを通知する。
    fn report_mid_object_fin(
        &mut self,
        py: Python<'_>,
        stream_id: u64,
    ) -> PyResult<Vec<CoreEvent>> {
        self.session
            .report_mid_object_fin(DataStreamId(stream_id))
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// オブジェクトデータグラムを送信することを通知する。
    ///
    /// フィルタで破棄される場合は `False` を返す。その場合 Python 側は
    /// データグラムを送信してはならない。
    #[pyo3(signature = (request_id, group_id, object_id, properties_data=None, status=None))]
    fn send_object_datagram(
        &mut self,
        py: Python<'_>,
        request_id: u64,
        group_id: u64,
        object_id: u64,
        properties_data: Option<Vec<u8>>,
        status: Option<u64>,
    ) -> PyResult<(bool, Vec<CoreEvent>)> {
        match self.session.send_object_datagram(
            request_id,
            group_id,
            object_id,
            properties_data,
            status,
        ) {
            Ok(()) => Ok((true, self.drain_events(py)?)),
            Err(shiguredo_moqt::session::types::SendRequestError::LocalFilterMismatch) => {
                Ok((false, Vec::new()))
            }
            Err(error) => Err(runtime_error(error)),
        }
    }

    /// パディングストリームの送信を要求する。
    fn send_padding_stream(&mut self, py: Python<'_>, length: u64) -> PyResult<Vec<CoreEvent>> {
        self.session
            .send_padding_stream(length)
            .map_err(runtime_error)?;
        self.drain_events(py)
    }

    /// パディングデータグラムの送信を要求する。
    fn send_padding_datagram(&mut self, py: Python<'_>, length: u64) -> PyResult<Vec<CoreEvent>> {
        self.session
            .send_padding_datagram(length)
            .map_err(runtime_error)?;
        self.drain_events(py)
    }
}
