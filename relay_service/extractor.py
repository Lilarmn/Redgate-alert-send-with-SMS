"""Alert extractor and normalizer with full nested dictionary support.

Extracts standard fields (alert_id, alert_name, severity, target, status, message, url, timestamp)
from arbitrary Redgate JSON payloads (e.g. {"Target": {"Name": "SQL01"}}).
Candidate keys can be specified as flat keys or dotted paths (e.g. "target.name", "alert.id").
"""
from typing import Any, Dict, List, Tuple


def _normalize_key(key: str) -> str:
    """Normalize key by lowercasing and stripping non-alphanumeric chars."""
    return "".join(ch for ch in str(key).lower() if ch.isalnum())


def _extract_paths(obj: Any, prefix: str, out: List[Tuple[str, str, Any]]) -> None:
    """Collect (normalized_leaf_key, normalized_full_path, scalar_value) tuples depth-first."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            norm_k = _normalize_key(k)
            curr_path = f"{prefix}.{norm_k}" if prefix else norm_k
            if isinstance(v, (dict, list)):
                _extract_paths(v, curr_path, out)
            elif v is not None and str(v).strip() != "":
                out.append((norm_k, curr_path, v))
    elif isinstance(obj, list):
        for item in obj:
            _extract_paths(item, prefix, out)


def extract_alert_details(payload: Any, field_mappings: Dict[str, List[str]]) -> Dict[str, str]:
    """Extract standard alert fields from payload based on candidate field mappings.
    
    Returns a dict with standard keys:
      alert_id, alert_name, severity, target, status, message, url, timestamp
    """
    details: Dict[str, str] = {
        "alert_id": "",
        "alert_name": "Unknown Alert",
        "severity": "Unknown",
        "target": "Unknown",
        "status": "Raised",
        "message": "",
        "url": "",
        "timestamp": "",
    }

    if isinstance(payload, (dict, list)):
        paths: List[Tuple[str, str, Any]] = []
        _extract_paths(payload, "", paths)

        for field_name, candidate_keys in field_mappings.items():
            found = False
            for cand in candidate_keys:
                # Check dotted path match first if dot present
                if "." in cand:
                    norm_cand_path = ".".join(_normalize_key(p) for p in cand.split("."))
                    match = next((val for _, full_path, val in paths if full_path == norm_cand_path), None)
                    if match is not None:
                        details[field_name] = str(match).strip()
                        found = True
                        break
                else:
                    norm_cand = _normalize_key(cand)
                    # Priority 1: Check if full path ends with this candidate key (or exact leaf match)
                    match = next((val for leaf, full_path, val in paths if leaf == norm_cand), None)
                    if match is not None:
                        details[field_name] = str(match).strip()
                        found = True
                        break
            if found:
                continue

    elif isinstance(payload, str) and payload.strip():
        details["message"] = payload.strip()
        details["alert_name"] = "Text Alert"

    return details
