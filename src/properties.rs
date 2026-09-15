//! MOQT の Object Properties と Track Properties の codec (`moqt.moqt`)。
//!
//! draft-ietf-moq-transport-21 §16.8 (Properties) Table 14 の Key-Value-Pair を
//! Python から encode / decode する。LOC (`moqt.loc`) と同じワイヤ形式であり、
//! 偶数型は varint、奇数型は長さ付きバイト列である
//! (draft-ietf-moq-transport-21 §8.3 (Key-Value-Pair Structure))。
//!
//! Object Properties は `Event.properties` が返す生バイトを解釈するために、
//! Track Properties は SUBSCRIBE_OK / FETCH_OK / PUBLISH が運ぶ値を組み立てるために使う。
//! この仕様は draft 由来であり、将来の改訂で変更される可能性がある。

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};

use shiguredo_moqt::object_properties::{
    ObjectProperties as MoqtObjectProperties, ObjectProperty, ObjectPropertyValue,
    PROP_PRIOR_GROUP_ID_GAP, PROP_PRIOR_OBJECT_ID_GAP,
};
use shiguredo_moqt::track_properties::{
    MANDATORY_TRACK_PROPERTY_MAX, MANDATORY_TRACK_PROPERTY_MIN, PROP_DEFAULT_PUBLISHER_GROUP_ORDER,
    PROP_DEFAULT_PUBLISHER_PRIORITY, PROP_DYNAMIC_GROUPS, PROP_IMMUTABLE_PROPERTIES,
    PROP_MAX_CACHE_DURATION, PROP_OBJECT_DELIVERY_TIMEOUT, PROP_SUBGROUP_DELIVERY_TIMEOUT,
    TrackProperties as MoqtTrackProperties, TrackProperty, TrackPropertyValue,
};

use crate::errors::codec_error;

/// 生バイトから Key-Value-Pair の値を取り出す。
///
/// 偶数型は varint、奇数型は長さ付きバイト列である。`int` と `bytes` のどちらを
/// 渡されたかで表現が決まる。
fn property_value(prop_type: u64, value: &Bound<'_, PyAny>) -> PyResult<ObjectPropertyValue> {
    if let Ok(bytes) = value.cast::<PyBytes>() {
        return Ok(ObjectPropertyValue::Bytes(bytes.as_bytes().to_vec()));
    }
    if let Ok(number) = value.extract::<u64>() {
        return Ok(ObjectPropertyValue::VarInt(number));
    }
    Err(PyValueError::new_err(format!(
        "property {prop_type:#x} requires an int or bytes value, got {}",
        value.get_type().name()?
    )))
}

/// MOQT の Object Properties。
///
/// ワイヤフォーマットは `Properties Length (vi64) | Key-Value-Pairs...` である。
/// encode は prop_type の昇順にソートし、delta encoding で型番号を圧縮する。
/// (draft-ietf-moq-transport-21 §16.8 (Properties) Table 14)
#[pyclass(name = "ObjectProperties")]
pub(crate) struct ObjectProperties {
    inner: MoqtObjectProperties,
}

#[pymethods]
impl ObjectProperties {
    /// 空のプロパティ集合を作成する。
    #[new]
    fn new() -> Self {
        Self {
            inner: MoqtObjectProperties::new(),
        }
    }

    /// プロパティを 1 件追加する。
    ///
    /// 偶数型は varint として `int` を、奇数型は長さ付きバイト列として `bytes` を渡す。
    /// この時点では型番号と値の型の対応を検査しない。対応が取れていないプロパティは
    /// `encode()` が `ValueError` で拒否する。
    fn add(&mut self, prop_type: u64, value: &Bound<'_, PyAny>) -> PyResult<()> {
        self.inner.push(ObjectProperty {
            prop_type,
            value: property_value(prop_type, value)?,
        });
        Ok(())
    }

    /// プロパティブロック全体をエンコードする。
    ///
    /// 空の集合は Properties Length = 0 の 1 バイトになる。
    fn encode<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        let mut buf = Vec::new();
        self.inner.encode(&mut buf).map_err(codec_error)?;
        Ok(PyBytes::new(py, &buf))
    }

    /// バッファ先頭からプロパティブロックをデコードし `(プロパティ, 消費バイト数)` を返す。
    ///
    /// ブロックの後ろに続くバイト列は消費しない。入れ子の IMMUTABLE_PROPERTIES など
    /// draft の MUST に違反する入力は `ValueError` になる。
    #[staticmethod]
    fn decode(data: &[u8]) -> PyResult<(Self, usize)> {
        let (inner, consumed) = MoqtObjectProperties::decode(data).map_err(codec_error)?;
        Ok((Self { inner }, consumed))
    }
    /// プロパティを `{prop_type: 値}` の辞書へ変換する。
    ///
    /// 未知の型番号も含めてすべて返す。
    fn to_dict(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let dict = PyDict::new(py);
        for property in self.inner.iter() {
            match &property.value {
                ObjectPropertyValue::VarInt(value) => {
                    dict.set_item(property.prop_type, *value)?;
                }
                ObjectPropertyValue::Bytes(value) => {
                    dict.set_item(property.prop_type, PyBytes::new(py, value))?;
                }
            }
        }
        Ok(dict.unbind())
    }

    /// PRIOR_GROUP_ID_GAP (0x3C): 直前の存在しない Group の個数。
    ///
    /// (draft-ietf-moq-transport-21 §10.8 (Prior Group ID Gap))
    #[getter]
    fn prior_group_id_gap(&self) -> Option<u64> {
        self.find_varint(PROP_PRIOR_GROUP_ID_GAP)
    }

    /// PRIOR_OBJECT_ID_GAP (0x3E): 直前の存在しない Object の個数。
    ///
    /// (draft-ietf-moq-transport-21 §10.9 (Prior Object ID Gap))
    #[getter]
    fn prior_object_id_gap(&self) -> Option<u64> {
        self.find_varint(PROP_PRIOR_OBJECT_ID_GAP)
    }

    /// OBJECT_DELIVERY_TIMEOUT (0x02): Object の配送期限 (ms)。
    ///
    /// (draft-ietf-moq-transport-21 §10.2 (OBJECT_DELIVERY_TIMEOUT))
    #[getter]
    fn object_delivery_timeout(&self) -> Option<u64> {
        self.find_varint(PROP_OBJECT_DELIVERY_TIMEOUT)
    }

    /// SUBGROUP_DELIVERY_TIMEOUT (0x06): Subgroup の配送期限 (ms)。
    ///
    /// (draft-ietf-moq-transport-21 §10.1 (SUBGROUP_DELIVERY_TIMEOUT))
    #[getter]
    fn subgroup_delivery_timeout(&self) -> Option<u64> {
        self.find_varint(PROP_SUBGROUP_DELIVERY_TIMEOUT)
    }

    /// IMMUTABLE_PROPERTIES (0x0B): 途中で変化しないプロパティの入れ子リスト。
    ///
    /// 内容は解釈せず生バイト列として返す。
    /// (draft-ietf-moq-transport-21 §10.7 (Immutable Properties))
    #[getter]
    fn immutable_properties<'py>(&self, py: Python<'py>) -> Option<Bound<'py, PyBytes>> {
        self.find_bytes(PROP_IMMUTABLE_PROPERTIES)
            .map(|value| PyBytes::new(py, value))
    }

    fn __len__(&self) -> usize {
        self.inner.len()
    }

    /// Properties を含むオブジェクトを送信する引数へそのまま渡せるバイト列を返す。
    ///
    /// `moqt.moq.Publication.send_object` と `send_datagram` の `properties_data` は
    /// この形を受け取る。
    fn __bytes__<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        self.encode(py)
    }

    fn __repr__(&self) -> String {
        format!("ObjectProperties(len={})", self.inner.len())
    }

    fn __eq__(&self, other: &Bound<'_, PyAny>) -> bool {
        match other.cast::<ObjectProperties>() {
            // 追加した順序は wire 上の意味を持たない。encode が型番号の昇順に並べる
            // ため、比較も型番号の昇順に正規化してから行う。
            Ok(other) => {
                sorted_object_properties(&self.inner)
                    == sorted_object_properties(&other.borrow().inner)
            }
            Err(_) => false,
        }
    }
}

impl ObjectProperties {
    /// varint 型のプロパティを引く。
    fn find_varint(&self, prop_type: u64) -> Option<u64> {
        self.inner.iter().find_map(|property| {
            if property.prop_type != prop_type {
                return None;
            }
            match property.value {
                ObjectPropertyValue::VarInt(value) => Some(value),
                ObjectPropertyValue::Bytes(_) => None,
            }
        })
    }

    /// バイト列型のプロパティを引く。
    fn find_bytes(&self, prop_type: u64) -> Option<&[u8]> {
        self.inner.iter().find_map(|property| {
            if property.prop_type != prop_type {
                return None;
            }
            match &property.value {
                ObjectPropertyValue::Bytes(value) => Some(value.as_slice()),
                ObjectPropertyValue::VarInt(_) => None,
            }
        })
    }
}

/// プロパティを型番号の昇順に並べた列を返す。
fn sorted_object_properties(properties: &MoqtObjectProperties) -> Vec<ObjectProperty> {
    let mut sorted: Vec<ObjectProperty> = properties.iter().cloned().collect();
    sorted.sort_by_key(|property| property.prop_type);
    sorted
}

/// MOQT の Track Properties。
///
/// Track 単位で決まるプロパティである。SUBSCRIBE_OK / FETCH_OK / PUBLISH が運ぶ
/// (draft-ietf-moq-transport-21 §16.8 (Properties) Table 14)。
#[pyclass(name = "TrackProperties")]
pub(crate) struct TrackProperties {
    inner: MoqtTrackProperties,
}

#[pymethods]
impl TrackProperties {
    /// 空のプロパティ集合を作成する。
    #[new]
    fn new() -> Self {
        Self {
            inner: MoqtTrackProperties::new(),
        }
    }

    /// プロパティを 1 件追加する。
    ///
    /// 偶数型は varint として `int` を、奇数型は長さ付きバイト列として `bytes` を渡す。
    fn add(&mut self, prop_type: u64, value: &Bound<'_, PyAny>) -> PyResult<()> {
        let value = match property_value(prop_type, value)? {
            ObjectPropertyValue::VarInt(value) => TrackPropertyValue::VarInt(value),
            ObjectPropertyValue::Bytes(value) => TrackPropertyValue::Bytes(value),
        };
        self.inner.push(TrackProperty { prop_type, value });
        Ok(())
    }

    /// プロパティを `{prop_type: 値}` の辞書へ変換する。
    ///
    /// 辞書は Session の `send_*` に渡す `track_properties` 引数と同じ形である。
    /// 未知の型番号も含めてすべて返す。
    fn to_dict(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let dict = PyDict::new(py);
        for property in self.inner.as_slice() {
            match &property.value {
                TrackPropertyValue::VarInt(value) => {
                    dict.set_item(property.prop_type, *value)?;
                }
                TrackPropertyValue::Bytes(value) => {
                    dict.set_item(property.prop_type, PyBytes::new(py, value))?;
                }
            }
        }
        Ok(dict.unbind())
    }

    /// DYNAMIC_GROUPS (0x30): Group が動的に決まるか。
    ///
    /// (draft-ietf-moq-transport-21 §10.6 (Dynamic Groups))
    #[getter]
    fn dynamic_groups(&self) -> Option<u64> {
        self.inner.dynamic_groups()
    }

    /// DEFAULT_PUBLISHER_PRIORITY (0x0E): 既定の Publisher Priority。
    ///
    /// 省略時は `None` になる。draft の既定値 128 は適用しない
    /// (draft-ietf-moq-transport-21 §10.4 (Default Publisher Priority))。
    #[getter]
    fn default_publisher_priority(&self) -> Option<u8> {
        self.inner.default_publisher_priority()
    }

    /// DEFAULT_PUBLISHER_GROUP_ORDER (0x22): 既定の Group Order。
    ///
    /// 省略時は `None` になる。draft の既定値 Ascending (0x1) は適用しない
    /// (draft-ietf-moq-transport-21 §10.5 (Default Publisher Group Order))。
    #[getter]
    fn default_publisher_group_order(&self) -> Option<u8> {
        self.inner.default_publisher_group_order()
    }

    /// OBJECT_DELIVERY_TIMEOUT (0x02): Object の配送期限 (ms)。
    #[getter]
    fn object_delivery_timeout(&self) -> Option<u64> {
        self.inner.object_delivery_timeout()
    }

    /// SUBGROUP_DELIVERY_TIMEOUT (0x06): Subgroup の配送期限 (ms)。
    #[getter]
    fn subgroup_delivery_timeout(&self) -> Option<u64> {
        self.inner.subgroup_delivery_timeout()
    }

    /// 未知の必須プロパティを含むか。
    ///
    /// 必須の範囲は `MANDATORY_TRACK_PROPERTY_MIN` から `MANDATORY_TRACK_PROPERTY_MAX`
    /// である (draft-ietf-moq-transport-21 §16.8 (Properties) Table 14)。未知の必須
    /// プロパティを含む Track は扱えないため、アプリは購読を拒否できる。
    #[getter]
    fn has_unknown_mandatory(&self) -> bool {
        self.inner.has_unknown_mandatory()
    }

    fn __len__(&self) -> usize {
        self.inner.len()
    }

    fn __repr__(&self) -> String {
        format!("TrackProperties(len={})", self.inner.len())
    }

    fn __eq__(&self, other: &Bound<'_, PyAny>) -> bool {
        match other.cast::<TrackProperties>() {
            Ok(other) => {
                sorted_track_properties(&self.inner)
                    == sorted_track_properties(&other.borrow().inner)
            }
            Err(_) => false,
        }
    }
}

/// プロパティを型番号の昇順に並べた列を返す。
fn sorted_track_properties(properties: &MoqtTrackProperties) -> Vec<TrackProperty> {
    let mut sorted: Vec<TrackProperty> = properties.as_slice().to_vec();
    sorted.sort_by_key(|property| property.prop_type);
    sorted
}

/// Properties の型番号をモジュール定数として登録する。
pub(crate) fn register_constants(module: &Bound<'_, PyModule>) -> PyResult<()> {
    // Object Properties (draft-ietf-moq-transport-21 §16.8 (Properties) Table 14)
    module.add("PROP_PRIOR_GROUP_ID_GAP", PROP_PRIOR_GROUP_ID_GAP)?;
    module.add("PROP_PRIOR_OBJECT_ID_GAP", PROP_PRIOR_OBJECT_ID_GAP)?;

    // Track Properties (draft-ietf-moq-transport-21 §16.8 (Properties) Table 14)
    module.add("PROP_OBJECT_DELIVERY_TIMEOUT", PROP_OBJECT_DELIVERY_TIMEOUT)?;
    module.add("PROP_MAX_CACHE_DURATION", PROP_MAX_CACHE_DURATION)?;
    module.add(
        "PROP_SUBGROUP_DELIVERY_TIMEOUT",
        PROP_SUBGROUP_DELIVERY_TIMEOUT,
    )?;
    module.add("PROP_IMMUTABLE_PROPERTIES", PROP_IMMUTABLE_PROPERTIES)?;
    module.add(
        "PROP_DEFAULT_PUBLISHER_PRIORITY",
        PROP_DEFAULT_PUBLISHER_PRIORITY,
    )?;
    module.add(
        "PROP_DEFAULT_PUBLISHER_GROUP_ORDER",
        PROP_DEFAULT_PUBLISHER_GROUP_ORDER,
    )?;
    module.add("PROP_DYNAMIC_GROUPS", PROP_DYNAMIC_GROUPS)?;
    module.add("MANDATORY_TRACK_PROPERTY_MIN", MANDATORY_TRACK_PROPERTY_MIN)?;
    module.add("MANDATORY_TRACK_PROPERTY_MAX", MANDATORY_TRACK_PROPERTY_MAX)?;

    Ok(())
}
