//! `moq` のネイティブ拡張。
//!
//! `moqt-rs` の codec と sans I/O セッション状態機械を PyO3 経由で公開する。
//! Python 側の公開 API は `moq.moqt` / `moq.loc` / `moq.msf` の 3 モジュールであり、
//! この拡張モジュール (`moq._native`) はその実体である。
//!
//! # 役割分担
//!
//! - Python 側: WebTransport のストリーム操作、イベントループ、公開 API の組み立て
//! - Rust 側: MoQT / LOC / MSF の codec と、MoQT のプロトコル状態機械
//!
//! I/O は Python の webtransport-py が担当する。Rust 側は自側が送るべきバイト列を
//! イベントとして返し、ストリームの実体には触れない。

mod codec;
mod core;
mod errors;
mod loc;
mod msf;

use pyo3::prelude::*;

/// Python から `import moq._native` される拡張モジュール。
#[pymodule(gil_used = false, name = "_native")]
mod _native {
    use pyo3::prelude::*;
    use pyo3::types::PyModule;

    use shiguredo_moqt::error::{
        PUBLISH_DONE_GOING_AWAY, PUBLISH_DONE_INTERNAL_ERROR, PUBLISH_DONE_MALFORMED_TRACK,
        PUBLISH_DONE_TOO_FAR_BEHIND, PUBLISH_DONE_TRACK_ENDED, PUBLISH_DONE_UNAUTHORIZED,
        REQUEST_DOES_NOT_EXIST, REQUEST_GOING_AWAY, REQUEST_INTERNAL_ERROR, REQUEST_INVALID_RANGE,
        REQUEST_MALFORMED_TRACK, REQUEST_NOT_SUPPORTED, REQUEST_PREFIX_OVERLAP, REQUEST_TIMEOUT,
        REQUEST_UNAUTHORIZED, STREAM_CANCELLED, STREAM_DELIVERY_TIMEOUT, STREAM_INTERNAL_ERROR,
        STREAM_MALFORMED_TRACK,
    };
    use shiguredo_moqt::message_parameter::{
        PARAM_AUTHORIZATION_TOKEN, PARAM_EXPIRES, PARAM_FILL_PARAMETERS, PARAM_FILL_TIMEOUT,
        PARAM_FORWARD, PARAM_GROUP_ORDER, PARAM_INCLUDE_PROPERTIES, PARAM_LARGEST_OBJECT,
        PARAM_LOCATION_FILTER, PARAM_NEW_GROUP_REQUEST, PARAM_OBJECT_DELIVERY_TIMEOUT,
        PARAM_OBJECT_PROPERTY_FILTER, PARAM_OBJECTID_FILTER, PARAM_PRIORITY_FILTER,
        PARAM_RENDEZVOUS_TIMEOUT, PARAM_SUBGROUP_DELIVERY_TIMEOUT, PARAM_SUBGROUP_FILTER,
        PARAM_SUBSCRIBER_PRIORITY, PARAM_TRACK_NAMESPACE_PREFIX, PARAM_TRACK_PROPERTY_FILTER,
    };
    use shiguredo_moqt::session::types::PUBLISHER_PRIORITY_DEFAULT;
    use shiguredo_moqt::stream::{
        FETCH_HEADER_TYPE, OBJECT_STATUS_END_OF_GROUP, OBJECT_STATUS_END_OF_TRACK,
        PADDING_DATAGRAM_TYPE, PADDING_STREAM_TYPE, SETUP_STREAM_TYPE,
    };

    // MoQT のプロトコル層 (moq.moqt)
    #[pymodule_export]
    use crate::codec::{
        Message, classify_data_stream_type, decode_message, decode_varint, decode_varint_prefix,
        encode_varint, is_padding_datagram, setup_stream_type,
    };
    #[pymodule_export]
    use crate::core::{CoreEvent, CoreSession};

    // LOC の codec (moq.loc)
    #[pymodule_export]
    use crate::loc::LocProperties;

    // MSF の codec (moq.msf)
    #[pymodule_export]
    use crate::msf::{
        Catalog, DeltaUpdate, EventTimeline, MediaTimeline, Uri, parse_fragment_pairs,
        resolve_catalog_variables,
    };

    /// モジュール定数を登録する。
    #[pymodule_init]
    fn init(module: &Bound<'_, PyModule>) -> PyResult<()> {
        // ストリーム種別 (draft-ietf-moq-transport-21 §6.4.1 (Unidirectional Streams) Table 3)
        module.add("SETUP_STREAM_TYPE", SETUP_STREAM_TYPE)?;
        module.add("FETCH_HEADER_TYPE", FETCH_HEADER_TYPE)?;
        module.add("PADDING_STREAM_TYPE", PADDING_STREAM_TYPE)?;
        module.add("PADDING_DATAGRAM_TYPE", PADDING_DATAGRAM_TYPE)?;

        // Message Parameters (draft-ietf-moq-transport-21 §9.20 (Control Message Parameters))
        module.add(
            "PARAM_OBJECT_DELIVERY_TIMEOUT",
            PARAM_OBJECT_DELIVERY_TIMEOUT,
        )?;
        module.add("PARAM_AUTHORIZATION_TOKEN", PARAM_AUTHORIZATION_TOKEN)?;
        module.add("PARAM_RENDEZVOUS_TIMEOUT", PARAM_RENDEZVOUS_TIMEOUT)?;
        module.add(
            "PARAM_SUBGROUP_DELIVERY_TIMEOUT",
            PARAM_SUBGROUP_DELIVERY_TIMEOUT,
        )?;
        module.add("PARAM_EXPIRES", PARAM_EXPIRES)?;
        module.add("PARAM_LARGEST_OBJECT", PARAM_LARGEST_OBJECT)?;
        module.add("PARAM_FILL_TIMEOUT", PARAM_FILL_TIMEOUT)?;
        module.add("PARAM_FORWARD", PARAM_FORWARD)?;
        module.add("PARAM_SUBSCRIBER_PRIORITY", PARAM_SUBSCRIBER_PRIORITY)?;
        module.add("PARAM_LOCATION_FILTER", PARAM_LOCATION_FILTER)?;
        module.add("PARAM_GROUP_ORDER", PARAM_GROUP_ORDER)?;
        module.add("PARAM_FILL_PARAMETERS", PARAM_FILL_PARAMETERS)?;
        module.add("PARAM_SUBGROUP_FILTER", PARAM_SUBGROUP_FILTER)?;
        module.add("PARAM_OBJECTID_FILTER", PARAM_OBJECTID_FILTER)?;
        module.add("PARAM_PRIORITY_FILTER", PARAM_PRIORITY_FILTER)?;
        module.add("PARAM_OBJECT_PROPERTY_FILTER", PARAM_OBJECT_PROPERTY_FILTER)?;
        module.add("PARAM_TRACK_PROPERTY_FILTER", PARAM_TRACK_PROPERTY_FILTER)?;
        module.add("PARAM_NEW_GROUP_REQUEST", PARAM_NEW_GROUP_REQUEST)?;
        module.add("PARAM_TRACK_NAMESPACE_PREFIX", PARAM_TRACK_NAMESPACE_PREFIX)?;
        module.add("PARAM_INCLUDE_PROPERTIES", PARAM_INCLUDE_PROPERTIES)?;

        // REQUEST_ERROR のコード (draft-ietf-moq-transport-21 §16.11.2)
        module.add("REQUEST_INTERNAL_ERROR", REQUEST_INTERNAL_ERROR)?;
        module.add("REQUEST_UNAUTHORIZED", REQUEST_UNAUTHORIZED)?;
        module.add("REQUEST_TIMEOUT", REQUEST_TIMEOUT)?;
        module.add("REQUEST_NOT_SUPPORTED", REQUEST_NOT_SUPPORTED)?;
        module.add("REQUEST_GOING_AWAY", REQUEST_GOING_AWAY)?;
        module.add("REQUEST_DOES_NOT_EXIST", REQUEST_DOES_NOT_EXIST)?;
        module.add("REQUEST_INVALID_RANGE", REQUEST_INVALID_RANGE)?;
        module.add("REQUEST_MALFORMED_TRACK", REQUEST_MALFORMED_TRACK)?;
        module.add("REQUEST_PREFIX_OVERLAP", REQUEST_PREFIX_OVERLAP)?;

        // PUBLISH_DONE のコード (draft-ietf-moq-transport-21 §16.11.3)
        module.add("PUBLISH_DONE_INTERNAL_ERROR", PUBLISH_DONE_INTERNAL_ERROR)?;
        module.add("PUBLISH_DONE_UNAUTHORIZED", PUBLISH_DONE_UNAUTHORIZED)?;
        module.add("PUBLISH_DONE_TRACK_ENDED", PUBLISH_DONE_TRACK_ENDED)?;
        module.add("PUBLISH_DONE_GOING_AWAY", PUBLISH_DONE_GOING_AWAY)?;
        module.add("PUBLISH_DONE_TOO_FAR_BEHIND", PUBLISH_DONE_TOO_FAR_BEHIND)?;
        module.add("PUBLISH_DONE_MALFORMED_TRACK", PUBLISH_DONE_MALFORMED_TRACK)?;

        // ストリーム reset のコード (draft-ietf-moq-transport-21 §16.11.4)
        module.add("STREAM_INTERNAL_ERROR", STREAM_INTERNAL_ERROR)?;
        module.add("STREAM_CANCELLED", STREAM_CANCELLED)?;
        module.add("STREAM_DELIVERY_TIMEOUT", STREAM_DELIVERY_TIMEOUT)?;
        module.add("STREAM_MALFORMED_TRACK", STREAM_MALFORMED_TRACK)?;

        // Object Status (draft-ietf-moq-transport-21 §11.1.2 (Object Status))
        module.add("OBJECT_STATUS_END_OF_GROUP", OBJECT_STATUS_END_OF_GROUP)?;
        module.add("OBJECT_STATUS_END_OF_TRACK", OBJECT_STATUS_END_OF_TRACK)?;

        // 既定値
        module.add("PUBLISHER_PRIORITY_DEFAULT", PUBLISHER_PRIORITY_DEFAULT)?;

        // LOC と MSF の定数
        crate::loc::register_constants(module)?;
        crate::msf::register_constants(module)?;
        Ok(())
    }
}
