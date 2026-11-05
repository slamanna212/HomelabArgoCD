#!/usr/bin/env python3
"""Hide the issue-origin chip in a task row below 640px.

The chip repeats the issue URL next to every title, and on a phone it takes about
half the row width. Row layout ("one line" / "detailed") cannot remove it — the
chip is rendered independently of that setting, which is why both look the same.
This is the one-line source fix: the same chip, wrapped in a span that is hidden
under Tailwind's `sm` breakpoint. The task page keeps the link.

Runs against a clean upstream checkout (see .github/workflows/opentodo-image.yml).
It fails loudly when no anchor matches, so an upstream refactor breaks the build
rather than silently shipping an unpatched image.

Usage: python3 hide-origin-chip.py <upstream-checkout>
"""
import pathlib
import sys

TARGET = pathlib.Path("web/src/app/TaskRow.tsx")

# Each entry is (anchor, replacement). Anchors are matched literally.
VARIANTS = [
    # v0.22.0 and the 0.22.x line
    (
        "  const originChip = origin ? <OriginChip origin={origin} quiet /> : null\n",
        "  // Local patch: on a phone this chip shares the row with the title and takes\n"
        "  // about half the width. Hidden below 640px; the task page still links out.\n"
        '  const originChip = origin ? <span className="hidden sm:inline-flex">'
        "<OriginChip origin={origin} quiet /></span> : null\n",
    ),
    # main after the row-title-first-on-phones work
    (
        "  const originChip = origin ? "
        "<OriginChip origin={origin} quiet iconOnly={origin.kind === 'import'} /> : null\n",
        "  // Local patch: on a phone this chip shares the row with the title and takes\n"
        "  // about half the width. Hidden below 640px; the task page still links out.\n"
        "  const originChip = origin\n"
        '    ? <span className="hidden sm:inline-flex">'
        "<OriginChip origin={origin} quiet iconOnly={origin.kind === 'import'} /></span>\n"
        "    : null\n",
    ),
]


def main(root: str) -> int:
    path = pathlib.Path(root) / TARGET
    if not path.is_file():
        print(f"missing {path}", file=sys.stderr)
        return 1
    text = path.read_text()

    for _, replacement in VARIANTS:
        if replacement in text:
            print(f"{path} is already patched")
            return 0

    for anchor, replacement in VARIANTS:
        if anchor in text:
            path.write_text(text.replace(anchor, replacement, 1))
            print(f"patched {path}")
            return 0

    print(
        f"no anchor matched in {path}: upstream changed the task row. "
        "Add the new line to patches/opentodo/hide-origin-chip.py instead of "
        "shipping an unpatched image.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
