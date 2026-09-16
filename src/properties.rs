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
use pyo3::types::{PyBytes, PyDict, PyList, PyTuple};

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

/// Key-Value-Pair の値を Python のオブジェクトへ変換する。
///
/// 偶数型は `int`、奇数型は `bytes` になる。Object / Track の両方の列挙で同じ表現を
/// 使うため、値の変換をここへ集約する。
fn property_python_value(py: Python<'_>, value: &impl PropertyValue) -> PyResult<Py<PyAny>> {
    if let Some(number) = value.as_varint() {
        return Ok(number.into_pyobject(py)?.into_any().unbind());
    }
    let Some(bytes) = value.as_bytes() else {
        // 値は varint かバイト列のどちらかである
        return Err(PyValueError::new_err(
            "property value is neither a varint nor bytes",
        ));
    };
    Ok(PyBytes::new(py, bytes).into_any().unbind())
}

/// `(型番号, 値)` の組を Python のタプルとして組み立てる。
fn property_pair(
    py: Python<'_>,
    prop_type: u64,
    value: &impl PropertyValue,
) -> PyResult<Py<PyAny>> {
    let converted = property_python_value(py, value)?;
    let prop_type = prop_type.into_pyobject(py)?.into_any().unbind();
    Ok(PyTuple::new(py, [prop_type, converted])?
        .into_any()
        .unbind())
}

/// Key-Value-Pair の値の読み出し方を Object Properties と Track Properties で共有する。
///
/// 両者は値の型が別々の enum で表現されているが、Python へは同じ
/// `int` / `bytes` として渡すため、読み出しだけをこの trait で抽象化する。
trait PropertyValue {
    /// varint 値を返す。バイト列の場合は `None` になる。
    fn as_varint(&self) -> Option<u64>;

    /// バイト列を返す。varint の場合は `None` になる。
    fn as_bytes(&self) -> Option<&[u8]>;
}

impl PropertyValue for ObjectPropertyValue {
    fn as_varint(&self) -> Option<u64> {
        match self {
            Self::VarInt(number) => Some(*number),
            Self::Bytes(_) => None,
        }
    }

    fn as_bytes(&self) -> Option<&[u8]> {
        match self {
            Self::VarInt(_) => None,
            Self::Bytes(bytes) => Some(bytes),
        }
    }
}

impl PropertyValue for TrackPropertyValue {
    fn as_varint(&self) -> Option<u64> {
        match self {
            Self::VarInt(number) => Some(*number),
            Self::Bytes(_) => None,
        }
    }

    fn as_bytes(&self) -> Option<&[u8]> {
        match self {
            Self::VarInt(_) => None,
            Self::Bytes(bytes) => Some(bytes),
        }
    }
}

/// Object Properties の `(型番号, 値)` を追加順に列挙するイテレータ。
///
/// 列挙の途中で元の集合を `add()` で変更しても、列挙中の列は変わらない。
#[pyclass(name = "ObjectPropertiesIterator")]
pub(crate) struct ObjectPropertiesIterator {
    /// 列挙対象のスナップショット。
    items: Vec<ObjectProperty>,
    /// 次に返す位置。
    index: usize,
}

#[pymethods]
impl ObjectPropertiesIterator {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __next__(&mut self, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        let Some(property) = self.items.get(self.index) else {
            return Ok(None);
        };
        self.index += 1;
        Ok(Some(property_pair(
            py,
            property.prop_type,
            &property.value,
        )?))
    }

    fn __repr__(&self) -> String {
        format!(
            "ObjectPropertiesIterator(index={}, len={})",
            self.index,
            self.items.len()
        )
    }
}

/// Track Properties の `(型番号, 値)` を追加順に列挙するイテレータ。
///
/// 列挙の途中で元の集合を `add()` で変更しても、列挙中の列は変わらない。
#[pyclass(name = "TrackPropertiesIterator")]
pub(crate) struct TrackPropertiesIterator {
    /// 列挙対象のスナップショット。
    items: Vec<TrackProperty>,
    /// 次に返す位置。
    index: usize,
}

#[pymethods]
impl TrackPropertiesIterator {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __next__(&mut self, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        let Some(property) = self.items.get(self.index) else {
            return Ok(None);
        };
        self.index += 1;
        Ok(Some(property_pair(
            py,
            property.prop_type,
            &property.value,
        )?))
    }

    fn __repr__(&self) -> String {
        format!(
            "TrackPropertiesIterator(index={}, len={})",
            self.index,
            self.items.len()
        )
    }
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
    #[pyo3(name = "encode")]
    fn encode_properties<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
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

    /// プロパティを保持している順に `(型番号, 値)` として列挙する。
    ///
    /// ワイヤ上の型番号の昇順ではなく、`add()` で追加した順に返す。
    /// `list(properties)` も同じ列を返す。
    fn items<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let items = PyList::empty(py);
        for property in self.inner.iter() {
            items.append(property_pair(py, property.prop_type, &property.value)?)?;
        }
        Ok(items)
    }

    /// 任意の型番号の varint 値を引く。
    ///
    /// 見つからない場合と、その型番号の値がバイト列である場合は `None` になる。
    /// draft-ietf-moq-transport-21 §10.7 (Immutable Properties) の「MUST search both」
    /// に従い IMMUTABLE_PROPERTIES の内側も探索し、外側の値を優先する。
    fn find_varint(&self, prop_type: u64) -> Option<u64> {
        self.inner.find_varint(prop_type)
    }

    fn __iter__(&self) -> ObjectPropertiesIterator {
        ObjectPropertiesIterator {
            items: self.inner.as_slice().to_vec(),
            index: 0,
        }
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
        self.inner
            .find_bytes(PROP_IMMUTABLE_PROPERTIES)
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
        self.encode_properties(py)
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

/// Object Properties の内部参照を Rust 側だけで共有する。
///
/// `#[pymethods]` に置いたメソッドは Python の公開 API になり型スタブにも現れる。
/// Python から使わせる必要の無い参照はこの trait 側に置く。
trait ObjectPropertiesExt {
    /// バイト列型のプロパティを引く。
    fn find_bytes(&self, prop_type: u64) -> Option<&[u8]>;
}

impl ObjectPropertiesExt for MoqtObjectProperties {
    fn find_bytes(&self, prop_type: u64) -> Option<&[u8]> {
        self.iter().find_map(|property| {
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

    /// プロパティ列をエンコードする。
    ///
    /// Object Properties と異なり長さプレフィックスを付けない。カウントプレフィックスを
    /// 持たない KVP 列そのものになり、空の集合は 0 バイトになる
    /// (draft-ietf-moq-transport-21 §8.4 (Track and Object Properties))。
    /// subscription を送る引数へ埋め込むバイト列がこれである。
    fn encode<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        let mut buf = Vec::new();
        self.inner.encode(&mut buf).map_err(codec_error)?;
        Ok(PyBytes::new(py, &buf))
    }

    /// バッファ全体を Track Properties としてデコードする。
    ///
    /// 長さプレフィックスが無いためバッファ末尾まで読む。空のバッファは空の集合になる。
    /// 型番号の重複など draft の MUST に違反する入力は `ValueError` になる。
    #[staticmethod]
    fn decode(data: &[u8]) -> PyResult<Self> {
        let inner = MoqtTrackProperties::decode(data).map_err(codec_error)?;
        Ok(Self { inner })
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

    /// プロパティを保持している順に `(型番号, 値)` として列挙する。
    ///
    /// ワイヤ上の型番号の昇順ではなく、`add()` で追加した順に返す。
    /// `list(properties)` も同じ列を返す。
    fn items<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let items = PyList::empty(py);
        for property in self.inner.as_slice() {
            let value = match &property.value {
                TrackPropertyValue::VarInt(number) => ObjectPropertyValue::VarInt(*number),
                TrackPropertyValue::Bytes(bytes) => ObjectPropertyValue::Bytes(bytes.clone()),
            };
            items.append(property_pair(py, property.prop_type, &value)?)?;
        }
        Ok(items)
    }

    /// 任意の型番号の varint 値を引く。
    ///
    /// 見つからない場合と、その型番号の値がバイト列である場合は `None` になる。
    /// IMMUTABLE_PROPERTIES の内側も探索し、外側の値を優先する
    /// (draft-ietf-moq-transport-21 §10.7 (Immutable Properties))。
    fn find_varint(&self, prop_type: u64) -> Option<u64> {
        self.inner.find_varint(prop_type)
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

    fn __iter__(&self) -> TrackPropertiesIterator {
        TrackPropertiesIterator {
            items: self.inner.as_slice().to_vec(),
            index: 0,
        }
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
