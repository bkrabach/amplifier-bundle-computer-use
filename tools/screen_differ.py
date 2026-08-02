"""Screen-state differ: screenshot -> quantify change -> verdict.

Used ad hoc, over and over, across a long investigation session to answer
three questions that all boil down to "did the screen change": *did the
overlay render?*, *did input land?*, *did the click do anything?* Each time
it was hand-rolled slightly differently (see `scripts/verify_linux_x11.py`'s
own `_sampled_diff_count` for one such one-off). This module is the
reusable version.

Two numbers matter, not one:

* **changed pixel count** (and fraction) - the obvious "did anything move"
  signal.
* **distinct colour count**, before and after - because a changed-pixel
  count of zero is not always a failure. A flat, single-colour desktop (a
  solid background, nothing open) can be the *legitimately correct* state
  after an action that was never supposed to draw anything - e.g.
  confirming an overlay did NOT appear when it should not have. Reporting
  only a pixel-change count would make that correct outcome look
  indistinguishable from "the differ is broken and sees nothing." Reporting
  distinct-colour counts alongside it lets a caller (or a human reading the
  summary) tell "nothing changed because nothing happened" apart from
  "nothing changed because the tool failed to see it."

Works from raw PNG bytes (`diff_bytes`) or from files (`diff_files`) - a
capture pipeline that already holds bytes in memory should never have to
round-trip through a temp file just to diff them. **Never prints or returns
base64** - only pixel/colour counts and a verdict string ever leave this
module; a caller with the actual bytes already has them, and a caller that
doesn't need never see them.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

#: Above this pixel count, an exact per-pixel scan is a poor trade against a
#: cheap histogram-based one - see `_changed_pixel_count`. This is a
#: performance knob, not a correctness one: PIL's `.histogram()` is exact,
#: not sampled, regardless of image size.
_HISTOGRAM_METHOD_THRESHOLD = 0


class ScreenDifferError(RuntimeError):
    """The differ could not compare the two images (decode failure, size
    mismatch) - never silently degrades to a partial or guessed result."""


@dataclass(frozen=True)
class DiffResult:
    """Everything a caller needs to judge "did the screen change" without
    re-deriving it: raw counts first, an opinionated `verdict` second.

    `verdict` is deliberately a plain three-way signal, not a boolean -
    `"changed"` / `"unchanged"` are direct readings of `changed_pixels`;
    `"inconclusive"` never happens today (reserved for a future confidence
    threshold) but the type is three-way from day one so callers do not have
    to special-case a fourth outcome landing later as a breaking change.
    """

    width: int
    height: int
    total_pixels: int
    changed_pixels: int
    changed_fraction: float
    distinct_colors_before: int
    distinct_colors_after: int
    verdict: str  # "changed" | "unchanged" | "inconclusive"

    @property
    def flat_before(self) -> bool:
        """True if the BEFORE image was a single solid colour - the "flat
        desktop" case the module docstring calls out: a legitimate reason
        for `changed_pixels == 0` to be the CORRECT result, not a failure."""
        return self.distinct_colors_before <= 1

    @property
    def flat_after(self) -> bool:
        return self.distinct_colors_after <= 1


def _load_rgb(png_bytes: bytes):
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(png_bytes))
        img.load()
    except Exception as exc:  # noqa: BLE001 - any decode failure is a hard error, not a guess
        raise ScreenDifferError(
            f"could not decode image: {type(exc).__name__}: {exc}"
        ) from exc
    return img.convert("RGB")


def _changed_pixel_count(before, after) -> int:
    """Exact count of pixels that differ between two same-size RGB images,
    via PIL's own C-implemented `ImageChops.difference` + `histogram()` -
    no numpy dependency (this repo does not carry one; see
    `modules/tool-computer-use/pyproject.toml`), and no manual per-pixel
    Python loop, which would be the slow path this specifically avoids.
    """
    from PIL import ImageChops

    diff = ImageChops.difference(before, after).convert("L")
    # A pixel is "changed" if ANY channel differs - `ImageChops.difference`
    # on an RGB pair already reflects that per-channel, and converting to
    # grayscale via "L" takes the (weighted) max-like luma, which is 0 only
    # when R, G, and B were all exactly equal. Histogram bin 0 is therefore
    # exactly "no channel differed"; every other bin is "at least one did".
    hist = diff.histogram()
    return sum(hist[1:])


def _distinct_colors(img) -> int:
    """Exact distinct-colour count via PIL's own `getcolors`, with
    `maxcolors` set to the full pixel count so it can never silently return
    `None` (PIL's documented behaviour when the true count exceeds
    `maxcolors`) and turn into a wrong answer instead of a real one."""
    w, h = img.size
    colors = img.getcolors(maxcolors=w * h)
    if colors is None:
        # Should be unreachable given maxcolors == w*h, but fail loud rather
        # than guess if PIL's behavior here ever changes.
        raise ScreenDifferError("getcolors() returned None despite maxcolors=w*h")
    return len(colors)


def diff_images(before_png: bytes, after_png: bytes) -> DiffResult:
    """Compare two PNGs already in memory. See `diff_bytes`/`diff_files` for
    the documented public entry points - this is the shared implementation."""
    before = _load_rgb(before_png)
    after = _load_rgb(after_png)
    if before.size != after.size:
        raise ScreenDifferError(
            f"image size mismatch: before={before.size} after={after.size} - "
            "cannot diff two different resolutions; capture both from the "
            "same source at the same time"
        )
    width, height = before.size
    total_pixels = width * height
    changed = _changed_pixel_count(before, after)
    fraction = changed / total_pixels if total_pixels else 0.0
    return DiffResult(
        width=width,
        height=height,
        total_pixels=total_pixels,
        changed_pixels=changed,
        changed_fraction=fraction,
        distinct_colors_before=_distinct_colors(before),
        distinct_colors_after=_distinct_colors(after),
        verdict="changed" if changed > 0 else "unchanged",
    )


def diff_bytes(before_png: bytes, after_png: bytes) -> DiffResult:
    """Diff two in-memory PNG byte strings. Never touches disk."""
    return diff_images(before_png, after_png)


def diff_files(before_path: str | Path, after_path: str | Path) -> DiffResult:
    """Diff two PNG files on disk."""
    before_bytes = Path(before_path).read_bytes()
    after_bytes = Path(after_path).read_bytes()
    return diff_images(before_bytes, after_bytes)


def format_summary(result: DiffResult) -> str:
    """Human-readable one-block summary - counts and a verdict, never
    base64, safe to print or log directly."""
    lines = [
        f"screen-differ: {result.width}x{result.height} ({result.total_pixels} px)",
        f"  changed pixels:      {result.changed_pixels} / {result.total_pixels} "
        f"({result.changed_fraction * 100:.4f}%)",
        f"  distinct colors:     before={result.distinct_colors_before}  "
        f"after={result.distinct_colors_after}",
        f"  verdict:             {result.verdict}",
    ]
    if result.flat_before and result.flat_after and result.verdict == "unchanged":
        lines.append(
            "  note: both frames are a single solid colour - 'unchanged' is "
            "the correct read here (a flat desktop), not a sign the differ "
            "failed to see something."
        )
    return "\n".join(lines)


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", help="path to the BEFORE screenshot (PNG)")
    parser.add_argument("after", help="path to the AFTER screenshot (PNG)")
    args = parser.parse_args(argv)

    result = diff_files(args.before, args.after)
    print(format_summary(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
