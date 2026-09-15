//! LOC (Low Overhead Media Container) のプロパティ codec (`moq.loc`)。
//!
//! draft-ietf-moq-loc-04 の LOC Properties を Python から encode / decode する。
//! この仕様は draft 由来であり、将来の改訂で変更される可能性がある。
//!
//! LOC Properties は Public と Private に分類される (draft-ietf-moq-loc-04 §2.2)。
//! どちらに置くかはアプリケーション層の責務であり、このモジュールは両者を区別せず
//! プロパティ列の encode / decode だけを提供する。

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};

use shiguredo_moqt::loc::{
    LocProperties as MoqtLocProperties, LocProperty, LocPropertyValue, PROP_AUDIO_CONFIG,
    PROP_AUDIO_LEVEL, PROP_TIMESCALE, PROP_TIMESTAMP, PROP_VIDEO_CONFIG, PROP_VIDEO_FRAME_MARKING,
};

use crate::errors::codec_error;

/// LOC プロパティの集合。
///
/// ワイヤフォーマットは `Properties Length (vi64) | Key-Value-Pairs...` である。
/// encode は prop_id の昇順にソートし、delta encoding で ID を圧縮する。
/// (draft-ietf-moq-loc-04 §2.3)
#[pyclass(name = "LocProperties")]
pub(crate) struct LocProperties {
    inner: MoqtLocProperties,
}

#[pymethods]
impl LocProperties {
    /// 空のプロパティ集合を作成する。
    #[new]
    fn new() -> Self {
        Self {
            inner: MoqtLocProperties::new(),
        }
    }

    /// プロパティを 1 件追加する。
    ///
    /// 偶数 ID は vi64、奇数 ID は長さ付きバイト列で表現する
    /// (draft-ietf-moq-loc-04 §2.3)。`value` には `int` または `bytes` を渡す。
    ///
    /// この時点では ID と値の型の対応を検査しない。対応が取れていないプロパティは
    /// `encode()` が `ValueError` で拒否する。不正な入力を意図的に組み立てて
    /// 検証したいテストのために、構築時ではなく encode 時に検査する。
    fn add(&mut self, prop_id: u64, value: &Bound<'_, PyAny>) -> PyResult<()> {
        let prop_value = if let Ok(bytes) = value.cast::<PyBytes>() {
            LocPropertyValue::Bytes(bytes.as_bytes().to_vec())
        } else if let Ok(number) = value.extract::<u64>() {
            LocPropertyValue::VarInt(number)
        } else {
            return Err(PyValueError::new_err(format!(
                "LOC property {prop_id:#x} requires an int or bytes value, got {}",
                value.get_type().name()?
            )));
        };
        self.inner.push(LocProperty {
            prop_id,
            value: prop_value,
        });
        Ok(())
    }

    /// プロパティブロック全体をエンコードする。
    ///
    /// 空の集合は Properties Length = 0 の 1 バイトになる。
    /// (draft-ietf-moq-transport-21 §11.3.1 (Subgroup Header))
    fn encode<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        let data = self.inner.encode().map_err(codec_error)?;
        Ok(PyBytes::new(py, &data))
    }

    /// バッファ先頭からプロパティブロックをデコードし `(プロパティ, 消費バイト数)` を返す。
    ///
    /// ブロックの後ろに続くバイト列は消費しない。
    #[staticmethod]
    fn decode(data: &[u8]) -> PyResult<(Self, usize)> {
        let (inner, consumed) = MoqtLocProperties::decode(data).map_err(codec_error)?;
        Ok((Self { inner }, consumed))
    }

    /// プロパティを `{prop_id: 値}` の辞書へ変換する。
    fn to_dict(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let dict = PyDict::new(py);
        for property in self.inner.iter() {
            match &property.value {
                LocPropertyValue::VarInt(value) => dict.set_item(property.prop_id, *value)?,
                LocPropertyValue::Bytes(value) => {
                    dict.set_item(property.prop_id, PyBytes::new(py, value))?
                }
            }
        }
        Ok(dict.unbind())
    }

    /// Timestamp (ID=0x10): エンコードされたメディアフレームのタイムスタンプ。
    ///
    /// Timescale が無い場合は Unix エポック以降のマイクロ秒、ある場合は
    /// Timescale 単位のメディア時刻として解釈する。
    /// (draft-ietf-moq-loc-04 §2.3.1.1)
    #[getter]
    fn timestamp(&self) -> Option<u64> {
        self.inner.timestamp()
    }

    /// Timescale (ID=0x08): Timestamp の単位 (1 秒あたりのユニット数)。
    ///
    /// 代表的な値は 1000000 (マイクロ秒)、48000、90000 である。
    /// (draft-ietf-moq-loc-04 §2.3.1.2)
    #[getter]
    fn timescale(&self) -> Option<u64> {
        self.inner.timescale()
    }

    /// Video Frame Marking (ID=0x09): RFC 9626 のビデオフレームフラグ。
    ///
    /// 内容は解釈せずバイト列として返す。長さは 1-4 バイトである。
    /// (draft-ietf-moq-loc-04 §2.3.2.2)
    #[getter]
    fn video_frame_marking<'py>(&self, py: Python<'py>) -> Option<Bound<'py, PyBytes>> {
        self.inner
            .video_frame_marking()
            .map(|v| PyBytes::new(py, v))
    }

    /// Audio Level (ID=0x0C): RFC 6464 の音声レベル (vi64 の下位 8 bit)。
    ///
    /// (draft-ietf-moq-loc-04 §2.3.3.2)
    #[getter]
    fn audio_level(&self) -> Option<u64> {
        self.inner.audio_level()
    }

    /// Video Config (ID=0x0D): ビデオコーデックの設定 (extradata)。
    ///
    /// (draft-ietf-moq-loc-04 §2.3.2.1)
    #[getter]
    fn video_config<'py>(&self, py: Python<'py>) -> Option<Bound<'py, PyBytes>> {
        self.inner.video_config().map(|v| PyBytes::new(py, v))
    }

    /// Audio Config (ID=0x0F): 音声コーデックの設定。
    ///
    /// (draft-ietf-moq-loc-04 §2.3.3.1)
    #[getter]
    fn audio_config<'py>(&self, py: Python<'py>) -> Option<Bound<'py, PyBytes>> {
        self.inner.audio_config().map(|v| PyBytes::new(py, v))
    }

    fn __len__(&self) -> usize {
        self.inner.len()
    }

    fn __repr__(&self) -> String {
        format!("LocProperties(len={})", self.inner.len())
    }

    fn __eq__(&self, other: &Bound<'_, PyAny>) -> bool {
        match other.cast::<LocProperties>() {
            // 追加した順序は wire 上の意味を持たない。encode が ID の昇順に並べる
            // ため、比較も ID の昇順に正規化してから行う。
            Ok(other) => sorted_by_id(&self.inner) == sorted_by_id(&other.borrow().inner),
            Err(_) => false,
        }
    }
}

/// プロパティを ID の昇順に並べた列を返す。
fn sorted_by_id(properties: &MoqtLocProperties) -> Vec<LocProperty> {
    let mut sorted: Vec<LocProperty> = properties.iter().cloned().collect();
    sorted.sort_by_key(|property| property.prop_id);
    sorted
}

/// LOC プロパティ ID をモジュール定数として登録する。
pub(crate) fn register_constants(module: &Bound<'_, PyModule>) -> PyResult<()> {
    // draft-ietf-moq-loc-04 §2.3
    module.add("LOC_PROP_TIMESTAMP", PROP_TIMESTAMP)?;
    module.add("LOC_PROP_TIMESCALE", PROP_TIMESCALE)?;
    module.add("LOC_PROP_VIDEO_FRAME_MARKING", PROP_VIDEO_FRAME_MARKING)?;
    module.add("LOC_PROP_AUDIO_LEVEL", PROP_AUDIO_LEVEL)?;
    module.add("LOC_PROP_VIDEO_CONFIG", PROP_VIDEO_CONFIG)?;
    module.add("LOC_PROP_AUDIO_CONFIG", PROP_AUDIO_CONFIG)?;
    Ok(())
}
