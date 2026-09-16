//! GREASE (Generate Random Extensions And Sustain Extensibility) のヘルパー (`moqt.moqt`)。
//!
//! draft-ietf-moq-transport-21 §13 (Grease) が定める予約値を生成・判定する。
//! GREASE 値は「未知の値を無視できるか」を検証するために、Setup Options /
//! Properties / エラーコードなどのレジストリへ意図的に混ぜる値である。
//! この仕様は draft 由来であり、将来の改訂で計算式が変更される可能性がある。
//!
//! 値の並びは `0x7F * N + 0x9D` (N は非負整数) で、上限は `0x3FFFFFFFFFFFFFDE` である。
//! 値の生成・判定そのものは moqt-rs の `grease` モジュールに委譲する。

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyModule;

use shiguredo_moqt::grease::{
    GREASE_BASE, GREASE_INTERVAL, GREASE_MAX, generate as moqt_generate,
    is_grease as moqt_is_grease,
};

/// GREASE 値の連番 (`0x7F * N + 0x9D` の N) として取り得る最大値。
///
/// `GREASE_MAX` ちょうどが値の並びに乗るため、`(GREASE_MAX - GREASE_BASE) / GREASE_INTERVAL`
/// が最大の N になる。`GREASE_MAX / GREASE_INTERVAL` を使うと、生成値が上限を超えて
/// 生成に失敗する N を乱数源へ渡してしまう。
const GREASE_MAX_SEQUENCE: u64 = (GREASE_MAX - GREASE_BASE) / GREASE_INTERVAL;

/// GREASE 値を生成する。
///
/// `source` は `stop` を 1 つだけ受け取り、`[0, stop)` の整数を返す呼び出し可能
/// オブジェクトである (`random.Random.randrange` と同じ形)。省略した場合は
/// 標準ライブラリの `random.randrange` を使う。テストから決定的な値を渡せるように
/// 引数で受け取る。
#[pyfunction]
#[pyo3(signature = (source = None))]
pub(crate) fn generate(py: Python<'_>, source: Option<&Bound<'_, PyAny>>) -> PyResult<u64> {
    let n = match source {
        Some(source) => draw_sequence(source)?,
        None => {
            // 乱数源を渡されなかった場合だけ標準ライブラリを使う
            let randrange = PyModule::import(py, "random")?.getattr("randrange")?;
            draw_sequence(&randrange)?
        }
    };
    // N は範囲内なので moqt-rs 側の生成は失敗しない
    moqt_generate(n).ok_or_else(|| {
        PyValueError::new_err(format!("random source returned an out-of-range value: {n}"))
    })
}

/// 与えられた値が GREASE 値かどうかを返す。
///
/// 上限 `GREASE_MAX` を超えた値も、値の並びに合致すれば `True` になる。受信側は
/// 将来 draft の上限が広がった場合にも未知値として無視できる必要があるためである。
#[pyfunction]
pub(crate) fn is_grease(value: u64) -> bool {
    moqt_is_grease(value)
}

/// 乱数源から GREASE 値の連番を 1 つ引く。
///
/// `[0, GREASE_MAX_SEQUENCE + 1)` を上限として渡し、戻り値が整数でその範囲に
/// 収まっていることを検査する。範囲外の値を渡された場合は黙って丸めずエラーにする。
fn draw_sequence(source: &Bound<'_, PyAny>) -> PyResult<u64> {
    let drawn = source.call1((GREASE_MAX_SEQUENCE + 1,))?;
    let Some(n) = drawn.extract::<u64>().ok() else {
        return Err(PyValueError::new_err(format!(
            "random source must return an int, got {}",
            drawn.get_type().name()?
        )));
    };
    if n > GREASE_MAX_SEQUENCE {
        return Err(PyValueError::new_err(format!(
            "random source returned {n}, which exceeds {GREASE_MAX_SEQUENCE}"
        )));
    }
    Ok(n)
}

/// GREASE の定数をモジュール定数として登録する。
pub(crate) fn register_constants(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("GREASE_BASE", GREASE_BASE)?;
    module.add("GREASE_INTERVAL", GREASE_INTERVAL)?;
    module.add("GREASE_MAX", GREASE_MAX)?;
    Ok(())
}
