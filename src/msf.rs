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
use pyo3::types::{PyBytes, PyDict, PyList};

use shiguredo_moqt::msf::{
    self, MSF_CATALOG_TRACK_NAME, MSF_VERSION, MsfAccessibility, MsfAuthInfo, MsfBuffers,
    MsfCatalog, MsfCatalogDocument, MsfCloneTrack, MsfDeltaOperation, MsfDeltaUpdate,
    MsfEventIndex, MsfEventTimeline, MsfEventTimelineEntry, MsfInitData, MsfInitDataKind,
    MsfMediaTimeline, MsfMediaTimelineEntry, MsfPackaging, MsfRemoveTrack, MsfTemplate, MsfTrack,
    TimelineEncodingOptions, uri,
};
use shiguredo_moqt::name;

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

/// draft-ietf-moq-msf-01 §5.2.4 (Packaging) の packaging 文字列を `MsfPackaging` へ変換する。
///
/// 許容値は `loc` / `mediatimeline` / `eventtimeline` / `moqlog` / `moqmetrics` である。
/// この仕様は draft 由来であり、将来の改訂で変更される可能性がある。
fn packaging_from_string(value: &str) -> PyResult<MsfPackaging> {
    match value {
        "loc" => Ok(MsfPackaging::Loc),
        "mediatimeline" => Ok(MsfPackaging::MediaTimeline),
        "eventtimeline" => Ok(MsfPackaging::EventTimeline),
        "moqlog" => Ok(MsfPackaging::MoqLog),
        "moqmetrics" => Ok(MsfPackaging::MoqMetrics),
        other => Err(PyValueError::new_err(format!(
            "unknown MSF packaging '{other}': expected one of loc / mediatimeline / eventtimeline / moqlog / moqmetrics"
        ))),
    }
}

/// `Option` を Python の値と同じ表記で書き出す。
///
/// `repr` は対話環境やログ、テストの失敗メッセージに出る。Rust の `Some(..)` ではなく
/// Python の `None` と同じ表記にして、利用者が Python の値として読めるようにする。
fn format_optional<T: std::fmt::Display>(value: Option<T>) -> String {
    match value {
        Some(value) => value.to_string(),
        None => "None".to_string(),
    }
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

    /// [`DeltaUpdate`] が組み立てた delta 更新をこのカタログへ適用する。
    ///
    /// 適用規則は [`Catalog::apply_delta`] と同じである。JSON 文字列を経由せずに
    /// 組み立てた操作を適用する場合に使う。
    #[pyo3(signature = (delta, namespace = None))]
    fn apply_delta_update(
        &mut self,
        delta: &Bound<'_, DeltaUpdate>,
        namespace: Option<&str>,
    ) -> PyResult<()> {
        let delta = delta.borrow();
        self.inner
            .apply_delta(&delta.inner, namespace)
            .map_err(codec_error)
    }

    /// トラックを `tracks` へ追加する。
    ///
    /// draft の MUST に照らした検証は encode 時に行う。`packaging` が draft
    /// §5.2.4 の許容値でない場合と、トラックのフィールドの型が合わない場合は
    /// この時点で `ValueError` になる。
    fn add_track(&mut self, track: &Bound<'_, Track>) -> PyResult<()> {
        let track = track.borrow().to_inner(track.py())?;
        self.inner.tracks.push(track);
        Ok(())
    }

    /// トラックを `publishTracks` へ追加する
    /// (draft-ietf-moq-msf-01 §5.1.5 (Publish tracks))。
    fn add_publish_track(&mut self, track: &Bound<'_, Track>) -> PyResult<()> {
        let track = track.borrow().to_inner(track.py())?;
        self.inner.publish_tracks.push(track);
        Ok(())
    }

    /// 初期化データを `initDataList` へ追加する
    /// (draft-ietf-moq-msf-01 §5.1.7 (Initialization Data List))。
    ///
    /// トラックの `init_ref` が指す id をここで登録する。登録の無い id を指す
    /// `init_ref` は encode 時に `ValueError` になる。
    fn add_init_data(&mut self, init_data: &Bound<'_, InitData>) -> PyResult<()> {
        let init_data = init_data.borrow().to_inner();
        self.inner.init_data_list.push(init_data);
        Ok(())
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

    /// カタログ生成時刻 (ms) を設定する (draft-ietf-moq-msf-01 §5.1.2)。
    #[setter]
    fn set_generated_at(&mut self, value: Option<u64>) {
        self.inner.generated_at = value;
    }

    /// ブロードキャストが完了しているか (draft-ietf-moq-msf-01 §5.1.3)。
    #[getter]
    fn is_complete(&self) -> bool {
        self.inner.is_complete
    }

    /// ブロードキャストが完了しているかを設定する (draft-ietf-moq-msf-01 §5.1.3)。
    ///
    /// 真にすると、それ以降の delta 更新によるトラックの追加と複製が拒否される。
    #[setter]
    fn set_is_complete(&mut self, value: bool) {
        self.inner.is_complete = value;
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
/// draft-ietf-moq-msf-01 §5.1.6 (Delta update) の文書である。JSON から読み込むほかに、
/// [`DeltaUpdate::add_tracks`] / [`DeltaUpdate::remove_tracks`] /
/// [`DeltaUpdate::clone_tracks`] で操作列を組み立てられる。
#[pyclass(name = "DeltaUpdate")]
pub(crate) struct DeltaUpdate {
    inner: MsfDeltaUpdate,
}

#[pymethods]
impl DeltaUpdate {
    /// 空の delta 更新を作成する。
    ///
    /// 操作を持たない delta 更新は draft §5.3 が許さないため、そのまま encode すると
    /// `ValueError` になる。少なくとも 1 つの操作を追加すること。
    #[new]
    fn new() -> Self {
        Self {
            inner: MsfDeltaUpdate {
                generated_at: None,
                operations: Vec::new(),
            },
        }
    }

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
    ///
    /// 書き出す前に draft の MUST を検証する。手組みの不正な値は `ValueError` になる。
    fn encode<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        let document = MsfCatalogDocument::Delta(self.inner.clone());
        let data = document.encode().map_err(codec_error)?;
        Ok(PyBytes::new(py, &data))
    }

    /// トラックを追加する操作 ("add") を操作列の末尾へ追加する
    /// (draft-ietf-moq-msf-01 §5.1.6 (Delta update))。
    ///
    /// 操作は配列順に適用されるため、追加した順が適用順になる。
    fn add_tracks(&mut self, tracks: Vec<Bound<'_, Track>>) -> PyResult<()> {
        let mut converted = Vec::new();
        for track in &tracks {
            converted.push(track.borrow().to_inner(track.py())?);
        }
        self.inner
            .operations
            .push(MsfDeltaOperation::Add { tracks: converted });
        Ok(())
    }

    /// トラックを削除する操作 ("remove") を操作列の末尾へ追加する
    /// (draft-ietf-moq-msf-01 §5.1.6 (Delta update))。
    fn remove_tracks(&mut self, tracks: Vec<Bound<'_, RemoveTrack>>) -> PyResult<()> {
        let mut converted = Vec::new();
        for track in &tracks {
            converted.push(track.borrow().to_inner());
        }
        self.inner
            .operations
            .push(MsfDeltaOperation::Remove { tracks: converted });
        Ok(())
    }

    /// トラックを複製する操作 ("clone") を操作列の末尾へ追加する
    /// (draft-ietf-moq-msf-01 §5.1.6 (Delta update))。
    ///
    /// 複製は親トラックの属性を継承し、再定義した属性だけを上書きする。
    fn clone_tracks(&mut self, tracks: Vec<Bound<'_, CloneTrack>>) -> PyResult<()> {
        let mut converted = Vec::new();
        for track in &tracks {
            converted.push(track.borrow().to_inner(track.py())?);
        }
        self.inner
            .operations
            .push(MsfDeltaOperation::Clone { tracks: converted });
        Ok(())
    }

    /// カタログ生成時刻 (ms) (draft-ietf-moq-msf-01 §5.1.6)。
    #[getter]
    fn generated_at(&self) -> Option<u64> {
        self.inner.generated_at
    }

    /// カタログ生成時刻 (ms) を設定する (draft-ietf-moq-msf-01 §5.1.6)。
    #[setter]
    fn set_generated_at(&mut self, value: Option<u64>) {
        self.inner.generated_at = value;
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

/// MSF のターゲットバッファ (draft-ietf-moq-msf-01 §5.2.9 (Buffers))。
///
/// [`Track::buffers`] に設定する。draft は target / min / max を省略可能な
/// フィールドとして定義する。
///
/// この仕様は draft 由来であり、将来の改訂で変更される可能性がある。
#[pyclass(name = "Buffers", get_all, set_all)]
pub(crate) struct Buffers {
    /// 目標バッファ (ms)。
    pub target: Option<u64>,
    /// 最小バッファ (ms)。
    pub min: Option<u64>,
    /// 最大バッファ (ms)。
    pub max: Option<u64>,
}

impl Buffers {
    /// `MsfBuffers` へ変換する。
    fn to_inner(&self) -> MsfBuffers {
        MsfBuffers {
            target: self.target,
            min: self.min,
            max: self.max,
        }
    }
}

#[pymethods]
impl Buffers {
    /// バッファを組み立てる。
    #[new]
    #[pyo3(signature = (target = None, min = None, max = None))]
    fn new(target: Option<u64>, min: Option<u64>, max: Option<u64>) -> Self {
        Self { target, min, max }
    }

    fn __repr__(&self) -> String {
        format!(
            "Buffers(target={}, min={}, max={})",
            format_optional(self.target),
            format_optional(self.min),
            format_optional(self.max)
        )
    }

    fn __eq__(&self, other: &Bound<'_, PyAny>) -> bool {
        match other.cast::<Buffers>() {
            Ok(other) => self.to_inner() == other.borrow().to_inner(),
            Err(_) => false,
        }
    }
}

/// MSF のメディアタイムラインテンプレート
/// (draft-ietf-moq-msf-01 §5.2.15 (Template) / §7.4.1 (Template Format))。
///
/// [`Track::template`] に設定する。8 つの値は JSON では 6 要素の配列であり、
/// `[start_media_time, delta_media_time, [start_group_id, start_object_id],
/// [delta_group_id, delta_object_id], start_wallclock, delta_wallclock]` の順に並ぶ。
/// n 番目のエントリの計算式は群のフィールドから
/// [`Template::resolve_entry`] で求める。
///
/// この仕様は draft 由来であり、将来の改訂で変更される可能性がある。
#[pyclass(name = "Template", get_all, set_all)]
pub(crate) struct Template {
    /// 開始メディア時刻 (ms)。
    pub start_media_time: u64,
    /// メディア時刻の増分 (ms)。
    pub delta_media_time: u64,
    /// 開始 Group ID。
    pub start_group_id: u64,
    /// 開始 Object ID。
    pub start_object_id: u64,
    /// Group ID の増分。
    pub delta_group_id: u64,
    /// Object ID の増分。
    pub delta_object_id: u64,
    /// 開始ウォールクロック (ms)。
    pub start_wallclock: u64,
    /// ウォールクロックの増分 (ms)。
    pub delta_wallclock: u64,
}

impl Template {
    /// `MsfTemplate` へ変換する。
    fn to_inner(&self) -> MsfTemplate {
        MsfTemplate {
            start_media_time: self.start_media_time,
            delta_media_time: self.delta_media_time,
            start_group_id: self.start_group_id,
            start_object_id: self.start_object_id,
            delta_group_id: self.delta_group_id,
            delta_object_id: self.delta_object_id,
            start_wallclock: self.start_wallclock,
            delta_wallclock: self.delta_wallclock,
        }
    }
}

#[pymethods]
impl Template {
    /// テンプレートを組み立てる。
    #[new]
    #[pyo3(signature = (
        start_media_time,
        delta_media_time,
        start_group_id,
        start_object_id,
        delta_group_id,
        delta_object_id,
        start_wallclock,
        delta_wallclock,
    ))]
    #[expect(
        clippy::too_many_arguments,
        reason = "draft が定める 8 つの値がすべて必須であり、まとめる単位がない"
    )]
    fn new(
        start_media_time: u64,
        delta_media_time: u64,
        start_group_id: u64,
        start_object_id: u64,
        delta_group_id: u64,
        delta_object_id: u64,
        start_wallclock: u64,
        delta_wallclock: u64,
    ) -> Self {
        Self {
            start_media_time,
            delta_media_time,
            start_group_id,
            start_object_id,
            delta_group_id,
            delta_object_id,
            start_wallclock,
            delta_wallclock,
        }
    }

    /// n 番目 (0 始まり) のエントリを `(pts_ms, group_id, object_id, wallclock_ms)` として返す。
    ///
    /// draft-ietf-moq-msf-01 §7.4.1 の計算式に従う。4 系列のいずれかが overflow する
    /// 場合は `None` を返す。この仕様は draft 由来であり、将来の改訂で変更される
    /// 可能性がある。
    fn resolve_entry(&self, n: u64) -> Option<(u64, u64, u64, u64)> {
        self.to_inner().resolve_entry(n).map(|entry| {
            (
                entry.pts_ms,
                entry.group_id,
                entry.object_id,
                entry.wallclock_ms,
            )
        })
    }

    fn __repr__(&self) -> String {
        format!(
            "Template(start_media_time={}, delta_media_time={}, start_group_id={}, start_object_id={})",
            self.start_media_time, self.delta_media_time, self.start_group_id, self.start_object_id
        )
    }

    fn __eq__(&self, other: &Bound<'_, PyAny>) -> bool {
        match other.cast::<Template>() {
            Ok(other) => self.to_inner() == other.borrow().to_inner(),
            Err(_) => false,
        }
    }
}

/// MSF の認可情報エントリ (draft-ietf-moq-msf-01 §5.2.42 (Authorization Info))。
///
/// [`Track::auth_info`] に設定する。`value` は scheme 固有の JSON 値そのものであり、
/// UTF-8 の生 JSON バイト列として渡す。単独の JSON 値でない場合は encode 時に
/// `ValueError` になる。
///
/// この仕様は draft 由来であり、将来の改訂で変更される可能性がある。
#[pyclass(name = "AuthInfo", get_all, set_all)]
pub(crate) struct AuthInfo {
    /// 認可 scheme 名。
    pub scheme: String,
    /// scheme 固有値の生 JSON。
    pub value: Vec<u8>,
}

impl AuthInfo {
    /// `MsfAuthInfo` へ変換する。
    fn to_inner(&self) -> MsfAuthInfo {
        MsfAuthInfo {
            scheme: self.scheme.clone(),
            value_raw: self.value.clone(),
        }
    }
}

#[pymethods]
impl AuthInfo {
    /// 認可情報エントリを組み立てる。
    #[new]
    fn new(scheme: String, value: Vec<u8>) -> Self {
        Self { scheme, value }
    }

    fn __repr__(&self) -> String {
        format!("AuthInfo(scheme={})", self.scheme)
    }

    fn __eq__(&self, other: &Bound<'_, PyAny>) -> bool {
        match other.cast::<AuthInfo>() {
            Ok(other) => {
                let other = other.borrow();
                self.scheme == other.scheme && self.value == other.value
            }
            Err(_) => false,
        }
    }
}

/// MSF の accessibility 記述子 (draft-ietf-moq-msf-01 §5.2.44 (Accessibility))。
///
/// [`Track::accessibility`] に設定する。
///
/// この仕様は draft 由来であり、将来の改訂で変更される可能性がある。
#[pyclass(name = "Accessibility", get_all, set_all)]
pub(crate) struct Accessibility {
    /// 記述子 scheme。
    pub scheme: String,
    /// 記述子値。
    pub value: String,
}

impl Accessibility {
    /// `MsfAccessibility` へ変換する。
    fn to_inner(&self) -> MsfAccessibility {
        MsfAccessibility {
            scheme: self.scheme.clone(),
            value: self.value.clone(),
        }
    }
}

#[pymethods]
impl Accessibility {
    /// accessibility 記述子を組み立てる。
    #[new]
    fn new(scheme: String, value: String) -> Self {
        Self { scheme, value }
    }

    fn __repr__(&self) -> String {
        format!("Accessibility(scheme={})", self.scheme)
    }

    fn __eq__(&self, other: &Bound<'_, PyAny>) -> bool {
        match other.cast::<Accessibility>() {
            Ok(other) => {
                let other = other.borrow();
                self.scheme == other.scheme && self.value == other.value
            }
            Err(_) => false,
        }
    }
}

/// MSF の初期化データエントリ (draft-ietf-moq-msf-01 §5.1.7 (Initialization Data List))。
///
/// [`Catalog::add_init_data`] で登録する。draft が定める `type` は現状 `inline`
/// (Base64 [RFC 4648] で符号化した初期化データ) だけであり、JSON へは常に `inline`
/// として書き出す。この仕様は draft 由来であり、将来の改訂で変更される可能性がある。
#[pyclass(name = "InitData", get_all, set_all)]
pub(crate) struct InitData {
    /// カタログ内で一意な id。
    pub id: String,
    /// Base64 で符号化した初期化データ。
    pub data: String,
}

impl InitData {
    /// `MsfInitData` へ変換する。
    fn to_inner(&self) -> MsfInitData {
        MsfInitData {
            id: self.id.clone(),
            kind: MsfInitDataKind::Inline,
            data: self.data.clone(),
        }
    }
}

#[pymethods]
impl InitData {
    /// 初期化データを組み立てる。
    #[new]
    fn new(id: String, data: String) -> Self {
        Self { id, data }
    }

    fn __repr__(&self) -> String {
        format!("InitData(id={})", self.id)
    }

    fn __eq__(&self, other: &Bound<'_, PyAny>) -> bool {
        match other.cast::<InitData>() {
            Ok(other) => {
                let other = other.borrow();
                self.id == other.id && self.data == other.data
            }
            Err(_) => false,
        }
    }
}

/// カタログから削除するトラックの参照
/// (draft-ietf-moq-msf-01 §5.1.6 (Delta update) の remove 操作)。
///
/// draft はトラック名と任意のネームスペースだけを持つ参照を定める。ネームスペースを
/// 省略した場合はカタログトラックのネームスペースを継承したものとして解決される
/// (§5.2.2 (Track namespace))。
///
/// この仕様は draft 由来であり、将来の改訂で変更される可能性がある。
#[pyclass(name = "RemoveTrack", get_all, set_all)]
pub(crate) struct RemoveTrack {
    /// 削除するトラック名。
    pub name: String,
    /// 削除するトラックのネームスペース。
    pub namespace: Option<String>,
}

impl RemoveTrack {
    /// `MsfRemoveTrack` へ変換する。
    fn to_inner(&self) -> MsfRemoveTrack {
        MsfRemoveTrack {
            name: self.name.clone(),
            namespace: self.namespace.clone(),
        }
    }
}

#[pymethods]
impl RemoveTrack {
    /// 削除するトラックの参照を組み立てる。
    #[new]
    #[pyo3(signature = (name, namespace = None))]
    fn new(name: String, namespace: Option<String>) -> Self {
        Self { name, namespace }
    }

    fn __repr__(&self) -> String {
        format!(
            "RemoveTrack(name={}, namespace={})",
            self.name,
            format_optional(self.namespace.as_deref())
        )
    }

    fn __eq__(&self, other: &Bound<'_, PyAny>) -> bool {
        match other.cast::<RemoveTrack>() {
            Ok(other) => {
                let other = other.borrow();
                self.name == other.name && self.namespace == other.namespace
            }
            Err(_) => false,
        }
    }
}

/// MSF のトラックオブジェクト (draft-ietf-moq-msf-01 §5.2 (Track Object Fields))。
///
/// カタログの `tracks` / `publishTracks` と、delta 更新の add 操作が運ぶトラック 1 件で
/// ある。JSON のフィールド名を snake_case にした属性を持つ。
///
/// `packaging` は draft §5.2.4 (Packaging) が定める `loc` / `mediatimeline` /
/// `eventtimeline` / `moqlog` / `moqmetrics` のいずれかである。それ以外の値を
/// [`Catalog::add_track`] や [`DeltaUpdate::add_tracks`] へ渡すと `ValueError` になる。
/// draft の MUST 違反 (eventType と packaging の組み合わせ、targetLatency と buffers の
/// 共存、mediatimeline / eventtimeline の depends と mimeType など) は encode 時に
/// `ValueError` になる。
///
/// この仕様は draft 由来であり、将来の改訂で変更される可能性がある。
#[pyclass(name = "Track", get_all, set_all)]
pub(crate) struct Track {
    /// トラック名 (draft-ietf-moq-msf-01 §5.2.3 (Track name))。必須。
    pub name: String,
    /// トラックネームスペース (draft-ietf-moq-msf-01 §5.2.2 (Track namespace))。
    pub namespace: Option<String>,
    /// パッケージングタイプ (draft-ietf-moq-msf-01 §5.2.4 (Packaging))。必須。
    pub packaging: String,
    /// イベントタイムラインタイプ (draft-ietf-moq-msf-01 §5.2.5 (Event timeline type))。
    pub event_type: Option<String>,
    /// トラックロール (draft-ietf-moq-msf-01 §5.2.6 (Track role))。
    pub role: Option<String>,
    /// ライブフラグ (draft-ietf-moq-msf-01 §5.2.7 (Is Live))。必須。
    pub is_live: bool,
    /// ターゲットレイテンシ (ms) (draft-ietf-moq-msf-01 §5.2.8 (Target latency))。
    ///
    /// `buffers` と同時には指定できない。
    pub target_latency: Option<u64>,
    /// ターゲットバッファ (draft-ietf-moq-msf-01 §5.2.9 (Buffers))。
    ///
    /// `target_latency` と同時には指定できない。
    pub buffers: Option<Py<Buffers>>,
    /// トラックラベル (draft-ietf-moq-msf-01 §5.2.10 (Track label))。
    pub label: Option<String>,
    /// レンダーグループ (draft-ietf-moq-msf-01 §5.2.11 (Render group))。
    pub render_group: Option<u64>,
    /// オルタネートグループ (draft-ietf-moq-msf-01 §5.2.12 (Alternate group))。
    pub alt_group: Option<u64>,
    /// 初期化データ参照 (draft-ietf-moq-msf-01 §5.2.13 (Initialization reference))。
    pub init_ref: Option<String>,
    /// 依存トラック名 (draft-ietf-moq-msf-01 §5.2.14 (Dependencies))。
    pub depends: Vec<String>,
    /// メディアタイムラインテンプレート (draft-ietf-moq-msf-01 §5.2.15 (Template))。
    pub template: Option<Py<Template>>,
    /// テンポラル ID (draft-ietf-moq-msf-01 §5.2.16 (Temporal ID))。
    pub temporal_id: Option<u64>,
    /// スペーシャル ID (draft-ietf-moq-msf-01 §5.2.17 (Spatial ID))。
    pub spatial_id: Option<u64>,
    /// コーデック (draft-ietf-moq-msf-01 §5.2.18 (Codec))。
    pub codec: Option<String>,
    /// MIME タイプ (draft-ietf-moq-msf-01 §5.2.19 (Mimetype))。
    pub mime_type: Option<String>,
    /// フレームレート (fps) (draft-ietf-moq-msf-01 §5.2.20 (Framerate))。
    pub framerate: Option<f64>,
    /// タイムスケール (draft-ietf-moq-msf-01 §5.2.21 (Timescale))。
    pub timescale: Option<u64>,
    /// 最大ビットレート (bps) (draft-ietf-moq-msf-01 §5.2.22 (Maximum Bitrate))。
    pub bitrate: Option<u64>,
    /// 平均ビットレート (bps) (draft-ietf-moq-msf-01 §5.2.23 (Average Bitrate))。
    pub avg_bitrate: Option<u64>,
    /// 最大 GOP 長 (ms) (draft-ietf-moq-msf-01 §5.2.24 (Maximum GOP Duration))。
    pub max_gop_duration: Option<u64>,
    /// 最大 Group 長 (ms) (draft-ietf-moq-msf-01 §5.2.25 (Maximum Group Duration))。
    pub max_group_duration: Option<u64>,
    /// エンコード幅 (px) (draft-ietf-moq-msf-01 §5.2.26 (Width))。
    pub width: Option<u64>,
    /// エンコード高さ (px) (draft-ietf-moq-msf-01 §5.2.27 (Height))。
    pub height: Option<u64>,
    /// オーディオサンプルレート (Hz) (draft-ietf-moq-msf-01 §5.2.28 (Audio sample rate))。
    pub samplerate: Option<u64>,
    /// チャンネル設定 (draft-ietf-moq-msf-01 §5.2.29 (Channel configuration))。
    pub channel_config: Option<String>,
    /// 表示幅 (px) (draft-ietf-moq-msf-01 §5.2.30 (Display width))。
    pub display_width: Option<u64>,
    /// 表示高さ (px) (draft-ietf-moq-msf-01 §5.2.31 (Display height))。
    pub display_height: Option<u64>,
    /// 言語タグ (draft-ietf-moq-msf-01 §5.2.32 (Language))。
    pub lang: Option<String>,
    /// トラック長 (ms) (draft-ietf-moq-msf-01 §5.2.35 (Track duration))。
    ///
    /// `is_live` が真の場合は指定できない。
    pub track_duration: Option<u64>,
    /// 接続先 URI (draft-ietf-moq-msf-01 §5.2.36 (Connection URI))。
    pub connection_uri: Option<String>,
    /// 認証トークン (draft-ietf-moq-msf-01 §5.2.37 (Token))。
    pub token: Option<String>,
    /// 暗号化方式 (draft-ietf-moq-msf-01 §5.2.38 (Encryption Scheme))。
    pub encryption_scheme: Option<String>,
    /// 暗号スイート (draft-ietf-moq-msf-01 §5.2.39 (Cipher Suite))。
    pub cipher_suite: Option<String>,
    /// 鍵識別子 (draft-ietf-moq-msf-01 §5.2.40 (Key ID))。
    pub key_id: Option<String>,
    /// track 基本鍵 (draft-ietf-moq-msf-01 §5.2.41 (Track Base Key))。
    pub track_base_key: Option<String>,
    /// 認可情報 (draft-ietf-moq-msf-01 §5.2.42 (Authorization Info))。
    pub auth_info: Option<Vec<Py<AuthInfo>>>,
    /// accessibility 記述子 (draft-ietf-moq-msf-01 §5.2.44 (Accessibility))。
    pub accessibility: Vec<Py<Accessibility>>,
}

impl Track {
    /// 必須フィールドだけを指定した既定値を作る。
    fn with_defaults(name: String, packaging: String, is_live: bool) -> Self {
        Self {
            name,
            packaging,
            is_live,
            namespace: None,
            event_type: None,
            role: None,
            target_latency: None,
            buffers: None,
            label: None,
            render_group: None,
            alt_group: None,
            init_ref: None,
            depends: Vec::new(),
            template: None,
            temporal_id: None,
            spatial_id: None,
            codec: None,
            mime_type: None,
            framerate: None,
            timescale: None,
            bitrate: None,
            avg_bitrate: None,
            max_gop_duration: None,
            max_group_duration: None,
            width: None,
            height: None,
            samplerate: None,
            channel_config: None,
            display_width: None,
            display_height: None,
            lang: None,
            track_duration: None,
            connection_uri: None,
            token: None,
            encryption_scheme: None,
            cipher_suite: None,
            key_id: None,
            track_base_key: None,
            auth_info: None,
            accessibility: Vec::new(),
        }
    }

    /// `MsfTrack` へ変換する。
    ///
    /// `parentName` は clone 操作の中だけで意味を持つ
    /// (draft-ietf-moq-msf-01 §5.2.33 (Parent name)) ため、カタログのトラックと
    /// add 操作のトラックでは常に省略する。
    fn to_inner(&self, py: Python<'_>) -> PyResult<MsfTrack> {
        Ok(MsfTrack {
            name: self.name.clone(),
            namespace: self.namespace.clone(),
            packaging: packaging_from_string(&self.packaging)?,
            event_type: self.event_type.clone(),
            role: self.role.clone(),
            is_live: self.is_live,
            target_latency: self.target_latency,
            buffers: self
                .buffers
                .as_ref()
                .map(|buffers| buffers.borrow(py).to_inner()),
            label: self.label.clone(),
            render_group: self.render_group,
            alt_group: self.alt_group,
            init_ref: self.init_ref.clone(),
            depends: self.depends.clone(),
            template: self
                .template
                .as_ref()
                .map(|value| value.borrow(py).to_inner()),
            temporal_id: self.temporal_id,
            spatial_id: self.spatial_id,
            codec: self.codec.clone(),
            mime_type: self.mime_type.clone(),
            framerate: self.framerate,
            timescale: self.timescale,
            bitrate: self.bitrate,
            avg_bitrate: self.avg_bitrate,
            max_gop_duration: self.max_gop_duration,
            max_group_duration: self.max_group_duration,
            width: self.width,
            height: self.height,
            samplerate: self.samplerate,
            channel_config: self.channel_config.clone(),
            display_width: self.display_width,
            display_height: self.display_height,
            lang: self.lang.clone(),
            parent_name: None,
            track_duration: self.track_duration,
            connection_uri: self.connection_uri.clone(),
            token: self.token.clone(),
            encryption_scheme: self.encryption_scheme.clone(),
            cipher_suite: self.cipher_suite.clone(),
            key_id: self.key_id.clone(),
            track_base_key: self.track_base_key.clone(),
            auth_info: self.auth_info.as_ref().map(|infos| {
                infos
                    .iter()
                    .map(|info| info.borrow(py).to_inner())
                    .collect()
            }),
            accessibility: self
                .accessibility
                .iter()
                .map(|descriptor| descriptor.borrow(py).to_inner())
                .collect(),
        })
    }
}

#[pymethods]
impl Track {
    /// トラックを組み立てる。
    ///
    /// `name` / `packaging` / `is_live` だけが必須であり、残りは属性で設定する。
    #[new]
    fn new(name: String, packaging: String, is_live: bool) -> Self {
        Self::with_defaults(name, packaging, is_live)
    }

    fn __repr__(&self) -> String {
        format!(
            "Track(name={}, packaging={}, is_live={})",
            self.name, self.packaging, self.is_live
        )
    }

    fn __eq__(&self, other: &Bound<'_, PyAny>) -> bool {
        match other.cast::<Track>() {
            Ok(other) => {
                let py = other.py();
                let other = other.borrow();
                match (self.to_inner(py), other.to_inner(py)) {
                    (Ok(left), Ok(right)) => left == right,
                    _ => false,
                }
            }
            Err(_) => false,
        }
    }
}

/// MSF の delta 更新が複製するトラック定義
/// (draft-ietf-moq-msf-01 §5.1.6 (Delta update) の clone 操作)。
///
/// 親トラックの属性を継承し、再定義した属性だけを上書きする。指定しなかった属性は
/// 親から継承されるため、[`Track`] と違ってすべての属性が省略可能である。
///
/// この仕様は draft 由来であり、将来の改訂で変更される可能性がある。
#[pyclass(name = "CloneTrack", get_all, set_all)]
pub(crate) struct CloneTrack {
    /// 新しいトラック名 (draft-ietf-moq-msf-01 §5.2.3 (Track name))。必須。
    pub name: String,
    /// 親トラック名 (draft-ietf-moq-msf-01 §5.2.33 (Parent name))。必須。
    pub parent_name: String,
    /// 親トラックネームスペース (draft-ietf-moq-msf-01 §5.2.34 (Parent namespace))。
    ///
    /// 省略した場合はカタログのネームスペースを継承したものとして解決される。
    pub parent_namespace: Option<String>,
    /// パッケージングタイプ (draft-ietf-moq-msf-01 §5.2.4 (Packaging))。省略時は親から継承する。
    pub packaging: Option<String>,
    /// トラックネームスペース (draft-ietf-moq-msf-01 §5.2.2 (Track namespace))。
    pub namespace: Option<String>,
    /// イベントタイムラインタイプ (draft-ietf-moq-msf-01 §5.2.5 (Event timeline type))。
    pub event_type: Option<String>,
    /// トラックロール (draft-ietf-moq-msf-01 §5.2.6 (Track role))。
    pub role: Option<String>,
    /// ライブフラグ (draft-ietf-moq-msf-01 §5.2.7 (Is Live))。省略時は親から継承する。
    pub is_live: Option<bool>,
    /// ターゲットレイテンシ (ms) (draft-ietf-moq-msf-01 §5.2.8 (Target latency))。
    pub target_latency: Option<u64>,
    /// ターゲットバッファ (draft-ietf-moq-msf-01 §5.2.9 (Buffers))。
    pub buffers: Option<Py<Buffers>>,
    /// トラックラベル (draft-ietf-moq-msf-01 §5.2.10 (Track label))。
    pub label: Option<String>,
    /// レンダーグループ (draft-ietf-moq-msf-01 §5.2.11 (Render group))。
    pub render_group: Option<u64>,
    /// オルタネートグループ (draft-ietf-moq-msf-01 §5.2.12 (Alternate group))。
    pub alt_group: Option<u64>,
    /// 初期化データ参照 (draft-ietf-moq-msf-01 §5.2.13 (Initialization reference))。
    pub init_ref: Option<String>,
    /// 依存トラック名 (draft-ietf-moq-msf-01 §5.2.14 (Dependencies))。省略時は親から継承する。
    pub depends: Option<Vec<String>>,
    /// メディアタイムラインテンプレート (draft-ietf-moq-msf-01 §5.2.15 (Template))。
    pub template: Option<Py<Template>>,
    /// テンポラル ID (draft-ietf-moq-msf-01 §5.2.16 (Temporal ID))。
    pub temporal_id: Option<u64>,
    /// スペーシャル ID (draft-ietf-moq-msf-01 §5.2.17 (Spatial ID))。
    pub spatial_id: Option<u64>,
    /// コーデック (draft-ietf-moq-msf-01 §5.2.18 (Codec))。
    pub codec: Option<String>,
    /// MIME タイプ (draft-ietf-moq-msf-01 §5.2.19 (Mimetype))。
    pub mime_type: Option<String>,
    /// フレームレート (fps) (draft-ietf-moq-msf-01 §5.2.20 (Framerate))。
    pub framerate: Option<f64>,
    /// タイムスケール (draft-ietf-moq-msf-01 §5.2.21 (Timescale))。
    pub timescale: Option<u64>,
    /// 最大ビットレート (bps) (draft-ietf-moq-msf-01 §5.2.22 (Maximum Bitrate))。
    pub bitrate: Option<u64>,
    /// 平均ビットレート (bps) (draft-ietf-moq-msf-01 §5.2.23 (Average Bitrate))。
    pub avg_bitrate: Option<u64>,
    /// 最大 GOP 長 (ms) (draft-ietf-moq-msf-01 §5.2.24 (Maximum GOP Duration))。
    pub max_gop_duration: Option<u64>,
    /// 最大 Group 長 (ms) (draft-ietf-moq-msf-01 §5.2.25 (Maximum Group Duration))。
    pub max_group_duration: Option<u64>,
    /// エンコード幅 (px) (draft-ietf-moq-msf-01 §5.2.26 (Width))。
    pub width: Option<u64>,
    /// エンコード高さ (px) (draft-ietf-moq-msf-01 §5.2.27 (Height))。
    pub height: Option<u64>,
    /// オーディオサンプルレート (Hz) (draft-ietf-moq-msf-01 §5.2.28 (Audio sample rate))。
    pub samplerate: Option<u64>,
    /// チャンネル設定 (draft-ietf-moq-msf-01 §5.2.29 (Channel configuration))。
    pub channel_config: Option<String>,
    /// 表示幅 (px) (draft-ietf-moq-msf-01 §5.2.30 (Display width))。
    pub display_width: Option<u64>,
    /// 表示高さ (px) (draft-ietf-moq-msf-01 §5.2.31 (Display height))。
    pub display_height: Option<u64>,
    /// 言語タグ (draft-ietf-moq-msf-01 §5.2.32 (Language))。
    pub lang: Option<String>,
    /// トラック長 (ms) (draft-ietf-moq-msf-01 §5.2.35 (Track duration))。
    pub track_duration: Option<u64>,
    /// 接続先 URI (draft-ietf-moq-msf-01 §5.2.36 (Connection URI))。
    pub connection_uri: Option<String>,
    /// 認証トークン (draft-ietf-moq-msf-01 §5.2.37 (Token))。
    pub token: Option<String>,
    /// 暗号化方式 (draft-ietf-moq-msf-01 §5.2.38 (Encryption Scheme))。
    pub encryption_scheme: Option<String>,
    /// 暗号スイート (draft-ietf-moq-msf-01 §5.2.39 (Cipher Suite))。
    pub cipher_suite: Option<String>,
    /// 鍵識別子 (draft-ietf-moq-msf-01 §5.2.40 (Key ID))。
    pub key_id: Option<String>,
    /// track 基本鍵 (draft-ietf-moq-msf-01 §5.2.41 (Track Base Key))。
    pub track_base_key: Option<String>,
    /// 認可情報 (draft-ietf-moq-msf-01 §5.2.42 (Authorization Info))。
    pub auth_info: Option<Vec<Py<AuthInfo>>>,
    /// accessibility 記述子 (draft-ietf-moq-msf-01 §5.2.44 (Accessibility))。
    pub accessibility: Option<Vec<Py<Accessibility>>>,
}

impl CloneTrack {
    /// 必須フィールドだけを指定した既定値を作る。
    fn with_defaults(name: String, parent_name: String) -> Self {
        Self {
            name,
            parent_name,
            parent_namespace: None,
            packaging: None,
            namespace: None,
            event_type: None,
            role: None,
            is_live: None,
            target_latency: None,
            buffers: None,
            label: None,
            render_group: None,
            alt_group: None,
            init_ref: None,
            depends: None,
            template: None,
            temporal_id: None,
            spatial_id: None,
            codec: None,
            mime_type: None,
            framerate: None,
            timescale: None,
            bitrate: None,
            avg_bitrate: None,
            max_gop_duration: None,
            max_group_duration: None,
            width: None,
            height: None,
            samplerate: None,
            channel_config: None,
            display_width: None,
            display_height: None,
            lang: None,
            track_duration: None,
            connection_uri: None,
            token: None,
            encryption_scheme: None,
            cipher_suite: None,
            key_id: None,
            track_base_key: None,
            auth_info: None,
            accessibility: None,
        }
    }

    /// `MsfCloneTrack` へ変換する。
    fn to_inner(&self, py: Python<'_>) -> PyResult<MsfCloneTrack> {
        let packaging = match &self.packaging {
            Some(value) => Some(packaging_from_string(value)?),
            None => None,
        };
        Ok(MsfCloneTrack {
            name: self.name.clone(),
            parent_name: self.parent_name.clone(),
            parent_namespace: self.parent_namespace.clone(),
            packaging,
            namespace: self.namespace.clone(),
            event_type: self.event_type.clone(),
            role: self.role.clone(),
            is_live: self.is_live,
            target_latency: self.target_latency,
            buffers: self
                .buffers
                .as_ref()
                .map(|buffers| buffers.borrow(py).to_inner()),
            label: self.label.clone(),
            render_group: self.render_group,
            alt_group: self.alt_group,
            init_ref: self.init_ref.clone(),
            depends: self.depends.clone(),
            template: self
                .template
                .as_ref()
                .map(|value| value.borrow(py).to_inner()),
            temporal_id: self.temporal_id,
            spatial_id: self.spatial_id,
            codec: self.codec.clone(),
            mime_type: self.mime_type.clone(),
            framerate: self.framerate,
            timescale: self.timescale,
            bitrate: self.bitrate,
            avg_bitrate: self.avg_bitrate,
            max_gop_duration: self.max_gop_duration,
            max_group_duration: self.max_group_duration,
            width: self.width,
            height: self.height,
            samplerate: self.samplerate,
            channel_config: self.channel_config.clone(),
            display_width: self.display_width,
            display_height: self.display_height,
            lang: self.lang.clone(),
            track_duration: self.track_duration,
            connection_uri: self.connection_uri.clone(),
            token: self.token.clone(),
            encryption_scheme: self.encryption_scheme.clone(),
            cipher_suite: self.cipher_suite.clone(),
            key_id: self.key_id.clone(),
            track_base_key: self.track_base_key.clone(),
            auth_info: self.auth_info.as_ref().map(|infos| {
                infos
                    .iter()
                    .map(|info| info.borrow(py).to_inner())
                    .collect()
            }),
            accessibility: self.accessibility.as_ref().map(|descriptors| {
                descriptors
                    .iter()
                    .map(|descriptor| descriptor.borrow(py).to_inner())
                    .collect()
            }),
        })
    }
}

#[pymethods]
impl CloneTrack {
    /// 複製するトラックを組み立てる。
    ///
    /// `name` と `parent_name` だけが必須であり、残りは属性で設定する。設定しなかった
    /// 属性は親トラックから継承される。
    #[new]
    fn new(name: String, parent_name: String) -> Self {
        Self::with_defaults(name, parent_name)
    }

    fn __repr__(&self) -> String {
        format!(
            "CloneTrack(name={}, parent_name={})",
            self.name, self.parent_name
        )
    }

    fn __eq__(&self, other: &Bound<'_, PyAny>) -> bool {
        match other.cast::<CloneTrack>() {
            Ok(other) => {
                let py = other.py();
                let other = other.borrow();
                match (self.to_inner(py), other.to_inner(py)) {
                    (Ok(left), Ok(right)) => left == right,
                    _ => false,
                }
            }
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

/// MSF の media timeline template から n 番目のエントリを計算する
/// (draft-ietf-moq-msf-01 §7.4.1)。
///
/// `template` は `Catalog.tracks` の `template` 配列である。値が負の整数または
/// 配列でない場合は `ValueError` になる。計算が overflow する場合は `None` を返す。
/// 型付きで組み立てた [`Template`] からは [`Template::resolve_entry`] で同じ計算ができる。
#[pyfunction]
pub(crate) fn resolve_timeline_template(
    template: &Bound<'_, PyList>,
    n: u64,
) -> PyResult<Option<(u64, u64, u64, u64)>> {
    let template = msf_template_from_python(template)?;
    Ok(template.resolve_entry(n).map(|entry| {
        (
            entry.pts_ms,
            entry.group_id,
            entry.object_id,
            entry.wallclock_ms,
        )
    }))
}

/// Python の配列から `MsfTemplate` を構築する。
///
/// 配列は `[start_media_time, delta_media_time, [start_group_id, start_object_id],
/// [delta_group_id, delta_object_id], start_wallclock, delta_wallclock]` である
/// (draft-ietf-moq-msf-01 §7.4.1)。
fn msf_template_from_python(items: &Bound<'_, PyList>) -> PyResult<MsfTemplate> {
    if items.len() != 6 {
        return Err(PyValueError::new_err(format!(
            "media timeline template requires 6 elements, got {}",
            items.len()
        )));
    }
    // 入れ子の配列は `[Group ID, Object ID]` である。タプルへの extract は
    // list を受け付けないため、要素ごとに取り出す
    let start_group_id = items.get_item(2)?.get_item(0)?.extract()?;
    let start_object_id = items.get_item(2)?.get_item(1)?.extract()?;
    let delta_group_id = items.get_item(3)?.get_item(0)?.extract()?;
    let delta_object_id = items.get_item(3)?.get_item(1)?.extract()?;
    Ok(MsfTemplate {
        start_media_time: items.get_item(0)?.extract()?,
        delta_media_time: items.get_item(1)?.extract()?,
        start_group_id,
        start_object_id,
        delta_group_id,
        delta_object_id,
        start_wallclock: items.get_item(4)?.extract()?,
        delta_wallclock: items.get_item(5)?.extract()?,
    })
}

/// `parse_msf_fragment` の返り値。namespace、Track 名、fragment パラメータである。
type ParsedMsfFragment = (Py<PyAny>, Py<PyBytes>, Vec<(String, String)>);

/// MSF fragment (`msf:` prefix 付き) を namespace と Track 名とパラメータへ分解する
/// (draft-ietf-moq-msf-01 §11.1 (URL construction and interpretation))。
///
/// `Uri.parse` を通さない入力を扱う。返り値は
/// `(namespace, track_name, [(名前, 値), ...])` である。
#[pyfunction]
pub(crate) fn parse_msf_fragment(py: Python<'_>, fragment: &str) -> PyResult<ParsedMsfFragment> {
    let parsed = uri::parse_msf_fragment(fragment).map_err(codec_error)?;
    let namespace = track_namespace_to_python(py, &parsed.namespace)?;
    let parameters = parsed
        .parameters
        .into_iter()
        .map(|parameter| (parameter.name, parameter.value))
        .collect();
    Ok((
        namespace.into_any(),
        PyBytes::new(py, &parsed.track_name).unbind(),
        parameters,
    ))
}

/// MSF の Track 識別子 (`namespace--track` 形式) を namespace と Track 名へ分解する
/// (draft-ietf-moq-transport-21 §8.8 (Representing Namespace and Track Names))。
#[pyfunction]
pub(crate) fn parse_name(py: Python<'_>, text: &str) -> PyResult<(Py<PyAny>, Py<PyBytes>)> {
    let (namespace, track_name) = name::parse_name(text)
        .map_err(|error| PyValueError::new_err(format!("invalid Track name: {error:?}")))?;
    let namespace = track_namespace_to_python(py, &namespace)?;
    Ok((namespace.into_any(), PyBytes::new(py, &track_name).unbind()))
}

/// namespace と Track 名を MSF の Track 識別子 (`namespace--track` 形式) へ変換する。
#[pyfunction]
pub(crate) fn serialize_name(namespace: Vec<Vec<u8>>, track_name: &[u8]) -> PyResult<String> {
    let namespace = crate::core::track_namespace_from_python(namespace)?;
    Ok(name::serialize_name(&namespace, track_name))
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
