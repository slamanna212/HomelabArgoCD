#!/usr/bin/env python3
"""Hide the issue-origin chip in a task row below 640px.

The chip repeats the issue URL next to every title, and on a phone it takes about
half the row width. Row layout ("one line" / "detailed") cannot remove it — the
chip is rendered independently of the layout, which is why both settings look the
same. This is the one-line source fix: the same chip, wrapped in a span that is
hidden under Tailwind's `sm` breakpoint. The task page keeps the link.

Runs against a clean upstream checkout (see .github/workflows/opentodo-image.yml).
It fails loudly when the anchor line moves, so an upstream refactor breaks the
build instead of silently shipping an unpatched image.

Usage: python3 hide-origin-chip.py <upstream-checkout>
"""
import pathlib
import sys

TARGET = pathlib.Path("web/src/app/TaskRow.tsx")

OLD = (
    "  const originChip = origin ? "
    "<OriginChip origin={origin} quiet iconOnly={origin.kind === 'import'} /> : null\n"
)

NEW = (
    "  // Local patch: on a phone this chip shares the row with the title and takes\n"
    "  // about half the width. Hidden below 640px; the task page still links out.\n"
    "  const originChip = origin\n"
    '    ? <span className="hidden sm:inline-flex">'
    "<OriginChip origin={origin} quiet iconOnly={origin.kind === 'import'} /></span>\n"
    "    : null\n"
)


def main(root: str) -> int:
    path = pathlib.Path(root) / TARGET
    if not path.is_file():
        print(f"missing {path}", file=sys.stderr)
        return 1
    text = path.read_text()
    if NEW in text:
        print(f"{path} is already patched")
        return 0
    if OLD not in text:
        print(
            f"anchor line not found in {path}: upstream changed the task row. "
            "Update patches/opentodo/hide-origin-chip.py rather than shipping unpatched.",
            file=sys.stderr,
        )
        return 1
    path.write_text(text.replace(OLD, NEW, 1))
    print(f"patched {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
