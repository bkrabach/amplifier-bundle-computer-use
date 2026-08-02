"""Unit tests for `tools.screen_differ` - built entirely from in-memory PNGs
via Pillow, no real display, no desktop, no capture pipeline required.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.screen_differ import (
    ScreenDifferError,
    diff_bytes,
    diff_files,
    format_summary,
)


def _png_bytes(size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
    from PIL import Image

    img = Image.new(mode="RGB", size=size, color=color)  # type: ignore[arg-type]
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _png_bytes_two_colors(
    size: tuple[int, int],
    base: tuple[int, int, int],
    patch: tuple[int, int, int],
    patch_box,
) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new(mode="RGB", size=size, color=base)  # type: ignore[arg-type]
    draw = ImageDraw.Draw(img)
    draw.rectangle(patch_box, fill=patch)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_identical_flat_images_report_zero_change_and_one_distinct_color():
    """The exact case called out in the task: a flat, single-colour desktop
    that is legitimately unchanged must not look like a differ failure."""
    before = _png_bytes((100, 80), (10, 10, 10))
    after = _png_bytes((100, 80), (10, 10, 10))

    result = diff_bytes(before, after)

    assert result.changed_pixels == 0
    assert result.changed_fraction == 0.0
    assert result.distinct_colors_before == 1
    assert result.distinct_colors_after == 1
    assert result.verdict == "unchanged"
    assert result.flat_before and result.flat_after


def test_a_real_change_is_detected_with_exact_pixel_and_color_counts():
    before = _png_bytes((100, 80), (10, 10, 10))
    # PIL's `ImageDraw.rectangle` box is inclusive of both endpoints, so
    # (10, 10, 29, 29) covers x/y in [10, 29] - exactly 20 pixels per side.
    after = _png_bytes_two_colors(
        (100, 80), (10, 10, 10), (200, 30, 30), (10, 10, 29, 29)
    )  # a 20x20 patch = 400 px

    result = diff_bytes(before, after)

    assert result.changed_pixels == 400
    assert result.total_pixels == 100 * 80
    assert abs(result.changed_fraction - 400 / (100 * 80)) < 1e-9
    assert result.distinct_colors_before == 1
    assert result.distinct_colors_after == 2
    assert result.verdict == "changed"
    assert not result.flat_after


def test_size_mismatch_fails_loud_not_silently():
    before = _png_bytes((100, 80), (10, 10, 10))
    after = _png_bytes((50, 40), (10, 10, 10))
    try:
        diff_bytes(before, after)
    except ScreenDifferError as exc:
        assert "size mismatch" in str(exc)
    else:
        raise AssertionError("expected ScreenDifferError for mismatched sizes")


def test_garbage_bytes_fail_loud_not_silently():
    try:
        diff_bytes(b"not a png at all", b"also not a png")
    except ScreenDifferError:
        pass
    else:
        raise AssertionError("expected ScreenDifferError for undecodable input")


def test_diff_files_reads_from_disk(tmp_path):
    before_path = tmp_path / "before.png"
    after_path = tmp_path / "after.png"
    before_path.write_bytes(_png_bytes((40, 40), (0, 0, 0)))
    after_path.write_bytes(_png_bytes((40, 40), (255, 255, 255)))

    result = diff_files(before_path, after_path)

    assert result.changed_pixels == 40 * 40
    assert result.verdict == "changed"


def test_format_summary_never_includes_base64_and_notes_flat_case():
    before = _png_bytes((20, 20), (1, 2, 3))
    after = _png_bytes((20, 20), (1, 2, 3))
    result = diff_bytes(before, after)
    summary = format_summary(result)

    assert "base64" not in summary.lower()
    # A summary should be counts and words, not an encoded payload - sanity
    # check it stays a reasonably short, printable report.
    assert len(summary) < 2000
    assert "flat desktop" in summary
