"""README/DOCS описывают текущее состояние и не привязаны к номеру релиза."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RELEASE_VERSION = re.compile(r"(?<!\d)\d+\.\d+\.\d+(?!\d)")
DOCS_WITHOUT_RELEASE_HISTORY = (
    ROOT / "README.md",
    ROOT / "energy_ats" / "README.md",
    ROOT / "energy_ats" / "DOCS.md",
)


def test_readme_and_user_docs_do_not_contain_release_versions() -> None:
    """Актуальные README/DOCS не должны превращаться в историю конкретных релизов."""
    offenders: list[str] = []
    for path in DOCS_WITHOUT_RELEASE_HISTORY:
        text = path.read_text(encoding="utf-8")
        versions = sorted(set(RELEASE_VERSION.findall(text)))
        if versions:
            offenders.append(f"{path.relative_to(ROOT)}: {', '.join(versions)}")

    assert not offenders, (
        "Номера релизов допустимы в CHANGELOG/metadata, но не в README/DOCS:\n"
        + "\n".join(offenders)
    )
