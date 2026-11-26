from __future__ import annotations

import io

import numpy as np
from PIL import Image, ImageOps, ImageStat, UnidentifiedImageError

ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP", "BMP", "TIFF", "GIF", "MPO"}
MIN_WIDTH = 1000


class ImageError(ValueError):
    pass


def load(data: bytes, max_pixels: int) -> Image.Image:
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        image = Image.open(io.BytesIO(data))
        if image.format not in ALLOWED_FORMATS:
            raise ImageError(f"unsupported image format {image.format}")
        if image.width * image.height > max_pixels:
            raise ImageError("image has too many pixels")
        image.load()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError) as error:
        raise ImageError(f"not a readable image ({type(error).__name__})") from error
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA", "P"):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def prepare(image: Image.Image) -> Image.Image:
    if image.width >= MIN_WIDTH:
        return image
    scale = MIN_WIDTH / image.width
    return image.resize((MIN_WIDTH, round(image.height * scale)), Image.Resampling.LANCZOS)


def enhanced(image: Image.Image) -> Image.Image:
    gray = ImageOps.grayscale(image)
    if ImageStat.Stat(gray).mean[0] < 110:
        gray = ImageOps.invert(gray)
    gray = ImageOps.autocontrast(gray, cutoff=1)
    width = max(int(gray.width * 1.5), MIN_WIDTH)
    height = round(gray.height * width / gray.width)
    return gray.resize((width, height), Image.Resampling.LANCZOS).convert("RGB")


def to_bgr(image: Image.Image) -> np.ndarray:
    return np.ascontiguousarray(np.asarray(image)[:, :, ::-1])


def to_png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
