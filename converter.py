"""Convierte el logo de un canal en un custom emoji (100x100 WEBP, forma redonda)."""

from __future__ import annotations

import sys
from io import BytesIO

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

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


def _fit_subject(img: Image.Image, size: int, padding: float = 0.06) -> Image.Image:
    """Recorta al contenido visible y lo escala para que ocupe el lienzo."""
    bbox = img.getchannel("A").getbbox()
    if not bbox:
        return img
    left, top, right, bottom = bbox
    # Un pequeño margen para que el sujeto no toque el borde.
    pad = max(1, int(max(right - left, bottom - top) * padding))
    left = max(0, left - pad)
    top = max(0, top - pad)
    right = min(img.width, right + pad)
    bottom = min(img.height, bottom + pad)
    crop = img.crop((left, top, right, bottom))
    cw, ch = crop.size
    if cw <= 0 or ch <= 0:
        return img
    scale = min(size / cw, size / ch)
    nw = max(1, int(round(cw * scale)))
    nh = max(1, int(round(ch * scale)))
    resized = crop.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(resized, ((size - nw) // 2, (size - nh) // 2), resized)
    return canvas


def remove_background(img: Image.Image, tolerance: int = 34) -> Image.Image:
    """Quita el fondo desde los bordes, suaviza los bordes y ajusta el sujeto
    para que ocupe el lienzo sin dejar el espacio vacío del fondo recortado."""
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

    def med(vals):
        s = sorted(vals)
        return s[len(s) // 2]

    bg = (
        med([c[0] for c in samples]),
        med([c[1] for c in samples]),
        med([c[2] for c in samples]),
    )

    def dist2(x, y):
        r, g, b, a = px[x, y]
        if a == 0:
            return 0.0
        dr = r - bg[0]
        dg = g - bg[1]
        db = b - bg[2]
        return float(dr * dr + dg * dg + db * db)

    tol2 = tolerance * tolerance * 3
    # Banda amplia para detectar el contorno y limpiarlo sin dejar rastro.
    edge_tol2 = max(tol2, (tolerance + 28) * (tolerance + 28) * 3)

    visited = bytearray(w * h)
    is_bg = bytearray(w * h)
    from collections import deque
    q = deque()

    # Semillas: borde conectado al fondo.
    for x in range(w):
        for y in (0, h - 1):
            i = y * w + x
            if not visited[i] and dist2(x, y) <= tol2:
                visited[i] = 1
                is_bg[i] = 1
                q.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            i = y * w + x
            if not visited[i] and dist2(x, y) <= tol2:
                visited[i] = 1
                is_bg[i] = 1
                q.append((x, y))

    while q:
        x, y = q.popleft()
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < w and 0 <= ny < h:
                i = ny * w + nx
                if not visited[i] and dist2(nx, ny) <= tol2:
                    visited[i] = 1
                    is_bg[i] = 1
                    q.append((nx, ny))

    # Quita el fondo detectado.
    for y in range(h):
        for x in range(w):
            if is_bg[y * w + x]:
                r, g, b, _a = px[x, y]
                px[x, y] = (r, g, b, 0)

    # Suaviza y limpia el borde: alfa gradual + eliminación de halo de fondo.
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a == 0:
                continue
            d2 = (r - bg[0]) ** 2 + (g - bg[1]) ** 2 + (b - bg[2]) ** 2
            # Vecino del fondo recortado -> borde del sujeto.
            near_bg = False
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < w and 0 <= ny < h and is_bg[ny * w + nx]:
                    near_bg = True
                    break
            if d2 <= tol2:
                px[x, y] = (r, g, b, 0)
                continue
            if near_bg or d2 <= edge_tol2:
                # Alfa suave según lo parecido que sea al fondo (sin borde duro).
                t = (d2 - tol2) / max(1.0, float(edge_tol2 - tol2))
                t = max(0.0, min(1.0, t))
                new_a = int(round(a * (t ** 0.7)))
                # Descontamina el color del fondo para evitar halo en el borde.
                if new_a > 0 and new_a < 250:
                    k = max(0.0, 1.0 - t)
                    rr = int(round((r - bg[0] * k) / max(1e-3, 1.0 - k)))
                    gg = int(round((g - bg[1] * k) / max(1e-3, 1.0 - k)))
                    bb = int(round((b - bg[2] * k) / max(1e-3, 1.0 - k)))
                    r = max(0, min(255, rr))
                    g = max(0, min(255, gg))
                    b = max(0, min(255, bb))
                px[x, y] = (r, g, b, new_a)

    # Ajusta el sujeto al lienzo para que el espacio quitado no se desperdicie.
    rgba = _fit_subject(rgba, w)
    # Un pequeño desenfoque del canal alfa limpia dientes de sierra en el borde.
    alpha = rgba.getchannel("A").filter(ImageFilter.GaussianBlur(0.6))
    rgba.putalpha(alpha)
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
