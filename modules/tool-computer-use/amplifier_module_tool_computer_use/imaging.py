"""Screenshot scaling shared by every backend.

Every `Backend.capture()` returns PNG bytes at native resolution; downscaling to
MODEL space (and, for zoom, shrinking an oversized crop) is backend-agnostic logic
that belongs here, not duplicated inside each backend.
"""

from __future__ import annotations

import base64
import io
from typing import TYPE_CHECKING

from .geometry import Display, compute_display

if TYPE_CHECKING:
    from .backend import Backend


def capture_scaled_b64(
    backend: Backend,
    disp: Display,
    region: tuple[int, int, int, int] | None,
    max_edge: int,
    max_pixels: int,
) -> str:
    """Capture (optionally a SCREEN-space region) and return base64 PNG in model scale.

    Full-desktop captures are resized to exactly `disp.model_width x disp.model_height`.
    Region captures (zoom) are only shrunk if the crop itself exceeds the model budget,
    so a small zoom region is not upscaled.

    C1 fast path: if `backend` carries a `capture_scaled` capability, use it
    instead of `capture()` + a local PIL resize. `RemoteBackend` is the only
    backend that does - a faithful `capture()` there would drag the FULL
    native-resolution PNG (1.3-10.5 MB, measured - see
    docs/designs/remote-transport.md \u00a73.2/\u00a74) across the wire only to
    throw away ~90%+ of the pixels locally. Detected via the class-descriptor
    idiom (`getattr(type(backend), ...)`, never `hasattr(backend, ...)`) -
    the exact same idiom `hook-computer-use` already uses for
    `native_tool_spec` after the D3 fix, for the same reason: this must never
    risk invoking a property/descriptor that could raise before the intended
    try/except around it even starts. Local backends do not implement this
    capability and should not: locally, `capture()` costs a memory copy, so
    pushing the resize down would be duplication for zero gain - the
    capability exists because there is a wire to protect.
    """
    if getattr(type(backend), "capture_scaled", None) is not None:
        return backend.capture_scaled(  # type: ignore[attr-defined]
            region, (disp.model_width, disp.model_height), max_edge, max_pixels
        )

    from PIL import Image  # imported lazily so import errors surface as tool errors

    png = backend.capture(region=region)
    with Image.open(io.BytesIO(png)) as img:
        img = img.convert("RGB")
        if region:
            tw, th = compute_display(img.width, img.height, max_edge, max_pixels)
            if (tw, th) != (img.width, img.height):
                img = img.resize((tw, th), Image.Resampling.LANCZOS)
        else:
            img = img.resize(
                (disp.model_width, disp.model_height), Image.Resampling.LANCZOS
            )
        buf = io.BytesIO()
        img.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode()
