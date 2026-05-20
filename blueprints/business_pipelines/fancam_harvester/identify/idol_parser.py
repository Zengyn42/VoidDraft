"""
Parse LLM idol-identification output into a structured IdentityRecord.

The LLM node (identify) is asked to output a JSON block like:
    {
      "group": "TWICE",
      "idol": "Tzuyu",         // null if multiple idols in frame
      "song": "TT",
      "performance_date": "20261015",   // YYYYMMDD or null
      "confidence": 0.87,
      "notes": "..."
    }

This module extracts and validates that JSON from the raw LLM response.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_INLINE_JSON = re.compile(r"\{[^{}]*\"group\"[^{}]*\}", re.DOTALL)


@dataclass
class IdentityRecord:
    clip_id: str                        # matches DownloadRecord.clip_id
    group: Optional[str] = None
    idol: Optional[str] = None          # None → multi-idol frame
    song: Optional[str] = None
    performance_date: Optional[str] = None   # YYYYMMDD
    confidence: float = 0.0
    notes: str = ""
    raw_llm: str = ""

    @property
    def is_identified(self) -> bool:
        return (
            self.group is not None
            and self.performance_date is not None
            and self.confidence >= 0.0
        )

    def storage_path_parts(self) -> list[str]:
        """
        Returns path components for the library directory.
        e.g. ["twice", "20261015", "tzuyu"]  or ["twice", "20261015", "group"]
        """
        parts = [
            (self.group or "unknown").lower().replace(" ", "_"),
            self.performance_date or "unknown_date",
            (self.idol or "group").lower().replace(" ", "_"),
        ]
        return parts


def parse_llm_response(clip_id: str, raw_text: str) -> IdentityRecord:
    """
    Extract IdentityRecord from raw LLM response text.

    Tries:
      1. ```json ... ``` code block
      2. First inline {...} containing "group"
      3. Returns low-confidence placeholder on failure
    """
    record = IdentityRecord(clip_id=clip_id, raw_llm=raw_text)

    # Try code block first
    m = _JSON_BLOCK.search(raw_text)
    json_str = m.group(1) if m else None

    # Fallback to inline JSON
    if not json_str:
        m2 = _INLINE_JSON.search(raw_text)
        json_str = m2.group(0) if m2 else None

    if not json_str:
        logger.warning(f"[{clip_id}] No JSON found in LLM response")
        record.notes = "LLM response contained no parseable JSON"
        return record

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.warning(f"[{clip_id}] JSON decode error: {e}")
        record.notes = f"JSON decode error: {e}"
        return record

    record.group = data.get("group") or None
    record.idol = data.get("idol") or None
    record.song = data.get("song") or None
    record.performance_date = _normalise_date(data.get("performance_date"))
    record.confidence = float(data.get("confidence", 0.0))
    record.notes = data.get("notes", "")

    return record


def _normalise_date(raw: Optional[str]) -> Optional[str]:
    """Normalise various date strings to YYYYMMDD."""
    if not raw:
        return None
    # Already YYYYMMDD
    if re.fullmatch(r"\d{8}", str(raw)):
        return str(raw)
    # YYYY-MM-DD
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(raw))
    if m:
        return m.group(1) + m.group(2) + m.group(3)
    # YYYY.MM.DD
    m = re.match(r"(\d{4})\.(\d{2})\.(\d{2})", str(raw))
    if m:
        return m.group(1) + m.group(2) + m.group(3)
    logger.debug(f"Unrecognised date format: {raw!r}")
    return None
