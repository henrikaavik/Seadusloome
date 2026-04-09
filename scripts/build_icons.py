"""Build a self-hosted SVG sprite from the icons referenced in the codebase.

Scans ``app/`` recursively for ``Icon("name")`` and ``icon="name"`` calls,
downloads each matching Lucide SVG from ``unpkg.com/lucide-static``, extracts
its viewBox + inner markup, and writes a single ``<svg>`` sprite with one
``<symbol>`` per icon.

Usage::

    uv run python scripts/build_icons.py

Run manually by developers whenever a new icon is introduced. The resulting
sprite is checked into the repo so runtime never makes network calls.
"""

from __future__ import annotations

import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "app"
SPRITE_PATH = APP_DIR / "static" / "icons" / "sprite.svg"

LUCIDE_URL = "https://unpkg.com/lucide-static@latest/icons/{name}.svg"

# Match Icon("name") or Icon('name') or Icon(name="name") / icon="name"
ICON_POSITIONAL = re.compile(r"""Icon\(\s*["']([a-z0-9-]+)["']""")
ICON_KWARG = re.compile(r"""\bicon\s*=\s*["']([a-z0-9-]+)["']""")


def scan_used_icons(root: Path) -> set[str]:
    names: set[str] = set()
    for py_file in root.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        names.update(ICON_POSITIONAL.findall(text))
        names.update(ICON_KWARG.findall(text))
    return names


def fetch_icon(name: str) -> tuple[str, str]:
    """Return (viewBox, inner_svg_markup) for a given Lucide icon."""
    url = LUCIDE_URL.format(name=name)
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
        raw = resp.read().decode("utf-8")

    # Strip default namespace to simplify element handling.
    raw_no_ns = re.sub(r'\sxmlns="[^"]+"', "", raw, count=1)
    root = ET.fromstring(raw_no_ns)
    view_box = root.attrib.get("viewBox", "0 0 24 24")
    inner = "".join(ET.tostring(child, encoding="unicode") for child in root)
    return view_box, inner


def build_sprite(icons: dict[str, tuple[str, str]]) -> str:
    lines = [
        '<svg xmlns="http://www.w3.org/2000/svg" style="display:none">',
        "  <defs>",
    ]
    for name in sorted(icons):
        view_box, inner = icons[name]
        lines.append(f'    <symbol id="{name}" viewBox="{view_box}">{inner}</symbol>')
    lines.append("  </defs>")
    lines.append("</svg>")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    used = scan_used_icons(APP_DIR)
    if not used:
        print("No Icon() usages found — sprite unchanged.", file=sys.stderr)
        return 0

    fetched: dict[str, tuple[str, str]] = {}
    failures: list[str] = []
    for name in sorted(used):
        try:
            fetched[name] = fetch_icon(name)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}: {exc}")

    if failures:
        print("Failed to fetch icons:", file=sys.stderr)
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        return 1

    SPRITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SPRITE_PATH.write_text(build_sprite(fetched), encoding="utf-8")
    names = ", ".join(sorted(fetched))
    print(f"Built sprite with {len(fetched)} icons: {names}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
