from __future__ import annotations

import base64
from io import BytesIO

from PIL import Image, ImageOps


ALLOWED_INPUT_FORMATS = {"JPEG", "PNG", "WEBP"}


def normalize_report_photo(content: bytes, *, max_dimension: int = 2200, quality: int = 88) -> bytes:
    """Normalize an uploaded inspection photo to a report-friendly JPEG.

    EXIF orientation is applied and very large phone photos are reduced so DOCX
    files stay manageable. The original upload is not needed by the report
    engine after normalization.
    """
    if not content:
        raise ValueError("Photo file is empty")

    try:
        with Image.open(BytesIO(content)) as source:
            if (source.format or "").upper() not in ALLOWED_INPUT_FORMATS:
                raise ValueError("Photo must be JPEG, PNG, or WEBP")

            image = ImageOps.exif_transpose(source)
            if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")

            image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)

            output = BytesIO()
            image.save(output, format="JPEG", quality=quality, optimize=True)
            return output.getvalue()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Could not read photo: {exc}") from exc


def photo_data_url(content: bytes) -> str:
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"
