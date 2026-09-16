//! Message Parameters の型付きアクセサ (`moqt.moqt`)。
//!
//! draft-ietf-moq-transport-21 §9.20 (Control Message Parameters) はパラメータ型ごとに
//! 値の形式と意味を定める。このモジュールは、`Event.parameters` / `Message.parameters` が
//! 返す「型番号をキーにしたエンコード済みバイト列の辞書」を型付きで読み書きする口を
//! 公開する。辞書との相互変換も提供する。
//!
//! この仕様は draft 由来であり、将来の改訂で変更される可能性がある。

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};

use shiguredo_moqt::message::common::Location;
use shiguredo_moqt::message_parameter::{
    LocationFilter as MoqtLocationFilter, LocationFilterUpdate as MoqtLocationFilterUpdate,
    MessageParameter, MessageParameterValue, MessageParameters as MoqtMessageParameters,
    PARAM_AUTHORIZATION_TOKEN, PARAM_FILL_PARAMETERS, PARAM_LOCATION_FILTER,
    PARAM_OBJECT_PROPERTY_FILTER, PARAM_OBJECTID_FILTER, PARAM_PRIORITY_FILTER,
    PARAM_SUBGROUP_FILTER, PARAM_TRACK_PROPERTY_FILTER,
};

use crate::core::{
    decode_parameter_entry, decode_varint_prefix, message_parameters_to_python,
    parameter_value_from_python, parameter_value_to_python, track_namespace_to_python,
};
use crate::errors::codec_error;

/// 値が長さ付きバイト列であるパラメータ型かを返す。
///
/// これらの型の値は先頭の vi64 が値本体の長さである
/// (draft-ietf-moq-transport-21 §8.3 (Key-Value-Pair Structure))。
fn is_length_prefixed(param_type: u64) -> bool {
    matches!(
        param_type,
        PARAM_AUTHORIZATION_TOKEN
            | PARAM_LOCATION_FILTER
            | PARAM_SUBGROUP_FILTER
            | PARAM_OBJECTID_FILTER
            | PARAM_PRIORITY_FILTER
            | PARAM_OBJECT_PROPERTY_FILTER
            | PARAM_TRACK_PROPERTY_FILTER
            | PARAM_FILL_PARAMETERS
    )
}

/// 長さ付きバイト列のパラメータの値がエンコード済みであることを検証する。
///
/// フィルタ本体のような長さプレフィックスを持たない生バイト列を渡した場合に、
/// 黙って別の値として解釈しないよう長さを照合する。
fn validate_encoded_value(param_type: u64, value: &[u8]) -> PyResult<()> {
    if !is_length_prefixed(param_type) {
        return Ok(());
    }
    let Some((declared, prefix_len)) = decode_varint_prefix(value).map_err(codec_error)? else {
        return Err(PyValueError::new_err(format!(
            "message parameter {param_type:#x} requires an encoded value with a length prefix, got {} bytes",
            value.len()
        )));
    };
    let remaining = (value.len() - prefix_len) as u64;
    if declared != remaining {
        return Err(PyValueError::new_err(format!(
            "message parameter {param_type:#x} requires an encoded value: the length prefix declares {declared} bytes but {remaining} bytes follow"
        )));
    }
    Ok(())
}

/// パラメータ 1 件を「エンコード済みバイト列の辞書」の値から構築する。
///
/// `bytes` はエンコード済みの値、[`LocationFilter`] は LOCATION_FILTER の型付き表現、
/// それ以外は型ごとの Python 表現として解釈する。
fn parameter_from_dict(param_type: u64, value: &Bound<'_, PyAny>) -> PyResult<MessageParameter> {
    // 型付きのフィルタは長さプレフィックスを持たないため、先に受け付ける
    if param_type == PARAM_LOCATION_FILTER
        && let Ok(filter) = value.cast::<LocationFilter>()
    {
        return Ok(MessageParameter {
            param_type,
            value: MessageParameterValue::LengthPrefixed(filter.borrow().inner.encode_to_bytes()),
        });
    }
    if let Ok(encoded) = value.cast::<PyBytes>() {
        validate_encoded_value(param_type, encoded.as_bytes())?;
        return decode_parameter_entry(param_type, encoded.as_bytes());
    }
    Ok(MessageParameter {
        param_type,
        value: parameter_value_from_python(param_type, value)?,
    })
}

/// Python 側の辞書から `MessageParameters` を構築する。
///
/// キーはパラメータ型、値はパラメータ 1 件分のエンコード済みバイト列である。
/// AUTHORIZATION_TOKEN は複数回出現できるため、値にリストを渡した場合は同じ型を
/// 複数回追加する。
fn message_parameters_from_dict(dict: &Bound<'_, PyDict>) -> PyResult<MoqtMessageParameters> {
    let mut parameters = MoqtMessageParameters::new();
    for (key, item) in dict.iter() {
        let param_type = key.extract::<u64>()?;
        if param_type == PARAM_AUTHORIZATION_TOKEN
            && let Ok(tokens) = item.cast::<PyList>()
        {
            for token in tokens.iter() {
                parameters.push(parameter_from_dict(param_type, &token)?);
            }
            continue;
        }
        parameters.push(parameter_from_dict(param_type, &item)?);
    }
    Ok(parameters)
}

/// LOCATION_FILTER (draft-ietf-moq-transport-21 §9.20.10 (LOCATION FILTER Parameter)) の
/// 型付き表現。
///
/// wire 形式は Length-prefixed な optional vi64 群であり、Length (バイト数) で
/// フィールド数が決まる。フィールド数と意味の対応は次のとおりである。
///
/// - 1 フィールド: StartGroup (Largest Object 相対)
/// - 2 フィールド: StartGroup + StartObject (両方 0 なら Next Object、そうでなければ absolute)
/// - 3 フィールド: absolute Start + EndGroupDelta (End Group の全 Object を含む)
/// - 4 フィールド: absolute Start + EndGroupDelta + EndObject
///
/// `kind` は `relative_group` / `next_object` / `absolute_start` / `absolute_range` /
/// `absolute_range_with_end` のいずれかであり、種別ごとに必要なフィールドが異なる。
/// 過不足のあるフィールドを渡すと `ValueError` になる。
///
/// この仕様は draft 由来であり、将来の改訂で変更される可能性がある。
#[pyclass(name = "LocationFilter", frozen)]
pub(crate) struct LocationFilter {
    inner: MoqtLocationFilter,
}

impl LocationFilter {
    /// `MoqtLocationFilter` を包む。
    fn wrap(inner: MoqtLocationFilter) -> Self {
        Self { inner }
    }
}

/// kind が要求するフィールドを取り出す。
fn require_field(kind: &str, name: &str, value: Option<u64>) -> PyResult<u64> {
    value.ok_or_else(|| {
        PyValueError::new_err(format!("LOCATION_FILTER kind '{kind}' requires {name}"))
    })
}

/// kind が取らないフィールドが指定されていないことを検証する。
fn forbid_fields(kind: &str, fields: &[(&str, Option<u64>)]) -> PyResult<()> {
    for (name, value) in fields {
        if value.is_some() {
            return Err(PyValueError::new_err(format!(
                "LOCATION_FILTER kind '{kind}' does not take {name}"
            )));
        }
    }
    Ok(())
}

/// `Option<u64>` を Python の `None` と同じ表記で書き出す。
fn format_optional(value: Option<u64>) -> String {
    match value {
        Some(value) => value.to_string(),
        None => "None".to_string(),
    }
}

/// Python 側の種別とフィールドから `MoqtLocationFilter` を組み立てる。
fn location_filter_from_parts(
    kind: &str,
    start_group: Option<u64>,
    start_object: Option<u64>,
    end_group_delta: Option<u64>,
    end_object: Option<u64>,
) -> PyResult<MoqtLocationFilter> {
    match kind {
        "relative_group" => {
            let start_group = require_field(kind, "start_group", start_group)?;
            forbid_fields(
                kind,
                &[
                    ("start_object", start_object),
                    ("end_group_delta", end_group_delta),
                    ("end_object", end_object),
                ],
            )?;
            Ok(MoqtLocationFilter::RelativeGroup { start_group })
        }
        "next_object" => {
            forbid_fields(
                kind,
                &[
                    ("start_group", start_group),
                    ("start_object", start_object),
                    ("end_group_delta", end_group_delta),
                    ("end_object", end_object),
                ],
            )?;
            // Next Object は StartGroup / StartObject がともに 0 の 2 フィールドで表す
            Ok(MoqtLocationFilter::NextObject)
        }
        "absolute_start" => {
            let start_group = require_field(kind, "start_group", start_group)?;
            let start_object = require_field(kind, "start_object", start_object)?;
            forbid_fields(
                kind,
                &[
                    ("end_group_delta", end_group_delta),
                    ("end_object", end_object),
                ],
            )?;
            Ok(MoqtLocationFilter::AbsoluteStart {
                start: Location {
                    group_id: start_group,
                    object_id: start_object,
                },
            })
        }
        "absolute_range" => {
            let start_group = require_field(kind, "start_group", start_group)?;
            let start_object = require_field(kind, "start_object", start_object)?;
            let end_group_delta = require_field(kind, "end_group_delta", end_group_delta)?;
            forbid_fields(kind, &[("end_object", end_object)])?;
            Ok(MoqtLocationFilter::AbsoluteRange {
                start: Location {
                    group_id: start_group,
                    object_id: start_object,
                },
                end_group_delta,
            })
        }
        "absolute_range_with_end" => {
            let start_group = require_field(kind, "start_group", start_group)?;
            let start_object = require_field(kind, "start_object", start_object)?;
            let end_group_delta = require_field(kind, "end_group_delta", end_group_delta)?;
            let end_object = require_field(kind, "end_object", end_object)?;
            Ok(MoqtLocationFilter::AbsoluteRangeWithEnd {
                start: Location {
                    group_id: start_group,
                    object_id: start_object,
                },
                end_group_delta,
                end_object,
            })
        }
        other => Err(PyValueError::new_err(format!(
            "unknown LOCATION_FILTER kind '{other}': expected one of relative_group / next_object / absolute_start / absolute_range / absolute_range_with_end"
        ))),
    }
}

#[pymethods]
impl LocationFilter {
    /// LOCATION_FILTER を組み立てる。
    ///
    /// `kind` ごとに必要なフィールドは次のとおりである。
    ///
    /// - `relative_group`: `start_group`
    /// - `next_object`: なし
    /// - `absolute_start`: `start_group` / `start_object`
    /// - `absolute_range`: `start_group` / `start_object` / `end_group_delta`
    /// - `absolute_range_with_end`: `start_group` / `start_object` / `end_group_delta` /
    ///   `end_object`
    #[new]
    #[pyo3(signature = (
        kind,
        start_group = None,
        start_object = None,
        end_group_delta = None,
        end_object = None,
    ))]
    fn new(
        kind: &str,
        start_group: Option<u64>,
        start_object: Option<u64>,
        end_group_delta: Option<u64>,
        end_object: Option<u64>,
    ) -> PyResult<Self> {
        Ok(Self::wrap(location_filter_from_parts(
            kind,
            start_group,
            start_object,
            end_group_delta,
            end_object,
        )?))
    }

    /// wire format のバイト列から LOCATION_FILTER を読み込む。
    ///
    /// フィールド数が 0 または 5 以上の場合と、壊れた vi64 は `ValueError` になる
    /// (draft-ietf-moq-transport-21 §9.20.10 (LOCATION FILTER Parameter))。
    #[staticmethod]
    fn decode(data: &[u8]) -> PyResult<Self> {
        Ok(Self::wrap(
            MoqtLocationFilter::decode(data).map_err(codec_error)?,
        ))
    }

    /// LOCATION_FILTER の値部分をバイト列へ書き出す。
    ///
    /// 書き出したバイト列は `MessageParameters` の辞書の値として渡せる
    /// (パラメータ 1 件分のエンコード済みバイト列)。
    fn encode<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.inner.encode_to_bytes())
    }

    /// フィルタの種別。
    #[getter]
    fn kind(&self) -> &'static str {
        match self.inner {
            MoqtLocationFilter::RelativeGroup { .. } => "relative_group",
            MoqtLocationFilter::NextObject => "next_object",
            MoqtLocationFilter::AbsoluteStart { .. } => "absolute_start",
            MoqtLocationFilter::AbsoluteRange { .. } => "absolute_range",
            MoqtLocationFilter::AbsoluteRangeWithEnd { .. } => "absolute_range_with_end",
        }
    }

    /// Largest Object からの相対オフセット (`relative_group` のみ)。
    #[getter]
    fn start_group(&self) -> Option<u64> {
        match self.inner {
            MoqtLocationFilter::RelativeGroup { start_group } => Some(start_group),
            MoqtLocationFilter::AbsoluteStart { start }
            | MoqtLocationFilter::AbsoluteRange { start, .. }
            | MoqtLocationFilter::AbsoluteRangeWithEnd { start, .. } => Some(start.group_id),
            MoqtLocationFilter::NextObject => None,
        }
    }

    /// 開始 Location の Object ID (`absolute_*` のみ)。
    #[getter]
    fn start_object(&self) -> Option<u64> {
        match self.inner {
            MoqtLocationFilter::AbsoluteStart { start }
            | MoqtLocationFilter::AbsoluteRange { start, .. }
            | MoqtLocationFilter::AbsoluteRangeWithEnd { start, .. } => Some(start.object_id),
            MoqtLocationFilter::RelativeGroup { .. } | MoqtLocationFilter::NextObject => None,
        }
    }

    /// 開始 Group からの End Group の差分 (`absolute_range*` のみ)。
    #[getter]
    fn end_group_delta(&self) -> Option<u64> {
        match self.inner {
            MoqtLocationFilter::AbsoluteRange {
                end_group_delta, ..
            }
            | MoqtLocationFilter::AbsoluteRangeWithEnd {
                end_group_delta, ..
            } => Some(end_group_delta),
            _ => None,
        }
    }

    /// 終端 Group 内の終端 Object ID (`absolute_range_with_end` のみ)。
    #[getter]
    fn end_object(&self) -> Option<u64> {
        match self.inner {
            MoqtLocationFilter::AbsoluteRangeWithEnd { end_object, .. } => Some(end_object),
            _ => None,
        }
    }

    fn __repr__(&self) -> String {
        format!(
            "LocationFilter(kind={}, start_group={}, start_object={}, end_group_delta={}, end_object={})",
            self.kind(),
            format_optional(self.start_group()),
            format_optional(self.start_object()),
            format_optional(self.end_group_delta()),
            format_optional(self.end_object())
        )
    }

    fn __eq__(&self, other: &Bound<'_, PyAny>) -> bool {
        match other.cast::<LocationFilter>() {
            Ok(other) => self.inner == other.borrow().inner,
            Err(_) => false,
        }
    }
}

/// LOCATION_FILTER の更新指示 (draft-ietf-moq-transport-21 §3.3.1 (Location Filters))。
///
/// REQUEST_UPDATE では Length 0 がフィルタの削除を表す。パラメータの省略 (値の変更なし)
/// と区別するために 3 状態で返す。`kind` は `unchanged` / `removed` / `set` のいずれかで
/// あり、`set` のときだけ `filter` が入る。
///
/// この仕様は draft 由来であり、将来の改訂で変更される可能性がある。
#[pyclass(name = "LocationFilterUpdate", frozen, get_all)]
pub(crate) struct LocationFilterUpdate {
    /// `unchanged` / `removed` / `set` のいずれか。
    kind: String,
    /// `kind` が `set` のときの新しいフィルタ。
    filter: Option<Py<LocationFilter>>,
}

#[pymethods]
impl LocationFilterUpdate {
    fn __repr__(&self) -> String {
        format!("LocationFilterUpdate(kind={})", self.kind)
    }
}

/// Message Parameters (draft-ietf-moq-transport-21 §9.20 (Control Message Parameters))。
///
/// `Event.parameters` / `Message.parameters` が返す「型番号をキーにしたエンコード済み
/// バイト列の辞書」と同じ内容を、draft が定める値の型と意味で読み書きする。
/// 辞書からは [`MessageParameters::new`] で構築でき、[`MessageParameters::to_dict`] で
/// 辞書へ戻せる。
///
/// この仕様は draft 由来であり、将来の改訂で変更される可能性がある。
#[pyclass(name = "MessageParameters")]
pub(crate) struct MessageParameters {
    inner: MoqtMessageParameters,
}

#[pymethods]
impl MessageParameters {
    /// パラメータの集合を作成する。
    ///
    /// `parameters` は `Event.parameters` / `Message.parameters` が返す辞書と同じ形式で
    /// ある。キーはパラメータ型、値はパラメータ 1 件分のエンコード済みバイト列であり、
    /// `AUTHORIZATION_TOKEN` は複数回出現できるためバイト列のリストで指定する。
    /// 省略した場合は空の集合になる。
    ///
    /// `bytes` 以外の値は型ごとの Python 表現としても受け取る。`uint8` と `vi64` の
    /// パラメータは `int`、`LARGEST_OBJECT` は `(group_id, object_id)`、
    /// `TRACK_NAMESPACE_PREFIX` は `bytes` のリスト、`FILL_PARAMETERS` は入れ子の辞書、
    /// `AUTHORIZATION_TOKEN` は `{"kind": ...}` の辞書または
    /// `(token_type, token_value)` のタプルである。`LOCATION_FILTER`
    /// には [`LocationFilter`] も渡せる。
    ///
    /// `AUTHORIZATION_TOKEN` の辞書は `kind` で `delete` / `register` / `use_alias` /
    /// `use_value` を選び、キーは種別ごとに異なる。`delete` と `use_alias` は `alias`、
    /// `register` は `alias` / `token_type` / `token_value`、`use_value` は
    /// `token_type` / `token_value` を取る
    /// (draft-ietf-moq-transport-21 §8.9 (Authorization Token Compression))。
    ///
    /// 長さ付きバイト列のパラメータは、長さプレフィックスを含むエンコード済みの値を
    /// 要求する。長さが合わない値と解釈できない値は `ValueError` になる。
    /// この節番号・規則は draft 由来であり将来の改訂で変更されうる。
    #[new]
    #[pyo3(signature = (parameters = None))]
    fn new(parameters: Option<&Bound<'_, PyDict>>) -> PyResult<Self> {
        let inner = match parameters {
            Some(parameters) => message_parameters_from_dict(parameters)?,
            None => MoqtMessageParameters::new(),
        };
        Ok(Self { inner })
    }

    /// 同じ内容を「型番号をキーにしたエンコード済みバイト列の辞書」として返す。
    ///
    /// `Event.parameters` / `Message.parameters` と同じ形式であり、そのまま
    /// [`MessageParameters::new`] へ渡して往復させられる。
    fn to_dict(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        message_parameters_to_python(py, &self.inner)
    }

    /// LARGEST_OBJECT (type 0x09) の値を `(group_id, object_id)` として返す
    /// (draft-ietf-moq-transport-21 §9.20.9 (LARGEST_OBJECT Parameter))。
    #[getter]
    fn largest_object(&self) -> Option<(u64, u64)> {
        self.inner.largest_object()
    }

    /// LARGEST_OBJECT (type 0x09) を設定する。
    ///
    /// 既に値がある場合は置き換える。
    fn set_largest_object(&mut self, group_id: u64, object_id: u64) {
        self.inner.set_largest_object(group_id, object_id);
    }

    /// FORWARD (type 0x10) の値を返す
    /// (draft-ietf-moq-transport-21 §9.20.19 (FORWARD Parameter))。
    ///
    /// 0 は転送しない、1 は転送するである。
    #[getter]
    fn forward(&self) -> Option<u8> {
        self.inner.forward()
    }

    /// EXPIRES (type 0x08) の値を返す
    /// (draft-ietf-moq-transport-21 §9.20.17 (EXPIRES Parameter))。
    ///
    /// 値が 0 の場合と、パラメータが無い場合はどちらも `None` になる。0 を指定された
    /// ことを区別するには [`MessageParameters::has_expires`] を使う。
    #[getter]
    fn expires(&self) -> Option<u64> {
        self.inner.expires()
    }

    /// EXPIRES (type 0x08) が存在するかを返す。
    ///
    /// EXPIRES=0 もパラメータとしては存在するため `True` になる。
    #[getter]
    fn has_expires(&self) -> bool {
        self.inner.has_expires()
    }

    /// GROUP_ORDER (type 0x22) の値を返す
    /// (draft-ietf-moq-transport-21 §9.20.9 (GROUP ORDER Parameter))。
    #[getter]
    fn group_order(&self) -> Option<u8> {
        self.inner.group_order()
    }

    /// OBJECT_DELIVERY_TIMEOUT (type 0x02) の値をミリ秒で返す
    /// (draft-ietf-moq-transport-21 §9.20.2 (OBJECT_DELIVERY_TIMEOUT Parameter))。
    #[getter]
    fn object_delivery_timeout(&self) -> Option<u64> {
        self.inner.object_delivery_timeout()
    }

    /// SUBGROUP_DELIVERY_TIMEOUT (type 0x06) の値をミリ秒で返す
    /// (draft-ietf-moq-transport-21 §9.20.4 (SUBGROUP_DELIVERY_TIMEOUT Parameter))。
    #[getter]
    fn subgroup_delivery_timeout(&self) -> Option<u64> {
        self.inner.subgroup_delivery_timeout()
    }

    /// FILL_TIMEOUT (type 0x0A) の値をミリ秒で返す
    /// (draft-ietf-moq-transport-21 §9.20.6 (FILL TIMEOUT Parameter))。
    #[getter]
    fn fill_timeout(&self) -> Option<u64> {
        self.inner.fill_timeout()
    }

    /// SUBSCRIBER_PRIORITY (type 0x20) の値を返す
    /// (draft-ietf-moq-transport-21 §9.20.6 (SUBSCRIBER_PRIORITY Parameter))。
    #[getter]
    fn subscriber_priority(&self) -> Option<u8> {
        self.inner.subscriber_priority()
    }

    /// INCLUDE_PROPERTIES (type 0x35) の値を返す
    /// (draft-ietf-moq-transport-21 §9.20.22 (INCLUDE_PROPERTIES Parameter))。
    ///
    /// 0 は Properties を送らない、1 は送るである。パラメータが無い場合の既定は 1 で
    /// あるため、判定する側が既定を補う。
    #[getter]
    fn include_properties(&self) -> Option<u8> {
        self.inner.include_properties()
    }

    /// NEW_GROUP_REQUEST (type 0x32) の値を返す
    /// (draft-ietf-moq-transport-21 §9.20.20 (NEW_GROUP_REQUEST Parameter))。
    #[getter]
    fn new_group_request(&self) -> Option<u64> {
        self.inner.new_group_request()
    }

    /// LOCATION_FILTER (type 0x21) のフィルタ本体をバイト列として返す。
    ///
    /// 長さプレフィックスを含まないため、[`LocationFilter::decode`] へそのまま渡せる。
    /// 解釈した値が必要な場合は [`MessageParameters::location_filter_typed`] を使う。
    #[getter]
    fn location_filter(&self) -> Option<Vec<u8>> {
        self.inner.location_filter().map(<[u8]>::to_vec)
    }

    /// LOCATION_FILTER (type 0x21) を [`LocationFilter`] として返す。
    ///
    /// パラメータが無い場合と Length 0 (no filter) の場合は `None` になる。
    /// REQUEST_UPDATE での削除指示と省略を区別する場合は
    /// [`MessageParameters::location_filter_update`] を使う。
    #[getter]
    fn location_filter_typed(&self) -> PyResult<Option<LocationFilter>> {
        match self.inner.location_filter_update().map_err(codec_error)? {
            MoqtLocationFilterUpdate::Set(filter) => Ok(Some(LocationFilter::wrap(filter))),
            MoqtLocationFilterUpdate::Unchanged | MoqtLocationFilterUpdate::Removed => Ok(None),
        }
    }

    /// LOCATION_FILTER (type 0x21) の更新指示を返す
    /// (draft-ietf-moq-transport-21 §3.3.1 (Location Filters))。
    ///
    /// `kind` が `unchanged` なら省略、`removed` なら Length 0 による削除、
    /// `set` なら `filter` への置き換えである。
    #[getter]
    fn location_filter_update(&self, py: Python<'_>) -> PyResult<LocationFilterUpdate> {
        let update = self.inner.location_filter_update().map_err(codec_error)?;
        Ok(match update {
            MoqtLocationFilterUpdate::Unchanged => LocationFilterUpdate {
                kind: "unchanged".to_string(),
                filter: None,
            },
            MoqtLocationFilterUpdate::Removed => LocationFilterUpdate {
                kind: "removed".to_string(),
                filter: None,
            },
            MoqtLocationFilterUpdate::Set(filter) => LocationFilterUpdate {
                kind: "set".to_string(),
                filter: Some(Py::new(py, LocationFilter::wrap(filter))?),
            },
        })
    }

    /// FILL_PARAMETERS (type 0x23) の内側のパラメータ群を返す
    /// (draft-ietf-moq-transport-21 §9.20.16 (FILL PARAMETERS Parameter))。
    ///
    /// 内側は外側とは別のパラメータスコープであり、パラメータが無い場合は `None` になる。
    #[getter]
    fn fill_parameters(&self) -> Option<MessageParameters> {
        self.inner.fill_parameters().map(|inner| MessageParameters {
            inner: inner.clone(),
        })
    }

    /// AUTHORIZATION_TOKEN (type 0x03) の値を出現順に返す
    /// (draft-ietf-moq-transport-21 §8.9 (Authorization Token Compression))。
    ///
    /// 各要素は `decode_parameter` が返すものと同じ「`kind` で種別を表す辞書」であり、
    /// 4 種すべてで alias / token_type / token_value が復元される。AUTHORIZATION_TOKEN は
    /// 同一メッセージ内で複数回出現できる。
    fn authorization_tokens(&self, py: Python<'_>) -> PyResult<Vec<Py<PyAny>>> {
        let mut tokens = Vec::new();
        for token in self.inner.authorization_tokens() {
            let value = MessageParameterValue::AuthorizationToken(token.clone());
            tokens.push(parameter_value_to_python(py, &value)?);
        }
        Ok(tokens)
    }

    /// TRACK_NAMESPACE_PREFIX (type 0x34) の値を namespace のフィールド列として返す
    /// (draft-ietf-moq-transport-21 §9.20.21 (TRACK_NAMESPACE_PREFIX Parameter))。
    #[getter]
    fn track_namespace_prefix(&self, py: Python<'_>) -> PyResult<Option<Py<PyList>>> {
        match self.inner.track_namespace_prefix() {
            Some(namespace) => Ok(Some(track_namespace_to_python(py, namespace)?)),
            None => Ok(None),
        }
    }

    /// 指定した Range Filter 型 (0x25-0x29) の全インスタンスのフィルタ本体を出現順に返す
    /// (draft-ietf-moq-transport-21 §3.3.2 (Range Filters))。
    ///
    /// Range Filter は同一 Parameter Type が同一メッセージ内で複数回出現できるため、
    /// 単一の値ではなく列として返す。長さプレフィックスは含まない。Range Filter 型以外を
    /// 渡した場合は空のリストになる。
    fn range_filters<'py>(&self, py: Python<'py>, param_type: u64) -> Vec<Bound<'py, PyBytes>> {
        self.inner
            .range_filters(param_type)
            .into_iter()
            .map(|bytes| PyBytes::new(py, bytes))
            .collect()
    }

    /// Range Filter を 1 つ以上持つかを返す
    /// (draft-ietf-moq-transport-21 §3.3.2 (Range Filters))。
    #[getter]
    fn has_range_filters(&self) -> bool {
        self.inner.has_range_filters()
    }

    /// Range Filter の個数を返す
    /// (draft-ietf-moq-transport-21 §3.3.2 (Range Filters))。
    #[getter]
    fn range_filter_count(&self) -> u64 {
        self.inner.count_range_filters()
    }

    fn __len__(&self) -> usize {
        self.inner.len()
    }

    fn __repr__(&self) -> String {
        format!("MessageParameters(len={})", self.inner.len())
    }

    fn __eq__(&self, other: &Bound<'_, PyAny>) -> bool {
        match other.cast::<MessageParameters>() {
            Ok(other) => self.inner == other.borrow().inner,
            Err(_) => false,
        }
    }
}
