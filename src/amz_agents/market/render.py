"""Render the market dashboard: analysis JSON dropped into template.html."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

TEMPLATE = Path(__file__).with_name("template.html")


def render(analysis: dict[str, Any]) -> str:
    # Escape "</" so scraped text can't close the <script> tag.
    data = json.dumps(analysis, separators=(",", ":")).replace("</", "<\\/")
    return TEMPLATE.read_text().replace("/*ANALYSIS*/null", data)


def write(analysis: dict[str, Any], out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(analysis))
    return out
