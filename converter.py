"""Convierte el logo de un canal en un custom emoji (100x100 WEBP, forma redonda)."""

from __future__ import annotations

import sys
from io import BytesIO

from PIL import Image, ImageChops, ImageDraw, ImageOps

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


def remove_background(img: Image.Image, tolerance: int = 36) -> Image.Image:
    """Quita un fondo uniforme desde los bordes, haciendo transparentes los
    píxeles conectados al borde cuyo color se parezca al del fondo."""
    rgba = img.convert("RGBA")
    w, h = rgba.size
    px = rgba.load()
    # Color de fondo estimado con los píxeles de las esquinas y bordes.
    samples = []
    for x in range(w):
        samples.append(px[x, 0][:3])
        samples.append(px[x, h - 1][:3])
    for y in range(h):
        samples.append(px[0, y][:3])
        samples.append(px[w - 1, y][:3])
    # Mediana aproximada por canal para resistir ruido.
    def med(vals):
        s = sorted(vals)
        return s[len(s) // 2]
    bg = (med([c[0] for c in samples]), med([c[1] for c in samples]), med([c[2] for c in samples]))

    tol2 = tolerance * tolerance * 3
    visited = bytearray(w * h)
    from collections import deque
    q = deque()

    def similar(x, y):
        r, g, b, a = px[x, y]
        if a == 0:
            return True
        dr = r - bg[0]; dg = g - bg[1]; db = b - bg[2]
        return (dr * dr + dg * dg + db * db) <= tol2

    # Semillas: todo el borde que se parezca al fondo.
    for x in range(w):
        for y in (0, h - 1):
            i = y * w + x
            if not visited[i] and similar(x, y):
                visited[i] = 1; q.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            i = y * w + x
            if not visited[i] and similar(x, y):
                visited[i] = 1; q.append((x, y))

    # Relleno desde el borde hacia dentro.
    while q:
        x, y = q.popleft()
        px[x, y] = (px[x, y][0], px[x, y][1], px[x, y][2], 0)
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < w and 0 <= ny < h:
                i = ny * w + nx
                if not visited[i] and similar(nx, ny):
                    visited[i] = 1
                    q.append((nx, ny))
    return rgba


def shape_image(
    source: bytes,
    size: int = OUTPUT_SIZE,
    shape: str = "circle",
    remove_bg: bool = False,
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
    if remove_bg:
        img = remove_background(img)
    # putalpha reemplaza la transparencia: la imagen queda recortada con la forma.
    alpha = img.getchannel("A")
    mask = _shape_mask(size, shape)
    img.putalpha(ImageChops.multiply(alpha, mask))
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
