"""Results files, integrity validation and submission."""
from __future__ import annotations

from .results_io import (
    ResultsFormatError,
    parse_results_bytes,
    parse_results_text,
    read_results,
    write_results,
)
from .validate import ValidationReport, submission_id, validate_results

__all__ = [
    "ResultsFormatError",
    "ValidationReport",
    "parse_results_bytes",
    "parse_results_text",
    "read_results",
    "submission_id",
    "validate_results",
    "write_results",
]


def submit_results(*args, **kwargs):
    from .submit import submit_results as _submit

    return _submit(*args, **kwargs)
