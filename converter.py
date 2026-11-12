"""Convierte el logo de un canal en un custom emoji (100x100 WEBP, forma redonda)."""

from __future__ import annotations

import sys
from io import BytesIO

from PIL import Image, ImageDraw, ImageOps

OUTPUT_SIZE = 100
# La máscara se dibuja a 4x y se reduce, así el borde del círculo queda suave.
SUPERSAMPLE = 4
WEBP_QUALITY = 92


class InvalidImage(ValueError):
    """El archivo recibido no es una imagen que podamos procesar."""


def _center_square(img: Image.Image) -> Image.Image:
    """Recorta al cuadrado más grande posible desde el centro (sin deformar)."""
    width, height = img.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    return img.crop((left, top, left + side, top + side))


SHAPES = ("circle", "square", "rounded")


def _shape_mask(size: int, shape: str = "circle") -> Image.Image:
    """Máscara de la forma pedida, dibujada a 4x para que el borde sea suave."""
    big = size * SUPERSAMPLE
    mask = Image.new("L", (big, big), 0)
    draw = ImageDraw.Draw(mask)
    box = (0, 0, big - 1, big - 1)
    if shape == "square":
        draw.rectangle(box, fill=255)
    elif shape == "rounded":
        draw.rounded_rectangle(box, radius=int(big * 0.22), fill=255)
    else:  # circle
        # -1 para que el círculo no quede cortado en el borde derecho/inferior.
        draw.ellipse(box, fill=255)
    return mask.resize((size, size), Image.LANCZOS)


def shape_image(
    source: bytes, size: int = OUTPUT_SIZE, shape: str = "circle"
) -> Image.Image:
    """Abre la imagen, la recorta en cuadrado y le aplica la forma pedida."""
    try:
        img = Image.open(BytesIO(source))
        img.load()
    except Exception as exc:  # noqa: BLE001 - cualquier formato no soportado cae aquí
        raise InvalidImage("No pude abrir la imagen.") from exc

    if shape not in SHAPES:
        shape = "circle"
    img = ImageOps.exif_transpose(img).convert("RGBA")
    img = _center_square(img).resize((size, size), Image.LANCZOS)
    # putalpha reemplaza la transparencia: la imagen queda recortada con la forma.
    img.putalpha(_shape_mask(size, shape))
    return img


def round_image(source: bytes, size: int = OUTPUT_SIZE) -> Image.Image:
    """Abre la imagen, la recorta en cuadrado y le aplica la máscara circular."""
    return shape_image(source, size, "circle")


def round_emoji_bytes(source: bytes) -> bytes:
    """Devuelve el logo ya recortado en círculo y escalado a 100x100 WEBP."""
    img = round_image(source, OUTPUT_SIZE)

    out = BytesIO()
    img.save(out, format="WEBP", quality=WEBP_QUALITY, method=6)
    data = out.getvalue()
    if len(data) > 512 * 1024:
        # 100x100 nunca debería llegar aquí, pero Telegram rechaza >512 KB.
        out = BytesIO()
        img.save(out, format="WEBP", quality=70, method=6)
        data = out.getvalue()
    return data


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("uso: python converter.py <logo.png|jpg> <salida.webp>")
    with open(sys.argv[1], "rb") as fh:
        webp = round_emoji_bytes(fh.read())
    with open(sys.argv[2], "wb") as fh:
        fh.write(webp)
    print(f"OK: {sys.argv[2]} ({len(webp)} bytes)")
