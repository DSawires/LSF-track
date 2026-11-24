"""Real pictures for tests. The server decodes every picture it stores, so a
PNG signature followed by zeros is no longer a picture to it."""

from __future__ import annotations

import io


def picture(fmt: str = "PNG", size: tuple[int, int] = (64, 48), color=(200, 40, 40)) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, fmt)
    return out.getvalue()


def decoded(data: bytes):
    """(format, size, colour at the centre) of stored bytes."""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        rgb = image.convert("RGB")
        return image.format, image.size, rgb.getpixel((rgb.width // 2, rgb.height // 2))


def close(a, b, tolerance: int = 12) -> bool:
    """Lossy WebP shifts a solid colour by a few levels."""
    return all(abs(x - y) <= tolerance for x, y in zip(a, b))
