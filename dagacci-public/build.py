"""Render dagacci-public/index.html from snapshot.json and template.html."""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent


def build() -> Path:
    snap = json.loads((HERE / "snapshot.json").read_text())
    # Escape "</" so product text can't close the <script> tag.
    data = json.dumps(snap).replace("</", "<\\/")
    html = (HERE / "template.html").read_text().replace("/*SNAPSHOT*/null", data)
    out = HERE / "index.html"
    out.write_text(html)
    return out


if __name__ == "__main__":
    print(f"Dashboard written to {build()}")
