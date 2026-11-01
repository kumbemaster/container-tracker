"""Image preparation for BL scanning."""

from io import BytesIO

from PIL import Image, ImageOps


class ImageError(ValueError):
    """The upload could not be opened as an image."""


#: Refuse images with more pixels than this (40 megapixels).
MAX_PIXELS = 40_000_000


def prepare_image(image_bytes, max_side=2000, quality=85):
    """Prepare raw upload bytes for the model API.

    Applies EXIF orientation, converts to RGB, shrinks so the longest
    side is at most max_side pixels, and re-encodes as JPEG. Raises
    ImageError when the file cannot be opened as an image or has more
    than MAX_PIXELS pixels.
    """
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        image = Image.open(BytesIO(image_bytes))
    except Exception as exc:
        raise ImageError(
            'The file could not be opened as an image.'
        ) from exc
    width, height = image.size or (0, 0)
    if width * height > MAX_PIXELS:
        raise ImageError('The image is too large to scan.')
    try:
        image.load()
        image = ImageOps.exif_transpose(image)
        image = image.convert('RGB')
    except Exception as exc:
        raise ImageError(
            'The file could not be opened as an image.'
        ) from exc
    image.thumbnail((max_side, max_side), Image.LANCZOS)
    out = BytesIO()
    image.save(out, format='JPEG', quality=quality)
    return out.getvalue()
