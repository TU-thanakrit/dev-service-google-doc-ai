from typing import Any

from app.templates import DOCUMENT_TYPES


def summarize_api_response(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {
            "document_type": None,
            "needs_review": None,
            "extraction_keys": [],
            "analysis_keys": [],
            "metadata": {},
        }
    extracted = payload.get("สิ่งที่อ่าน")
    analysis = payload.get("Analysis")
    metadata = payload.get("metadata")
    safe_metadata = {}
    if isinstance(metadata, dict):
        safe_metadata = {
            key: metadata[key]
            for key in ("filename", "mime_type", "page_count", "llm_model")
            if key in metadata
        }
    return {
        "document_type": payload.get("document_type"),
        "needs_review": payload.get("needs_review"),
        "extraction_keys": list(extracted) if isinstance(extracted, dict) else [],
        "analysis_keys": list(analysis) if isinstance(analysis, dict) else [],
        "metadata": safe_metadata,
    }


def verify_api_result(expected_type: str, payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    detected_type = payload.get("document_type")
    if detected_type != expected_type:
        errors.append(
            f"detected type mismatch: expected={expected_type!r}, actual={detected_type!r}"
        )

    template = DOCUMENT_TYPES[expected_type]
    extracted = payload.get("สิ่งที่อ่าน")
    actual_fields = set(extracted) if isinstance(extracted, dict) else set()
    expected_fields = set(template.fields)
    if actual_fields != expected_fields:
        errors.append(
            f"extraction keys mismatch: expected={sorted(expected_fields)!r}, "
            f"actual={sorted(actual_fields)!r}"
        )

    analysis = payload.get("Analysis")
    actual_analysis = set(analysis) if isinstance(analysis, dict) else set()
    expected_analysis = {"Type of Expense"} if template.analysis_source else set()
    if actual_analysis != expected_analysis:
        errors.append(
            f"analysis keys mismatch: expected={sorted(expected_analysis)!r}, "
            f"actual={sorted(actual_analysis)!r}"
        )

    return errors
