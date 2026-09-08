from typing import Any


def score_value(result: dict[str, Any]) -> float:
    """Read score from both the legacy and external-embedding result contracts."""
    for key in ("overall_score", "overallScore", "score", "similarity"):
        value = result.get(key)
        if isinstance(value, (int, float)):
            return max(0.0, min(1.0, float(value)))
    combined_results = result.get("combined_results")
    if isinstance(combined_results, list) and combined_results:
        first = combined_results[0]
        value = first.get("combined_score") if isinstance(first, dict) else None
        if isinstance(value, (int, float)):
            return max(0.0, min(1.0, float(value)))
    return 0.0
