//! MSF (MOQT Streaming Format) の codec (`moqt.msf`)。
//!
//! draft-ietf-moq-msf-01 のカタログ・メディアタイムライン・イベントタイムライン・
//! URI を Python から扱う。この仕様は draft 由来であり、将来の改訂で変更される
//! 可能性がある。
//!
//! カタログもタイムラインも JSON 文書である。Rust 側の役割は draft の MUST に
//! 照らした検証と、delta 更新の適用である。フィールドの読み出しは JSON へ戻して
//! Python の `json` モジュールに渡す。フィールド名が draft の表記と必ず一致し、
//! ラッパ側で写し間違えが起きないようにするためである。

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};

use shiguredo_moqt::msf::{
    self, MSF_CATALOG_TRACK_NAME, MSF_VERSION, MsfCatalog, MsfCatalogDocument, MsfDeltaUpdate,
    MsfEventIndex, MsfEventTimeline, MsfEventTimelineEntry, MsfMediaTimeline,
    MsfMediaTimelineEntry, TimelineEncodingOptions, uri,
};

use crate::core::track_namespace_to_python;
use crate::errors::codec_error;

/// JSON バイト列を Python の値へ変換する。
fn json_to_python(py: Python<'_>, data: &[u8]) -> PyResult<Py<PyAny>> {
    let text = std::str::from_utf8(data)
        .map_err(|_| PyValueError::new_err("MSF JSON is not valid UTF-8"))?;
    let value = py.import("json")?.call_method1("loads", (text,))?;
    Ok(value.unbind())
}

/// JSON オブジェクトの member を Python の値として取り出す。
///
/// member が無い場合は `default` を返す。draft は空配列の member を省略できる。
fn json_member(
    py: Python<'_>,
    text: &str,
    key: &str,
    default: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let document = py.import("json")?.call_method1("loads", (text,))?;
    Ok(document.call_method1("get", (key, default))?.unbind())
}

/// MSF カタログ。
///
/// draft-ietf-moq-msf-01 §5 (Catalog) の完全カタログである。delta 更新は
/// [`DeltaUpdate`] で読み込み、[`Catalog::apply_delta`] で適用する。
#[pyclass(name = "Catalog")]
pub(crate) struct Catalog {
    inner: MsfCatalog,
}

#[pymethods]
impl Catalog {
    /// 空のカタログを作成する。
    ///
    /// version は対応する MSF バージョン、tracks / publishTracks / initDataList は
    /// 空になる。
    #[new]
    fn new() -> Self {
        Self {
            inner: MsfCatalog::new(),
        }
    }

    /// JSON バイト列からカタログを読み込む。
    ///
    /// draft の MUST に違反する文書は `ValueError` になる。delta 更新の文書を
    /// 渡した場合は [`DeltaUpdate`] を使うよう促す `ValueError` になる。
    #[staticmethod]
    fn decode(data: &[u8]) -> PyResult<Self> {
        match MsfCatalogDocument::decode(data).map_err(codec_error)? {
            MsfCatalogDocument::Full(catalog) => Ok(Self { inner: catalog }),
            MsfCatalogDocument::Delta(_) => Err(PyValueError::new_err(
                "the document is a delta update; read it with DeltaUpdate and apply it with Catalog.apply_delta",
            )),
        }
    }

    /// JSON 文字列からカタログを読み込む。
    #[staticmethod]
    fn parse(text: &str) -> PyResult<Self> {
        Self::decode(text.as_bytes())
    }

    /// カタログを JSON バイト列へ書き出す。
    ///
    /// 書き出す前に draft の MUST を検証する。手組みの不正な値は `ValueError` になる。
    fn encode<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        let document = MsfCatalogDocument::Full(self.inner.clone());
        let data = document.encode().map_err(codec_error)?;
        Ok(PyBytes::new(py, &data))
    }

    /// delta 更新をこのカタログへ適用する。
    ///
    /// `namespace` はカタログトラック自身のネームスペースであり、トラックが
    /// namespace を省略した場合の継承先として使う (draft-ietf-moq-msf-01 §5.2.2)。
    ///
    /// 操作は配列順に適用される。途中で失敗した場合、それまでの操作は取り消されない
    /// (draft-ietf-moq-msf-01 §5.1.6)。差し替え前の状態を保ちたい場合は、適用前に
    /// 呼び出し側でカタログを複製すること。
    #[pyo3(signature = (text, namespace = None))]
    fn apply_delta(&mut self, text: &str, namespace: Option<&str>) -> PyResult<()> {
        let delta = match MsfCatalogDocument::decode(text.as_bytes()).map_err(codec_error)? {
            MsfCatalogDocument::Delta(delta) => delta,
            MsfCatalogDocument::Full(_) => {
                return Err(PyValueError::new_err(
                    "expected a delta update document, got a full catalog",
                ));
            }
        };
        self.inner
            .apply_delta(&delta, namespace)
            .map_err(codec_error)
    }

    /// MSF バージョン (draft-ietf-moq-msf-01 §5.1.1)。
    #[getter]
    fn version(&self) -> &str {
        &self.inner.version
    }

    /// カタログ生成時刻 (ms) (draft-ietf-moq-msf-01 §5.1.2)。
    #[getter]
    fn generated_at(&self) -> Option<u64> {
        self.inner.generated_at
    }

    /// ブロードキャストが完了しているか (draft-ietf-moq-msf-01 §5.1.3)。
    #[getter]
    fn is_complete(&self) -> bool {
        self.inner.is_complete
    }

    /// トラック一覧 (draft-ietf-moq-msf-01 §5.1.4)。
    ///
    /// draft のフィールド名を持つ辞書のリストとして返す。
    #[getter]
    fn tracks(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let text = self.to_json()?;
        let default = pyo3::types::PyList::empty(py).into_any();
        json_member(py, &text, "tracks", &default)
    }

    /// publish track 一覧 (draft-ietf-moq-msf-01 §5.1.5)。
    #[getter]
    fn publish_tracks(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let text = self.to_json()?;
        let default = pyo3::types::PyList::empty(py).into_any();
        json_member(py, &text, "publishTracks", &default)
    }

    /// 初期化データ一覧 (draft-ietf-moq-msf-01 §5.1.7)。
    #[getter]
    fn init_data_list(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let text = self.to_json()?;
        let default = pyo3::types::PyList::empty(py).into_any();
        json_member(py, &text, "initDataList", &default)
    }

    fn __repr__(&self) -> String {
        format!(
            "Catalog(version={}, tracks={}, is_complete={})",
            self.inner.version,
            self.inner.tracks.len(),
            self.inner.is_complete
        )
    }

    fn __eq__(&self, other: &Bound<'_, PyAny>) -> bool {
        match other.cast::<Catalog>() {
            Ok(other) => self.inner == other.borrow().inner,
            Err(_) => false,
        }
    }
}

impl Catalog {
    /// カタログを JSON 文字列へ書き出す。
    fn to_json(&self) -> PyResult<String> {
        let document = MsfCatalogDocument::Full(self.inner.clone());
        let data = document.encode().map_err(codec_error)?;
        String::from_utf8(data).map_err(|_| PyValueError::new_err("MSF catalog is not valid UTF-8"))
    }
}

/// MSF の delta 更新。
///
/// draft-ietf-moq-msf-01 §5.1.6 (Delta update) の文書である。
#[pyclass(name = "DeltaUpdate")]
pub(crate) struct DeltaUpdate {
    inner: MsfDeltaUpdate,
}

#[pymethods]
impl DeltaUpdate {
    /// JSON バイト列から delta 更新を読み込む。
    ///
    /// 完全カタログの文書を渡した場合は [`Catalog`] を使うよう促す `ValueError` になる。
    #[staticmethod]
    fn decode(data: &[u8]) -> PyResult<Self> {
        match MsfCatalogDocument::decode(data).map_err(codec_error)? {
            MsfCatalogDocument::Delta(delta) => Ok(Self { inner: delta }),
            MsfCatalogDocument::Full(_) => Err(PyValueError::new_err(
                "the document is a full catalog; read it with Catalog",
            )),
        }
    }

    /// JSON 文字列から delta 更新を読み込む。
    #[staticmethod]
    fn parse(text: &str) -> PyResult<Self> {
        Self::decode(text.as_bytes())
    }

    /// delta 更新を JSON バイト列へ書き出す。
    fn encode<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        let document = MsfCatalogDocument::Delta(self.inner.clone());
        let data = document.encode().map_err(codec_error)?;
        Ok(PyBytes::new(py, &data))
    }

    /// カタログ生成時刻 (ms) (draft-ietf-moq-msf-01 §5.1.6)。
    #[getter]
    fn generated_at(&self) -> Option<u64> {
        self.inner.generated_at
    }

    /// 操作列 (draft-ietf-moq-msf-01 §5.1.6)。
    ///
    /// draft のフィールド名を持つ辞書のリストとして返す。操作は配列順に適用される。
    #[getter]
    fn operations(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let document = MsfCatalogDocument::Delta(self.inner.clone());
        let data = document.encode().map_err(codec_error)?;
        json_to_python(py, &data)
    }

    fn __repr__(&self) -> String {
        format!("DeltaUpdate(operations={})", self.inner.operations.len())
    }

    fn __eq__(&self, other: &Bound<'_, PyAny>) -> bool {
        match other.cast::<DeltaUpdate>() {
            Ok(other) => self.inner == other.borrow().inner,
            Err(_) => false,
        }
    }
}

/// MSF メディアタイムライン (draft-ietf-moq-msf-01 §7.1)。
///
/// フォーマットは `[[pts_ms, [group_id, object_id], wallclock_ms], ...]` である。
#[pyclass(name = "MediaTimeline")]
pub(crate) struct MediaTimeline {
    inner: MsfMediaTimeline,
}

#[pymethods]
impl MediaTimeline {
    /// 空のメディアタイムラインを作成する。
    #[new]
    fn new() -> Self {
        Self {
            inner: MsfMediaTimeline::new(),
        }
    }

    /// エントリを末尾に追加する。
    ///
    /// `wallclock_ms` が不明な場合は 0 を渡す。
    fn add(&mut self, pts_ms: u64, group_id: u64, object_id: u64, wallclock_ms: u64) {
        self.inner.0.push(MsfMediaTimelineEntry {
            pts_ms,
            group_id,
            object_id,
            wallclock_ms,
        });
    }

    /// エントリ列を `(pts_ms, group_id, object_id, wallclock_ms)` のリストとして返す。
    #[getter]
    fn entries(&self) -> Vec<(u64, u64, u64, u64)> {
        self.inner
            .0
            .iter()
            .map(|entry| {
                (
                    entry.pts_ms,
                    entry.group_id,
                    entry.object_id,
                    entry.wallclock_ms,
                )
            })
            .collect()
    }

    /// メディアタイムラインを JSON バイト列へ書き出す。
    ///
    /// `gzip` が真の場合は gzip で圧縮する (draft-ietf-moq-msf-01 §7.1)。
    #[pyo3(signature = (gzip = false))]
    fn encode<'py>(&self, py: Python<'py>, gzip: bool) -> PyResult<Bound<'py, PyBytes>> {
        let data = msf::encode_media_timeline(&self.inner, TimelineEncodingOptions { gzip })
            .map_err(codec_error)?;
        Ok(PyBytes::new(py, &data))
    }

    /// JSON バイト列からメディアタイムラインを読み込む。
    ///
    /// gzip で圧縮された入力は自動的に展開する。
    #[staticmethod]
    fn decode(data: &[u8]) -> PyResult<Self> {
        let inner = msf::decode_media_timeline(data).map_err(codec_error)?;
        Ok(Self { inner })
    }

    fn __len__(&self) -> usize {
        self.inner.0.len()
    }

    fn __repr__(&self) -> String {
        format!("MediaTimeline(len={})", self.inner.0.len())
    }

    fn __eq__(&self, other: &Bound<'_, PyAny>) -> bool {
        match other.cast::<MediaTimeline>() {
            Ok(other) => self.inner == other.borrow().inner,
            Err(_) => false,
        }
    }
}

/// MSF イベントタイムライン (draft-ietf-moq-msf-01 §8.1)。
///
/// フォーマットは `[{"t"/"l"/"m": ..., "data": ...}, ...]` である。
#[pyclass(name = "EventTimeline")]
pub(crate) struct EventTimeline {
    inner: MsfEventTimeline,
}

#[pymethods]
impl EventTimeline {
    /// 空のイベントタイムラインを作成する。
    #[new]
    fn new() -> Self {
        Self {
            inner: MsfEventTimeline::new(),
        }
    }

    /// MOQT Location を指すエントリを追加する ('l')。
    fn add_location(&mut self, group_id: u64, object_id: u64, data: &str) {
        self.push(MsfEventIndex::Location(group_id, object_id), data);
    }

    /// ウォールクロック (ms) を指すエントリを追加する ('t')。
    fn add_wallclock(&mut self, wallclock_ms: u64, data: &str) {
        self.push(MsfEventIndex::WallclockMs(wallclock_ms), data);
    }

    /// メディア PTS (ms) を指すエントリを追加する ('m')。
    fn add_media_pts(&mut self, pts_ms: u64, data: &str) {
        self.push(MsfEventIndex::MediaPtsMs(pts_ms), data);
    }

    /// エントリ列を draft のフィールド名を持つ辞書のリストとして返す。
    #[getter]
    fn entries(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let data = self.inner.encode().map_err(codec_error)?;
        json_to_python(py, &data)
    }

    /// イベントタイムラインを JSON バイト列へ書き出す。
    ///
    /// 各エントリの `data` は単一の JSON object でなければならない。そうでない
    /// 場合は `ValueError` になる。`gzip` が真の場合は gzip で圧縮する。
    #[pyo3(signature = (gzip = false))]
    fn encode<'py>(&self, py: Python<'py>, gzip: bool) -> PyResult<Bound<'py, PyBytes>> {
        let data = msf::encode_event_timeline(&self.inner, TimelineEncodingOptions { gzip })
            .map_err(codec_error)?;
        Ok(PyBytes::new(py, &data))
    }

    /// JSON バイト列からイベントタイムラインを読み込む。
    ///
    /// gzip で圧縮された入力は自動的に展開する。
    #[staticmethod]
    fn decode(data: &[u8]) -> PyResult<Self> {
        let inner = msf::decode_event_timeline(data).map_err(codec_error)?;
        Ok(Self { inner })
    }

    fn __len__(&self) -> usize {
        self.inner.0.len()
    }

    fn __repr__(&self) -> String {
        format!("EventTimeline(len={})", self.inner.0.len())
    }

    fn __eq__(&self, other: &Bound<'_, PyAny>) -> bool {
        match other.cast::<EventTimeline>() {
            Ok(other) => self.inner == other.borrow().inner,
            Err(_) => false,
        }
    }
}

impl EventTimeline {
    /// エントリを末尾に追加する。
    ///
    /// `data` の検証は encode 時に行う。不正な JSON を意図的に組み立てて検証したい
    /// テストのために、構築時ではなく encode 時に検査する。
    fn push(&mut self, index: MsfEventIndex, data: &str) {
        self.inner.0.push(MsfEventTimelineEntry {
            index,
            data_raw: data.as_bytes().to_vec(),
        });
    }
}

/// パース済みの MSF URI (draft-ietf-moq-msf-01 §11.1)。
///
/// `moqt://` URI の fragment (`msf:...`) をパースした結果である。percent-decode は
/// 行わず、値をそのまま保持する。
#[pyclass(name = "Uri", frozen)]
pub(crate) struct Uri {
    inner: uri::MsfUri,
}

#[pymethods]
impl Uri {
    /// MSF URI をパースする。
    ///
    /// draft-ietf-moq-msf-01 §11.1 の
    /// `msf-uri = "moqt://" authority path-abempty [ "?" query ] "#" msf-fragment`
    /// に従う。scheme は case-insensitive である。
    #[staticmethod]
    fn parse(uri: &str) -> PyResult<Self> {
        let inner = uri::parse_msf_uri(uri).map_err(codec_error)?;
        Ok(Self { inner })
    }

    /// authority (host + 任意の port)。
    #[getter]
    fn authority(&self) -> &str {
        &self.inner.authority
    }

    /// path (先頭の `/` を含む。無い場合は空文字列)。
    #[getter]
    fn path(&self) -> &str {
        &self.inner.path
    }

    /// query (`?` 以降。無い場合は `None`)。
    #[getter]
    fn query(&self) -> Option<&str> {
        self.inner.query.as_deref()
    }

    /// track-identifier を分解したネームスペース。
    #[getter]
    fn namespace<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, pyo3::types::PyList>> {
        track_namespace_to_python(py, &self.inner.fragment.namespace)
            .map(|value| value.into_bound(py))
    }

    /// track-identifier を分解した Track 名。
    #[getter]
    fn track_name<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.inner.fragment.track_name)
    }

    /// fragment パラメータ列を `(名前, 値)` のリストとして返す。出現順である。
    #[getter]
    fn parameters(&self) -> Vec<(String, String)> {
        self.inner
            .fragment
            .parameters
            .iter()
            .map(|parameter| (parameter.name.clone(), parameter.value.clone()))
            .collect()
    }

    /// 指定名のパラメータ値を出現順に返す。
    #[pyo3(signature = (name))]
    fn parameter_values(&self, name: &str) -> Vec<String> {
        self.inner
            .fragment
            .parameter_values(name)
            .into_iter()
            .map(str::to_string)
            .collect()
    }

    /// connection パラメータが要求する接続種別を出現順に返す。
    ///
    /// 値は `quic` または `webtransport` である。
    fn connection_types(&self) -> PyResult<Vec<&'static str>> {
        let types = self
            .inner
            .fragment
            .connection_types()
            .map_err(codec_error)?;
        Ok(types
            .into_iter()
            .map(|connection| match connection {
                uri::MsfConnectionType::Quic => "quic",
                uri::MsfConnectionType::WebTransport => "webtransport",
            })
            .collect())
    }

    /// wallclock-range パラメータを `(開始 ms, 終了 ms)` のリストとして返す。
    ///
    /// 終了が省略された open range の終了は `None` になる。
    fn wallclock_ranges(&self) -> PyResult<Vec<(u64, Option<u64>)>> {
        let ranges = self
            .inner
            .fragment
            .wallclock_ranges()
            .map_err(codec_error)?;
        Ok(ranges
            .into_iter()
            .map(|range| (range.start_ms, range.end_ms))
            .collect())
    }

    /// mediatime-range パラメータを `(開始 ms, 終了 ms)` のリストとして返す。
    fn mediatime_ranges(&self) -> PyResult<Vec<(u64, Option<u64>)>> {
        let ranges = self
            .inner
            .fragment
            .mediatime_ranges()
            .map_err(codec_error)?;
        Ok(ranges
            .into_iter()
            .map(|range| (range.start_ms, range.end_ms))
            .collect())
    }

    /// location-range パラメータを辞書のリストとして返す。
    ///
    /// 各辞書は `start_group_id` / `start_object_id` / `end_group_id` /
    /// `end_object_id` を持つ。省略された要素は `None` になる。
    fn location_ranges(&self, py: Python<'_>) -> PyResult<Vec<Py<PyDict>>> {
        let ranges = self.inner.fragment.location_ranges().map_err(codec_error)?;
        let mut result = Vec::with_capacity(ranges.len());
        for range in ranges {
            let entry = PyDict::new(py);
            entry.set_item("start_group_id", range.start_group_id)?;
            entry.set_item("start_object_id", range.start_object_id)?;
            match range.end {
                Some(end) => {
                    entry.set_item("end_group_id", end.group_id)?;
                    entry.set_item("end_object_id", end.object_id)?;
                }
                None => {
                    entry.set_item("end_group_id", py.None())?;
                    entry.set_item("end_object_id", py.None())?;
                }
            }
            result.push(entry.unbind());
        }
        Ok(result)
    }

    /// c4m パラメータの値を出現順に返す。
    fn c4m_tokens(&self) -> Vec<String> {
        self.inner
            .fragment
            .c4m_tokens()
            .into_iter()
            .map(str::to_string)
            .collect()
    }

    fn __repr__(&self) -> String {
        format!(
            "Uri(authority={}, path={})",
            self.inner.authority, self.inner.path
        )
    }

    fn __eq__(&self, other: &Bound<'_, PyAny>) -> bool {
        match other.cast::<Uri>() {
            Ok(other) => self.inner == other.borrow().inner,
            Err(_) => false,
        }
    }
}

/// fragment を `&` 区切りのパラメータ列へ分解する
/// (draft-ietf-moq-msf-01 §11.1)。
///
/// `msf:` prefix の検証は行わず、`&` 区切りの `名前=値` を取り出すだけである。
#[pyfunction]
pub(crate) fn parse_fragment_pairs(fragment: &str) -> PyResult<Vec<(String, String)>> {
    msf::parse_fragment_pairs(fragment).map_err(codec_error)
}

/// カタログの変数参照を fragment の値で解決する
/// (draft-ietf-moq-msf-01 §5.4 (Catalog variables))。
#[pyfunction]
pub(crate) fn resolve_catalog_variables<'py>(
    py: Python<'py>,
    document: &[u8],
    fragment: &str,
) -> PyResult<Bound<'py, PyBytes>> {
    let resolved = msf::resolve_catalog_variables(document, fragment).map_err(codec_error)?;
    Ok(PyBytes::new(py, &resolved))
}

/// MSF の定数をモジュール定数として登録する。
pub(crate) fn register_constants(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("MSF_VERSION", MSF_VERSION)?;
    module.add(
        "MSF_CATALOG_TRACK_NAME",
        PyBytes::new(module.py(), MSF_CATALOG_TRACK_NAME),
    )?;
    Ok(())
}
