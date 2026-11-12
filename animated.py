"""Genera custom emojis animados para Telegram (100x100, WebM VP9, ≤3 s).

El texto y los degradados se dibujan con Pillow y se codifican con ffmpeg.
Requisitos de Telegram: https://core.telegram.org/stickers#video-requirements

CLI rápida:
    python animated.py "HOLA" -o emoji.webm --palette neon --bg gradient --border spin
"""

from __future__ import annotations

import logging
import math
import shutil
import subprocess
import tempfile
from contextlib import suppress
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageMath

from converter import round_image, shape_image

logger = logging.getLogger(__name__)

# Telegram exige exactamente 100x100 para custom emojis animados.
SIZE = 100
FPS = 30
DURATION = 2.8  # < 3 s
FRAME_COUNT = round(FPS * DURATION)

# Renderizamos grande y reducimos: así el texto y el borde no salen pixelados.
CANVAS = 400
SUPERSAMPLE = 4

TARGET_BYTES = 64 * 1024  # objetivo recomendado
MAX_BYTES = 256 * 1024  # límite oficial de Telegram
CRF_ATTEMPTS = (30, 36, 42, 48, 54, 60)

PALETTES: dict[str, dict[str, Any]] = {
    "neon": {
        "label": "Neón",
        "colors": ("#00F5D4", "#00BBF9", "#F15BB5"),
    },
    "sunset": {
        "label": "Atardecer",
        "colors": ("#FF6B6B", "#FFD93D", "#FF8E53"),
    },
    "gold": {
        "label": "Oro",
        "colors": ("#F9D423", "#FF4E50", "#F9D423"),
    },
    "ocean": {
        "label": "Océano",
        "colors": ("#00C6FF", "#0072FF", "#00E0B8"),
    },
    "fire": {
        "label": "Fuego",
        "colors": ("#FF512F", "#F09819", "#FF2E63"),
    },
    "ice": {
        "label": "Hielo",
        "colors": ("#8EC5FC", "#E0C3FC", "#7F7FD5"),
    },
    "candy": {
        "label": "Caramelo",
        "colors": ("#FF61D2", "#FE9090", "#FFD84D"),
    },
    "mint": {
        "label": "Menta",
        "colors": ("#2AF598", "#009EFD", "#22D3EE"),
    },
    "royal": {
        "label": "Real",
        "colors": ("#7F5AF0", "#FFD166", "#2CB67D"),
    },
    "grape": {
        "label": "Uva",
        "colors": ("#B14EFF", "#FF4ECD", "#3A86FF"),
    },
    "aurora": {
        "label": "Aurora",
        "colors": ("#00E5FF", "#7CFF6B", "#B388FF"),
    },
    "rose": {
        "label": "Rosa",
        "colors": ("#FF9A9E", "#FAD0C4", "#FF5F8F"),
    },
    "forest": {
        "label": "Bosque",
        "colors": ("#11998E", "#38EF7D", "#0B6E4F"),
    },
    "cyber": {
        "label": "Cyber",
        "colors": ("#00F0FF", "#FF00E0", "#7A00FF"),
    },
}

BACKGROUNDS = {
    "gradient": "Degradado animado",
    "solid": "Color sólido",
    "pulse": "Color que respira",
    "transparent": "Transparente",
}

SHADOWS = {"on": "Con sombra", "off": "Sin sombra"}

BORDERS = {
    "none": "Sin borde",
    "ring": "Anillo",
    "spin": "Anillo giratorio",
    "dashed": "Anillo punteado",
    "glow": "Brillo",
    "double": "Doble anillo",
    "neon": "Borde neón",
    "pulse": "Borde que late",
}

# Color de la fuente y del contorno de las letras. "auto" elige el que mejor
# contrasta con la paleta.
TEXT_COLORS = {
    "auto": "Automático",
    "white": "Blanco",
    "black": "Negro",
    "gold": "Dorado",
    "cyan": "Cian",
    "pink": "Rosa",
    "red": "Rojo",
    "green": "Verde",
    "blue": "Azul",
    "orange": "Naranja",
}

STROKE_COLORS = {
    "auto": "Automático",
    "white": "Blanco",
    "black": "Negro",
    "palette": "Color de la paleta",
    "gold": "Dorado",
    "cyan": "Cian",
    "none": "Sin borde",
}

TEXT_COLOR_VALUES = {
    "white": (255, 255, 255),
    "black": (17, 17, 17),
    "gold": (255, 214, 10),
    "cyan": (0, 245, 212),
    "pink": (241, 91, 181),
    "red": (255, 71, 87),
    "green": (46, 213, 115),
    "blue": (30, 144, 255),
    "orange": (255, 159, 28),
}

# Animación del texto (las letras), independiente de la del fondo.
MOTIONS = {
    "none": "Sin animación",
    "type": "Máquina de escribir",
    "pop": "Pop",
    "bounce": "Rebote",
    "wave": "Ola",
    "fade": "Aparecer",
    "slide": "Deslizar",
    "shake": "Vibración",
    "zoom": "Zoom suave",
    "sway": "Balanceo",
    "jelly": "Gelatina",
    "float": "Flotar",
    "glitch": "Glitch",
    "flicker": "Parpadeo",
    "heartbeat": "Latido doble",
    "drop": "Caída",
    "scroll": "Deslizar infinito",
    "spring": "Resorte",
    "wiggle": "Meneo",
    "levitate": "Levitación",
}

# Efectos que se aplican al logo circular de un canal.
LOGO_EFFECTS = {
    "shine": "Brillo",
    "pulse": "Latido",
    "coin": "Moneda",
    "spin": "Giro 360°",
    "bounce": "Rebote",
    "wobble": "Balanceo",
    "swing": "Péndulo",
    "shake": "Vibración",
    "rainbow": "Arcoíris",
    "glow": "Aura",
    "ring": "Anillo",
    "pop": "Pop",
    "drop": "Gota (onda)",
    "float": "Flotar",
    "flip": "Voltear",
    "jelly": "Gelatina",
    "heartbeat": "Latido doble",
    "orbit": "Órbita",
    "ripple": "Ondas",
    "sparkle": "Destellos",
    "neon": "Neón",
    "spring": "Resorte",
    "wiggle": "Meneo",
    "levitate": "Levitación",
}

# Vueltas completas por loop: entero para que el emoji loopee sin salto.
SPEED_TURNS = {"slow": 1, "normal": 2, "fast": 3}
SPEED_LABELS = {"slow": "Lenta", "normal": "Normal", "fast": "Rápida"}

_FONT_DIRS = (
    Path(__file__).parent / "assets",
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/TTF"),
    Path("/usr/share/fonts/dejavu"),
)

# Fuentes disponibles en la imagen Docker (fonts-dejavu-core). Si falta alguna,
# se usa como respaldo la primera que exista en _FONT_DIRS.
FONT_CHOICES: dict[str, dict[str, Any]] = {
    "bold": {
        "label": "Sans negrita",
        "files": ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf"),
    },
    "sans": {
        "label": "Sans",
        "files": ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
    },
    "serif_bold": {
        "label": "Serif negrita",
        "files": ("DejaVuSerif-Bold.ttf", "DejaVuSerif.ttf"),
    },
    "serif": {
        "label": "Serif",
        "files": ("DejaVuSerif.ttf", "DejaVuSerif-Bold.ttf"),
    },
    "mono_bold": {
        "label": "Mono negrita",
        "files": ("DejaVuSansMono-Bold.ttf", "DejaVuSansMono.ttf"),
    },
    "mono": {
        "label": "Mono",
        "files": ("DejaVuSansMono.ttf", "DejaVuSansMono-Bold.ttf"),
    },
}

FONT_CANDIDATES = tuple(
    directory / name
    for directory in _FONT_DIRS
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf")
)


class EmojiError(RuntimeError):
    """No se pudo generar o codificar el emoji animado."""


# --------------------------------------------------------------------------- #
# Utilidades de color / formas
# --------------------------------------------------------------------------- #
def _rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _cyclic_color(colors: list[tuple[int, int, int]], pos: float) -> tuple[int, int, int]:
    """Color interpolado de una paleta que vuelve al inicio (loop)."""
    pos %= 1.0
    n = len(colors)
    scaled = pos * n
    index = int(scaled) % n
    frac = scaled - int(scaled)
    start = colors[index]
    end = colors[(index + 1) % n]
    return tuple(round(start[i] + (end[i] - start[i]) * frac) for i in range(3))


def _conic(colors: list[tuple[int, int, int]], size: int) -> Image.Image:
    image = Image.new("RGB", (size, size))
    draw = ImageDraw.Draw(image)
    for degrees in range(360):
        draw.pieslice(
            (0, 0, size - 1, size - 1),
            start=degrees,
            end=degrees + 1,
            fill=_cyclic_color(colors, degrees / 360),
        )
    return image


def _ring_mask(size: int, width: int, dashed: bool) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    inset = 4
    outer = (inset, inset, size - 1 - inset, size - 1 - inset)
    if dashed:
        segments = 16
        for index in range(segments):
            start = index * 360 / segments
            draw.arc(
                outer,
                start=start,
                end=start + (360 / segments) * 0.62,
                fill=255,
                width=width,
            )
    else:
        draw.ellipse(outer, outline=255, width=width)
    return mask


def _rounded_mask(width: int, height: int, radius: int) -> Image.Image:
    """Rectángulo con esquinas redondeadas (la forma base de los emojis de texto)."""
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, width - 1, height - 1), radius=max(0, radius), fill=255
    )
    return mask


def _rounded_rect_points(
    width: int, height: int, radius: int, inset: float = 0.0, steps: int = 20
) -> list[tuple[float, float]]:
    """Puntos del contorno de un rectángulo redondeado (para el borde punteado)."""
    x0, y0 = inset, inset
    x1, y1 = width - 1 - inset, height - 1 - inset
    r = max(0.0, min(float(radius), (x1 - x0) / 2, (y1 - y0) / 2))
    points: list[tuple[float, float]] = []

    def add_line(a: tuple[float, float], b: tuple[float, float]) -> None:
        for i in range(steps + 1):
            points.append(
                (
                    a[0] + (b[0] - a[0]) * i / steps,
                    a[1] + (b[1] - a[1]) * i / steps,
                )
            )

    def add_arc(cx: float, cy: float, start: float, end: float) -> None:
        for i in range(steps + 1):
            angle = math.radians(start + (end - start) * i / steps)
            points.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))

    add_line((x0 + r, y0), (x1 - r, y0))
    add_arc(x1 - r, y0 + r, -90, 0)
    add_line((x1, y0 + r), (x1, y1 - r))
    add_arc(x1 - r, y1 - r, 0, 90)
    add_line((x1 - r, y1), (x0 + r, y1))
    add_arc(x0 + r, y1 - r, 90, 180)
    add_line((x0, y1 - r), (x0, y0 + r))
    add_arc(x0 + r, y0 + r, 180, 270)
    return points


def _dashed_polyline(
    draw: ImageDraw.ImageDraw,
    points: list[tuple[float, float]],
    width: int,
    dash: float = 28.0,
    gap: float = 16.0,
) -> None:
    """Dibuja una línea gruesa a trazos siguiendo una lista de puntos."""
    if len(points) < 2:
        return
    period = dash + gap
    travelled = 0.0
    prev = points[0]
    for cur in points[1:]:
        segment = math.dist(prev, cur)
        if segment <= 0:
            continue
        step = 0.0
        while step < segment:
            phase = (travelled + step) % period
            if phase < dash:
                length = min(dash - phase, segment - step)
                start = step / segment
                end = (step + length) / segment
                draw.line(
                    [
                        (
                            prev[0] + (cur[0] - prev[0]) * start,
                            prev[1] + (cur[1] - prev[1]) * start,
                        ),
                        (
                            prev[0] + (cur[0] - prev[0]) * end,
                            prev[1] + (cur[1] - prev[1]) * end,
                        ),
                    ],
                    fill=255,
                    width=width,
                )
                step += length
            else:
                step += min(period - phase, segment - step)
        travelled += segment
        prev = cur


def _rounded_ring_mask(
    width: int,
    height: int,
    radius: int,
    ring_width: int,
    dashed: bool = False,
    inset: int = 4,
) -> Image.Image:
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    x0, y0 = inset, inset
    x1, y1 = width - 1 - inset, height - 1 - inset
    r = max(0, min(radius, (x1 - x0) // 2, (y1 - y0) // 2))
    if dashed:
        points = _rounded_rect_points(
            width, height, r, inset + ring_width / 2
        )
        _dashed_polyline(draw, points, ring_width)
    else:
        draw.rounded_rectangle(
            (x0, y0, x1, y1), radius=r, outline=255, width=ring_width
        )
    return mask


def _shape_ring_mask(
    size: int, shape: str, ring_width: int, dashed: bool = False
) -> Image.Image:
    """Anillo que sigue la forma elegida para el logo (círculo, cuadrado…)."""
    if shape == "square":
        return _rounded_ring_mask(size, size, 0, ring_width, dashed)
    if shape == "rounded":
        return _rounded_ring_mask(
            size, size, int(size * 0.22), ring_width, dashed
        )
    return _ring_mask(size, ring_width, dashed)


def _horizontal_gradient(
    colors: list[tuple[int, int, int]],
    width: int,
    height: int,
    phase: float = 0.0,
) -> Image.Image:
    """Degradado que recorre el lienzo de izquierda a derecha (loop)."""
    strip = Image.new("RGB", (width, 1))
    pixels = strip.load()
    for x in range(width):
        pixels[x, 0] = _cyclic_color(colors, x / width + phase)
    return strip.resize((width, height), Image.BICUBIC)


def _tinted(image: Image.Image, mask: Image.Image) -> Image.Image:
    out = image.convert("RGBA").copy()
    out.putalpha(mask)
    return out


def _luminance(colors: Iterable[tuple[int, int, int]]) -> float:
    values = list(colors)
    red = sum(c[0] for c in values) / len(values)
    green = sum(c[1] for c in values) / len(values)
    blue = sum(c[2] for c in values) / len(values)
    return (0.299 * red + 0.587 * green + 0.114 * blue) / 255


# --------------------------------------------------------------------------- #
# Texto
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=256)
def _font(size: int, family: str = "bold") -> ImageFont.FreeTypeFont:
    choice = FONT_CHOICES.get(family) or FONT_CHOICES["bold"]
    paths = [directory / name for directory in _FONT_DIRS for name in choice["files"]]
    for path in (*paths, *FONT_CANDIDATES):
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    raise EmojiError(
        "No encontré una fuente TTF. Instala fonts-dejavu-core en el sistema."
    )


# Los emojis premium se reciben como entidades de Telegram y se dibujan como
# imagen dentro del texto: se comportan como una letra más y también se pueden
# cortar entre dos emojis.
PREMIUM_PLACEHOLDER = "\ufffc"
PREMIUM_SCALE = 0.95  # alto del emoji premium respecto al tamaño de la fuente
PREMIUM_GAP = 0.05  # aire a cada lado del emoji premium
# Fotogramas que se extraen de cada emoji premium animado. Se reproducen una
# vez a lo largo del emoji generado, así se respeta su animación original.
PREMIUM_FRAMES = 24
PREMIUM_DECODE_SIZE = 200  # se decodifica grande y luego se reduce al dibujar
# Fotogramas de los emojis de ejemplo del menú (0,5 s: Telegram los reproduce
# en bucle y así se generan rápido).
EXAMPLE_FRAMES = 15


def _premium_runs(text: str) -> list[tuple[str, str]]:
    """Divide el texto en tramos de letras y huecos de emoji premium."""
    runs: list[tuple[str, str]] = []
    buffer: list[str] = []
    for char in text:
        if char == PREMIUM_PLACEHOLDER:
            if buffer:
                runs.append(("text", "".join(buffer)))
                buffer.clear()
            runs.append(("emoji", ""))
        else:
            buffer.append(char)
    if buffer:
        runs.append(("text", "".join(buffer)))
    return runs


def _layout_text(
    text: str, font: ImageFont.FreeTypeFont, stroke: int, draw: ImageDraw.ImageDraw
) -> dict[str, Any]:
    """Mide el texto con emojis premium en línea y anota dónde va cada cosa.

    Para avanzar se usa el ancho real de la fuente (incluye espacios finales),
    no el ancho de la tinta: así un espacio antes de un emoji premium no se
    pierde y las separaciones quedan como en el texto original.
    """
    emoji_size = max(1, int(round(font.size * PREMIUM_SCALE)))
    gap = max(1, int(round(font.size * PREMIUM_GAP)))
    items: list[dict[str, Any]] = []
    cursor = 0.0
    text_height = 0
    previous = ""
    for kind, chunk in _premium_runs(text):
        if kind == "text":
            box = draw.textbbox((0, 0), chunk, font=font, stroke_width=stroke)
            width = box[2] - box[0]
            advance = draw.textlength(chunk, font=font)
            height = box[3] - box[1]
            items.append(
                {
                    "kind": "text",
                    "text": chunk,
                    "x": cursor,
                    "box": box,
                    "width": width,
                    "advance": advance,
                    "height": height,
                }
            )
            cursor += advance
            text_height = max(text_height, height)
            previous = "text"
        else:
            # El contorno de la letra sobresale `stroke` por la derecha: se
            # suma para que el emoji no se monte encima del trazo. Después del
            # emoji el siguiente texto ya empieza con su propio contorno.
            x = cursor + gap + (stroke if previous == "text" else 0)
            items.append(
                {
                    "kind": "emoji",
                    "x": x,
                    "width": emoji_size,
                    "height": emoji_size,
                }
            )
            cursor = x + emoji_size + gap
            text_height = max(text_height, emoji_size)
            previous = "emoji"
    return {
        "items": items,
        "width": cursor,
        "text_height": text_height,
        "emoji_size": emoji_size,
    }


# Un texto se puede partir en hasta 10 emojis; los cortes son verticales y
# pueden atravesar una letra, así no quedan huecos entre caracteres.
MAX_WORD_EMOJIS = 10
WORD_MAX_TEXT_HEIGHT = 0.72  # proporción del alto del emoji
WORD_MIN_TEXT_HEIGHT = 0.48  # mínimo legible al elegir el menor número de emojis
WORD_PAD = 0.11  # margen interior para el borde en los extremos
MAX_FONT_SIZE = 380


def _word_plan(
    text: str,
    height: int,
    emoji_count: int = 0,
    font_key: str = "bold",
    max_count: int = MAX_WORD_EMOJIS,
) -> dict[str, Any]:
    """Calcula cuántos emojis necesita la palabra y con qué tamaño se dibuja.

    Se busca el menor número de emojis que mantenga el texto legible y, para
    ese número, la fuente más grande que quepa. El texto se dibuja una sola vez
    en un lienzo ancho y luego se corta en cuadrados exactos; por eso las letras
    quedan pegadas y un corte puede partir una letra por la mitad. Los emojis
    premium ocupan el ancho de una imagen cuadrada dentro del texto.
    """
    text = " ".join(text.split())
    if not text:
        raise EmojiError("El texto no puede estar vacío.")
    if len(text) > 14:
        raise EmojiError("El texto es demasiado largo: usa como máximo 14 caracteres.")

    # El número real de emojis premium manda: cada marcador del texto es uno.
    emoji_count = text.count(PREMIUM_PLACEHOLDER)

    cell = height
    pad = int(cell * WORD_PAD)
    max_text_height = int(cell * WORD_MAX_TEXT_HEIGHT)
    min_text_height = int(cell * WORD_MIN_TEXT_HEIGHT)
    draw = ImageDraw.Draw(Image.new("L", (1, 1)))

    # Medidas del texto completo con todas las fuentes candidatas.
    measurements: list[tuple[int, Any, int, dict[str, Any]]] = []
    for size in range(MAX_FONT_SIZE, 19, -2):
        font = _font(size, font_key)
        stroke = max(5, int(size * 0.11))
        layout = _layout_text(text, font, stroke, draw)
        measurements.append((size, font, stroke, layout))

    # Solo interesan las que no superan el alto máximo del emoji.
    height_fit = [
        item
        for item in measurements
        if item[3]["text_height"] <= max_text_height
    ]
    if not height_fit:
        height_fit = [measurements[-1]]

    chosen: tuple[int, Any] | None = None
    fallback: tuple[int, Any] | None = None
    for count in range(1, max(1, min(max_count, MAX_WORD_EMOJIS)) + 1):
        width_limit = count * cell - 2 * pad
        item = next(
            (
                candidate
                for candidate in height_fit
                if candidate[3]["width"] <= width_limit
            ),
            None,
        )
        if item is None:
            continue
        if fallback is None or item[3]["text_height"] > fallback[1][3]["text_height"]:
            fallback = (count, item)
        if item[3]["text_height"] >= min_text_height:
            chosen = (count, item)
            break

    selected = chosen or fallback
    if selected is None:
        raise EmojiError("No pude ajustar el texto al emoji.")

    # El número de emojis se deduce del ancho real: una palabra corta puede
    # ocupar 1 y una larga 4 o más; el bot no necesita que el usuario lo elija.
    count, (size, font, stroke, layout) = selected
    width = count * cell
    badge_width = min(width, int(round(layout["width"])) + 2 * pad)
    # Centrado: la parte que sobra se reparte a los dos lados para que ni el
    # primer ni el último trozo queden con un hueco visible a un lado.
    margin = max(0, (width - badge_width) // 2)
    return {
        "text": text,
        "count": count,
        "font": font,
        "size": size,
        "stroke": stroke,
        "pad": pad,
        "width": width,
        "badge_width": badge_width,
        "margin": margin,
        "emoji_size": layout["emoji_size"],
        "text_height": layout["text_height"],
        "items": layout["items"],
    }


def _png_bytes(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _png_frames_from_raw(
    raw: bytes, size: int, count: int
) -> list[bytes]:
    """Muestrea `count` fotogramas RGBA de un vídeo ya decodificado."""
    frame_size = size * size * 4
    total = len(raw) // frame_size
    if total <= 0:
        return []
    steps = min(count, total)
    frames: list[bytes] = []
    for index in range(steps):
        number = round(index * (total - 1) / (steps - 1)) if steps > 1 else 0
        start = number * frame_size
        image = Image.frombytes(
            "RGBA", (size, size), raw[start : start + frame_size]
        )
        frames.append(_png_bytes(image))
    return frames


def _tgs_frames(
    data: bytes, count: int = PREMIUM_FRAMES, size: int = PREMIUM_DECODE_SIZE
) -> list[bytes]:
    """Fotogramas de un sticker animado .tgs (Lottie comprimido con gzip)."""
    try:
        from rlottie_python import LottieAnimation
    except ImportError:
        return []

    with tempfile.NamedTemporaryFile(suffix=".tgs", delete=False) as tmp:
        tmp.write(data)
        path = Path(tmp.name)
    animation = None
    try:
        animation = LottieAnimation.from_tgs(str(path))
        total = animation.lottie_animation_get_totalframe()
        if total <= 0:
            return []
        steps = min(count, total)
        frames: list[bytes] = []
        for index in range(steps):
            number = round(index * (total - 1) / (steps - 1)) if steps > 1 else 0
            image = animation.render_pillow_frame(
                number, width=size, height=size
            ).convert("RGBA")
            frames.append(_png_bytes(image))
        return frames
    except Exception:  # noqa: BLE001 - Lottie corrupto o no soportado
        logger.warning("No pude renderizar el .tgs premium", exc_info=True)
        return []
    finally:
        if animation is not None:
            with suppress(Exception):
                animation.lottie_animation_destroy()
        path.unlink(missing_ok=True)


def _video_frames(
    data: bytes, count: int = PREMIUM_FRAMES, size: int = PREMIUM_DECODE_SIZE
) -> list[bytes]:
    """Fotogramas de un sticker de vídeo .webm (forzando el alfa de VP9)."""
    binary = shutil.which("ffmpeg")
    if not binary:
        return []
    command = [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-c:v",
        "libvpx-vp9",
        "-i",
        "pipe:0",
        "-vf",
        f"scale={size}:{size}:flags=lanczos",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgba",
        "-an",
        "-",
    ]
    process = subprocess.run(command, input=data, capture_output=True, check=False)
    if process.returncode != 0 or not process.stdout:
        detail = process.stderr.decode("utf-8", "replace").strip()
        logger.warning("No pude decodificar el .webm premium: %s", detail[-300:])
        return []
    return _png_frames_from_raw(process.stdout, size, count)


def decode_premium_frames(
    data: bytes,
    is_animated: bool = False,
    is_video: bool = False,
    count: int = PREMIUM_FRAMES,
) -> list[bytes]:
    """Fotogramas PNG de un emoji premium: uno si es estático, todos si se anima.

    Los .tgs se renderizan con rlottie y los .webm se decodifican con ffmpeg.
    Si el formato no se puede leer se devuelve una lista vacía y el bot usa la
    miniatura estática como respaldo.
    """
    if is_video:
        return _video_frames(data, count)
    if is_animated:
        return _tgs_frames(data, count)
    return [_png_bytes(Image.open(BytesIO(data)).convert("RGBA"))]


def _has_transparency(image: Image.Image) -> bool:
    return image.getchannel("A").getextrema()[0] < 8


def _cut_flat_background(image: Image.Image) -> Image.Image:
    """Quita un fondo plano opaco de un fotograma de emoji premium.

    Si Telegram entrega la miniatura en JPG, o si la decodificación del WebM
    pierde el canal alfa, el emoji llega opaco con un fondo sólido (negro o
    blanco según cómo lo aplane Telegram). Se detecta ese caso (fotograma
    opaco y las cuatro esquinas del mismo color) y se vuelve transparente esa
    zona conectada con el borde, para que se vea el fondo del emoji generado en
    lugar de un cuadrado opaco.
    """
    if _has_transparency(image):
        return image

    rgb = image.convert("RGB")
    width, height = rgb.size
    corners = ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1))
    samples = [rgb.getpixel(point) for point in corners]
    # Todas las esquinas deben ser del mismo color (negro, blanco…).
    if any(
        max(abs(a - b) for a, b in zip(sample, samples[0])) > 40
        for sample in samples[1:]
    ):
        return image

    # El relleno se hace sobre una copia suavizada: así el ruido del JPG no
    # deja islas opacas y el borde del emoji queda limpio.
    sentinel = (1, 2, 3)
    filled = rgb.filter(ImageFilter.GaussianBlur(1))
    for point in corners:
        if filled.getpixel(point) == sentinel:
            continue
        with suppress(Exception):
            ImageDraw.floodfill(filled, point, sentinel, thresh=110)
    marker = Image.new("RGB", (width, height), sentinel)
    keep = ImageChops.difference(filled, marker).convert("L")
    keep = keep.point(lambda value: 0 if value < 4 else 255)
    # Abre y erosiona la máscara: quita motas sueltas y el ribete oscuro que
    # deja el fondo negro pegado al contorno del emoji.
    keep = keep.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
    keep = keep.filter(ImageFilter.MinFilter(3))
    if keep.getextrema()[1] == 0:
        return image
    out = image.copy()
    out.putalpha(keep)
    return out


def _resize_rgba(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Redimensiona RGBA sin halos negros en los bordes transparentes.

    Pillow no premultiplica el alfa al redimensionar: los píxeles transparentes
    (negros) se mezclan con los visibles y dejan una mancha oscura alrededor del
    emoji. Se premultiplica, se reduce y se vuelve a dividir por el alfa.
    """
    image = image.convert("RGBA")
    if image.size == size:
        return image
    red, green, blue, alpha = image.split()
    premultiplied = Image.merge(
        "RGBA",
        (
            ImageChops.multiply(red, alpha),
            ImageChops.multiply(green, alpha),
            ImageChops.multiply(blue, alpha),
            alpha,
        ),
    ).resize(size, Image.LANCZOS)
    premultiplied.load()
    red, green, blue, alpha = premultiplied.split()
    safe_alpha = alpha.point(lambda value: value or 1)
    # Pillow 12 eliminó ImageMath.eval; unsafe_eval es el reemplazo. Se busca sin
    # tocar `eval` para no fallar en las versiones donde ya no existe.
    evaluate = getattr(ImageMath, "unsafe_eval", None) or getattr(ImageMath, "eval")
    channels = [
        evaluate("convert(c * 255 / a, 'L')", c=channel, a=safe_alpha)
        for channel in (red, green, blue)
    ]
    return Image.merge("RGBA", (*channels, alpha))


def _premium_frames(frames: list[bytes], size: int) -> list[Image.Image]:
    images: list[Image.Image] = []
    for frame in frames:
        try:
            image = Image.open(BytesIO(frame))
            image.load()
        except Exception as exc:  # noqa: BLE001 - formato no soportado
            raise EmojiError(
                "No pude usar uno de los emojis premium. Prueba con otro emoji."
            ) from exc
        image = _cut_flat_background(image.convert("RGBA"))
        images.append(_resize_rgba(image, (size, size)))
    return images


def _draw_word(
    plan: dict[str, Any],
    width: int,
    height: int,
    emoji_images: list[list[Image.Image]],
    shift: int = 0,
) -> dict[str, Any]:
    """Dibuja la palabra completa (letras + emojis premium) una sola vez."""
    font = plan["font"]
    stroke = plan["stroke"]
    origin = plan["margin"] + shift + plan["pad"]
    text_top = (height - plan["text_height"]) / 2
    emoji_top = (height - plan["emoji_size"]) / 2

    inner = Image.new("L", (width, height), 0)
    outer = Image.new("L", (width, height), 0)
    painter = ImageDraw.Draw(inner)
    outline_painter = ImageDraw.Draw(outer)

    placements: list[tuple[int, int, int, list[Image.Image], list[Image.Image]]] = []
    emoji_index = 0
    for item in plan["items"]:
        if item["kind"] == "text":
            box = item["box"]
            x = origin + item["x"] - box[0]
            y = text_top - box[1]
            painter.text((x, y), item["text"], font=font, fill=255)
            outline_painter.text(
                (x, y),
                item["text"],
                font=font,
                fill=255,
                stroke_width=stroke,
                stroke_fill=255,
            )
            continue

        frames = (
            emoji_images[emoji_index]
            if emoji_index < len(emoji_images)
            else None
        )
        emoji_index += 1
        if not frames:
            continue
        image = frames[0]
        x = int(round(origin + item["x"]))
        y = int(round(emoji_top))
        # El emoji NO se pinta con la máscara del texto: su alfa cambia en cada
        # fotograma y dejaría su silueta del primer fotograma marcada en el
        # fondo (se veía como un manchón detrás al girar una moneda).
        # Sombra propia por fotograma: también sigue la animación.
        shadows = [
            frame.getchannel("A")
            .filter(ImageFilter.GaussianBlur(stroke * 0.8))
            .point(lambda value: int(value * 0.55))
            for frame in frames
        ]
        placements.append((x, y, int(stroke * 0.6), frames, shadows))

    outline = ImageChops.subtract(outer, inner)
    # Los emojis premium no llevan contorno duro (se recortaría contra el
    # borde del emoji) y su sombra se calcula fotograma a fotograma.
    shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    shadow.putalpha(outer.filter(ImageFilter.GaussianBlur(stroke * 0.8)))
    shadow = _tinted(shadow, shadow.getchannel("A").point(lambda v: int(v * 0.55)))
    shadow = ImageChops.offset(shadow, 0, int(stroke * 0.6))
    return {
        "mask": inner,
        "outline": outline,
        "shadow": shadow,
        "placements": placements,
        "stroke": stroke,
        "shift": shift,
    }


def _premium_art(
    placements: list[tuple[int, int, int, list[Image.Image], list[Image.Image]]],
    width: int,
    height: int,
    frame_index: int,
    total_frames: int,
    reveal_x: int | None = None,
    with_shadow: bool = True,
) -> tuple[Image.Image, Image.Image]:
    """Capas (sombra, dibujo) de cada emoji premium en el fotograma actual.

    La animación del emoji se reparte a lo largo de todo el emoji generado, de
    modo que se reproduce una vez por bucle y no se queda congelada. La sombra
    se toma de ese mismo fotograma para que acompañe al movimiento.
    """
    art = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    shade = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for x, y, shadow_dy, frames, shadows in placements:
        if not frames:
            continue
        # Con la máquina de escribir, el emoji aparece cuando el cursor llega.
        if reveal_x is not None and x > reveal_x:
            continue
        if len(frames) > 1 and total_frames > 1:
            position = round(
                frame_index * (len(frames) - 1) / (total_frames - 1)
            )
        else:
            position = 0
        position = min(position, len(frames) - 1)
        frame = frames[position]
        shadow_alpha = shadows[position] if position < len(shadows) else None
        if with_shadow and shadow_alpha is not None:
            shadow_frame = Image.new("RGBA", frame.size, (0, 0, 0, 0))
            shadow_frame.putalpha(shadow_alpha)
            shade.alpha_composite(shadow_frame, (x, y + shadow_dy))
        art.alpha_composite(frame, (x, y))
    return shade, art


def _seam_shift(mask: Image.Image, count: int, cell: int, margin: int) -> int:
    """Desplaza el dibujo para que los cortes caigan sobre una letra.

    Así cada corte parte una letra por la mitad en vez de caer en el espacio
    entre dos letras, que es lo que hacía que se vieran separadas.
    """
    if count < 2 or margin <= 0:
        return 0
    best_shift = 0
    best_score = -1
    # De menor a mayor desplazamiento: si dos opciones empatan se usa la más
    # centrada, no un extremo.
    candidates = [0]
    for step in range(2, margin + 1, 2):
        candidates.extend((step, -step))
    for shift in candidates:
        score = 0
        for boundary in range(cell, count * cell, cell):
            column = boundary - shift
            if column <= 0:
                continue
            if mask.crop((column - 1, 0, column + 2, mask.height)).getbbox():
                score += 1
        if score > best_score:
            best_shift, best_score = shift, score
    return best_shift


def _word_text_masks(
    plan: dict[str, Any],
    width: int,
    height: int,
    emoji_images: list[list[bytes]] | None = None,
) -> dict[str, Any]:
    """Máscaras y capa de color de los emojis premium ya colocados."""
    images = [
        _premium_frames(frames, plan["emoji_size"])
        for frames in (emoji_images or [])
    ]
    layer = _draw_word(plan, width, height, images)
    shift = _seam_shift(layer["mask"], plan["count"], height, plan["margin"])
    if shift:
        layer = _draw_word(plan, width, height, images, shift)
    layer["count"] = plan["count"]
    return layer


def word_count(
    text: str, emoji_count: int | None = None, font_key: str = "bold"
) -> int:
    """Número de emojis que usará `generate_word` para este texto."""
    return int(_word_plan(text, CANVAS, emoji_count or 0, font_key)["count"])


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
ROUND_RADIUS = 0.24  # proporción del lado para las esquinas redondeadas
TYPE_FRACTION = 0.55  # parte del loop que tarda la máquina de escribir
TYPE_HOLD_END = 0.80  # desde aquí se borra, antes de repetir el loop


def _blank_like(image: Image.Image) -> Image.Image:
    fill = 0 if image.mode == "L" else (0, 0, 0, 0)
    return Image.new(image.mode, image.size, fill)


def _shift_layer(image: Image.Image, dx: float, dy: float) -> Image.Image:
    if abs(dx) < 0.5 and abs(dy) < 0.5:
        return image
    out = _blank_like(image)
    out.paste(image, (int(round(dx)), int(round(dy))))
    return out


def _scale_layer(image: Image.Image, factor: float) -> Image.Image:
    if abs(factor - 1) < 0.002 or factor <= 0:
        return image
    width = max(1, int(round(image.width * factor)))
    height = max(1, int(round(image.height * factor)))
    resized = image.resize((width, height), Image.LANCZOS)
    out = _blank_like(image)
    out.paste(resized, ((image.width - width) // 2, (image.height - height) // 2))
    return out


def _stretch_layer(
    image: Image.Image, scale_x: float, scale_y: float
) -> Image.Image:
    """Escala distinto en X y en Y sin salirse del lienzo (gelatina)."""
    if abs(scale_x - 1) < 0.002 and abs(scale_y - 1) < 0.002:
        return image
    width = max(1, int(round(image.width * scale_x)))
    height = max(1, int(round(image.height * scale_y)))
    resized = image.resize((width, height), Image.LANCZOS)
    out = _blank_like(image)
    out.paste(resized, ((image.width - width) // 2, (image.height - height) // 2))
    return out


def _rotate_layer(
    image: Image.Image, angle: float, center: tuple[int, int]
) -> Image.Image:
    if abs(angle) < 0.05:
        return image
    return image.rotate(angle, resample=Image.BICUBIC, center=center)


def _ease_out_back(value: float) -> float:
    back = 1.70158
    shift = value - 1
    return 1 + (back + 1) * shift**3 + back * shift**2


def _loop_phase(t: float, enter: float, hold_end: float, exit_end: float = 0.94) -> float:
    """0 → 1 en la entrada, 1 mientras se mantiene y 0 al final del loop.

    Los efectos de una sola pasada empiezan y acaban en el mismo estado, así
    que al repetir el loop no hay salto. Sin esto la palabra se recomponía de
    golpe al final y, como cada emoji se reproduce por su cuenta, parecía que
    cada trozo iba a su ritmo. El último fotograma es el previo al corte, así
    que la salida termina en `exit_end` y no en 1.0: de otro modo el video
    volvía a empezar a mitad de la salida.
    """
    if t <= 0:
        return 0.0
    if t < enter:
        return t / enter
    if t < hold_end:
        return 1.0
    if t >= exit_end:
        return 0.0
    return (exit_end - t) / (exit_end - hold_end)


def _motion_layers(
    mask: Image.Image,
    outline: Image.Image,
    shadow: Image.Image,
    motion: str,
    index: int,
    total_frames: int,
    width: int,
    height: int,
) -> tuple[Image.Image, Image.Image, Image.Image, tuple[int, int, int] | None]:
    """Aplica la animación de las letras y devuelve las capas del fotograma.

    Las capas son máscaras de toda la palabra, así que los efectos mueven o
    escalan el texto completo; el fondo, el borde y los emojis premium no se
    tocan. El cursor de la máquina de escribir se devuelve aparte.
    """
    if motion not in MOTIONS or motion == "none" or total_frames < 2:
        return mask, outline, shadow, None

    t = index / total_frames
    cursor: tuple[int, int, int] | None = None

    if motion == "type":
        bbox = mask.getbbox()
        if not bbox:
            return mask, outline, shadow, None
        left, right = bbox[0], bbox[2]
        span = max(1, right - left)
        # Avance lineal: la palabra se escribe a velocidad constante de punta a
        # punta. Con un ease-out el primer trozo se escribía casi de golpe y el
        # último tardaba mucho más, y al verlo como emojis separados parecía
        # que cada uno iba a su propio ritmo.
        progress = _loop_phase(t, TYPE_FRACTION, TYPE_HOLD_END)
        edge = int(left + span * progress)
        wipe = Image.new("L", (width, height), 0)
        ImageDraw.Draw(wipe).rectangle((0, 0, edge, height), fill=255)
        mask = ImageChops.multiply(mask, wipe)
        outline = ImageChops.multiply(outline, wipe)
        shadow = shadow.copy()
        shadow.putalpha(
            ImageChops.multiply(shadow.getchannel("A"), wipe)
        )
        if progress < 1:
            cursor = (edge, bbox[1], bbox[3])
    elif motion == "pop":
        progress = _loop_phase(t, 0.40, 0.78)
        factor = max(0.25, _ease_out_back(progress))
        mask = _scale_layer(mask, factor)
        outline = _scale_layer(outline, factor)
        shadow = _scale_layer(shadow, factor)
    elif motion == "bounce":
        dy = -abs(math.sin(2 * math.pi * t)) * height * 0.11
        mask = _shift_layer(mask, 0, dy)
        outline = _shift_layer(outline, 0, dy)
        shadow = _shift_layer(shadow, 0, dy)
    elif motion == "wave":
        bbox = mask.getbbox() or (0, 0, width, height)
        center = ((bbox[0] + bbox[2]) // 2, (bbox[1] + bbox[3]) // 2)
        angle = 5.5 * math.sin(2 * math.pi * t)
        mask = _rotate_layer(mask, angle, center)
        outline = _rotate_layer(outline, angle, center)
        shadow = _rotate_layer(shadow, angle, center)
    elif motion == "fade":
        progress = _loop_phase(t, 0.25, 0.76)
        factor = 1 - (1 - progress) ** 2
        # Escalar el alfa de cada capa sin tocar su forma.
        def fade(image: Image.Image) -> Image.Image:
            if image.mode == "L":
                return image.point(lambda value: int(value * factor))
            out = image.copy()
            out.putalpha(
                out.getchannel("A").point(lambda value: int(value * factor))
            )
            return out
        mask = fade(mask)
        outline = fade(outline)
        shadow = fade(shadow)
    elif motion == "slide":
        # Entra por la izquierda, se queda y vuelve a salir por la izquierda:
        # empieza y acaba fuera en el mismo lado, así el loop no da salto.
        progress = _loop_phase(t, 0.32, 0.72)
        dx = -width * 0.40 * (1 - (1 - (1 - progress)) ** 3) if progress < 1 else 0.0
        # suavizado simétrico: fuera (-offset) -> 0 -> fuera (-offset)
        dx = -width * 0.40 * (1 - progress) ** 2
        mask = _shift_layer(mask, dx, 0)
        outline = _shift_layer(outline, dx, 0)
        shadow = _shift_layer(shadow, dx, 0)
    elif motion == "shake":
        decay = max(0.0, 1 - t / 0.35)
        dx = math.sin(t * math.pi * 26) * width * 0.012 * decay
        mask = _shift_layer(mask, dx, 0)
        outline = _shift_layer(outline, dx, 0)
        shadow = _shift_layer(shadow, dx, 0)
    elif motion == "zoom":
        # Respiración suave: nunca crece por encima del original para no
        # recortar la primera y la última letra contra el borde del emoji.
        factor = 1 - 0.05 * (1 - math.cos(2 * math.pi * t)) / 2
        mask = _scale_layer(mask, factor)
        outline = _scale_layer(outline, factor)
        shadow = _scale_layer(shadow, factor)
    elif motion == "sway":
        bbox = mask.getbbox() or (0, 0, width, height)
        center = ((bbox[0] + bbox[2]) // 2, (bbox[1] + bbox[3]) // 2)
        angle = 4.5 * math.sin(2 * math.pi * t)
        mask = _rotate_layer(mask, angle, center)
        outline = _rotate_layer(outline, angle, center)
        shadow = _rotate_layer(shadow, angle, center)
    elif motion == "jelly":
        wobble = math.sin(2 * math.pi * t)
        scale_x = 1 + 0.045 * wobble
        scale_y = 1 - 0.045 * wobble
        mask = _stretch_layer(mask, scale_x, scale_y)
        outline = _stretch_layer(outline, scale_x, scale_y)
        shadow = _stretch_layer(shadow, scale_x, scale_y)
    elif motion == "float":
        dy = -math.sin(2 * math.pi * t) * height * 0.05
        mask = _shift_layer(mask, 0, dy)
        outline = _shift_layer(outline, 0, dy)
        shadow = _shift_layer(shadow, 0, dy)
    elif motion == "glitch":
        # Saltos horizontales seudosaleatorios que empiezan y acaban quietos:
        # así el loop no da un salto al repetir.
        amount = 0.0
        if 0.04 < t < 0.62:
            step = int(t * FRAME_COUNT) // 2
            rnd = ((step * 2654435761) % 997) / 997
            amount = (rnd - 0.5) * 2
        dx = amount * width * 0.016
        mask = _shift_layer(mask, dx, 0)
        outline = _shift_layer(outline, dx, 0)
        shadow = _shift_layer(shadow, dx, 0)
    elif motion == "flicker":
        # Parpadeo tipo neón: la primera y la última imagen son iguales.
        pulse = max(0.0, math.sin(2 * math.pi * 7 * t))
        factor = 1 - 0.55 * pulse**6

        def dim(image: Image.Image) -> Image.Image:
            if image.mode == "L":
                return image.point(lambda value: int(value * factor))
            out = image.copy()
            out.putalpha(
                out.getchannel("A").point(lambda value: int(value * factor))
            )
            return out

        mask = dim(mask)
        outline = dim(outline)
        shadow = dim(shadow)
    elif motion == "heartbeat":
        # Dos latidos por loop; el pico cae justo en el corte, sin salto.
        beat = (0.5 + 0.5 * math.cos(4 * math.pi * t)) ** 6
        factor = 1 + 0.09 * beat
        mask = _scale_layer(mask, factor)
        outline = _scale_layer(outline, factor)
        shadow = _scale_layer(shadow, factor)
    elif motion == "drop":
        # Cae desde arriba, se queda y vuelve a subir: empieza y acaba fuera por
        # arriba, así el loop no salta.
        if t < 0.40:
            progress = t / 0.40
            dy = -height * 0.75 * (1 - _ease_out_back(progress))
        elif t < 0.72:
            dy = 0.0
        elif t < 0.94:
            progress = (t - 0.72) / 0.22
            dy = -height * 0.75 * progress**2
        else:
            dy = -height * 0.75
        mask = _shift_layer(mask, 0, dy)
        outline = _shift_layer(outline, 0, dy)
        shadow = _shift_layer(shadow, 0, dy)
    elif motion == "scroll":
        # El lienzo completo se desplaza al final del render (fondo, borde,
        # texto y emojis a la vez): si solo se movieran las capas del texto, la
        # palabra se deslizaría sobre su propio fondo y se vería rota.
        pass
    elif motion == "spring":
        # Rebote elástico amortiguado (inspirado en emoji CryptoPJ): se comprime,
        # sobrepasa y oscila hasta asentarse, y repite el ciclo sin salto.
        kf = (
            (0.000, 1.000), (0.167, 0.750), (0.211, 1.000), (0.254, 1.107),
            (0.339, 0.947), (0.425, 1.026), (0.510, 0.987), (0.596, 1.006),
            (0.681, 0.997), (0.767, 1.002), (0.852, 0.999), (0.876, 0.999),
            (0.994, 1.000),
        )
        factor = 1.0
        for i in range(len(kf) - 1):
            t0, v0 = kf[i]
            t1, v1 = kf[i + 1]
            if t <= t1 or i == len(kf) - 2:
                span = max(1e-6, t1 - t0)
                p = max(0.0, min(1.0, (t - t0) / span))
                factor = v0 + (v1 - v0) * p
                break
        mask = _scale_layer(mask, factor)
        outline = _scale_layer(outline, factor)
        shadow = _scale_layer(shadow, factor)
    elif motion == "wiggle":
        # Meneo vivo con giro de moneda visto desde arriba: rota sobre sí mismo,
        # se inclina y cambia de tamaño a la vez que se menea.
        cycle = 2 * math.pi * t
        angle = 360 * t + 16 * math.sin(cycle)
        # Giro de moneda: se aplana en X y muestra la cara opuesta al dar la vuelta.
        cosine = math.cos(2 * math.pi * t)
        coin_x = 0.26 + 0.74 * abs(cosine)
        factor = 0.92 + 0.10 * (0.5 + 0.5 * math.sin(2 * cycle))
        dy = -math.sin(cycle) * height * 0.035
        bbox = mask.getbbox() or (0, 0, width, height)
        center = ((bbox[0] + bbox[2]) // 2, (bbox[1] + bbox[3]) // 2)

        def _coin_face(layer: Image.Image) -> Image.Image:
            face = layer.transpose(Image.FLIP_LEFT_RIGHT) if cosine < 0 else layer
            return _stretch_layer(face, coin_x * factor, factor)

        mask = _rotate_layer(_coin_face(mask), angle, center)
        outline = _rotate_layer(_coin_face(outline), angle, center)
        shadow = _rotate_layer(_coin_face(shadow), angle, center)
        mask = _shift_layer(mask, 0, dy)
        outline = _shift_layer(outline, 0, dy)
        shadow = _shift_layer(shadow, 0, dy)
    elif motion == "levitate":
        # Levitación suave con giro de moneda: flota arriba y abajo y, a la vez,
        # gira como moneda mostrando la cara opuesta al dar la vuelta.
        kf = (
            (0.000, 0.010), (0.139, 0.115), (0.206, -0.045), (0.356, -0.115),
            (0.517, 0.082), (0.667, -0.049), (0.817, 0.049), (0.956, 0.010),
            (1.000, 0.010),
        )
        dyf = 0.0
        for i in range(len(kf) - 1):
            t0, v0 = kf[i]
            t1, v1 = kf[i + 1]
            if t <= t1 or i == len(kf) - 2:
                span = max(1e-6, t1 - t0)
                p = max(0.0, min(1.0, (t - t0) / span))
                p = p * p * (3 - 2 * p)
                dyf = v0 + (v1 - v0) * p
                break
        dy = dyf * height * 0.22
        factor = 1.04 + 0.015 * math.sin(2 * math.pi * t)
        # Giro de moneda: se comprime y muestra la cara opuesta en la media vuelta.
        cosine = math.cos(2 * math.pi * t)
        coin_x = 0.26 + 0.74 * abs(cosine)

        def _coin_face(layer: Image.Image) -> Image.Image:
            face = layer.transpose(Image.FLIP_LEFT_RIGHT) if cosine < 0 else layer
            return _stretch_layer(face, coin_x * factor, factor)

        mask = _shift_layer(_coin_face(mask), 0, dy)
        outline = _shift_layer(_coin_face(outline), 0, dy)
        shadow = _shift_layer(_coin_face(shadow), 0, dy)

    return mask, outline, shadow, cursor


def _render_word(
    text: str,
    palette: str,
    background: str,
    border: str,
    speed: str,
    frame_limit: int | None,
    emoji_images: list[list[bytes]] | None,
    font_key: str = "bold",
    max_count: int = MAX_WORD_EMOJIS,
    text_color_key: str = "auto",
    stroke_color_key: str = "auto",
    motion: str = "none",
    with_shadow: bool = True,
) -> tuple[list[list[bytes]], list[bytes]]:
    """Renderiza la palabra completa y devuelve los trozos y la tira unida.

    El texto (con los emojis premium en línea) se dibuja una sola vez en un
    lienzo ancho, con el tamaño y el número de emojis calculados a partir de su
    ancho real. Luego se corta en cuadrados exactos: las letras no llevan
    separación y un corte puede partir una letra. El fondo y el borde son una
    sola forma continua, centrada, con las esquinas redondeadas solo en los
    extremos.
    """
    plan = _word_plan(text, CANVAS, font_key=font_key, max_count=max_count)
    count = int(plan["count"])
    height = CANVAS
    width = int(plan["width"])
    text_layer = _word_text_masks(plan, width, height, emoji_images)
    pad = int(plan["pad"])
    badge_width = max(1, int(plan["badge_width"]))
    badge_x = int(plan["margin"]) + int(text_layer.get("shift", 0))

    palette_data = PALETTES.get(palette) or PALETTES["neon"]
    background = background if background in BACKGROUNDS else "gradient"
    border = border if border in BORDERS else "spin"
    motion = motion if motion in MOTIONS else "none"
    turns = SPEED_TURNS.get(speed, SPEED_TURNS["normal"])
    colors = [_rgb(color) for color in palette_data["colors"]]

    # La forma abarca solo la palabra y queda centrada en la tira de emojis.
    radius = min(int(height * ROUND_RADIUS), max(1, badge_width // 2))
    ring_width = max(2, min(38, pad - 6))
    badge = _rounded_mask(badge_width, height, radius)
    ring = _rounded_ring_mask(
        badge_width,
        height,
        radius,
        ring_width=ring_width,
        dashed=(border == "dashed"),
    )
    if border == "double":
        thin = max(2, ring_width // 2)
        outer_ring = _rounded_ring_mask(
            badge_width, height, radius, ring_width=thin, inset=4
        )
        inner_ring = _rounded_ring_mask(
            badge_width,
            height,
            radius,
            ring_width=thin,
            inset=4 + thin + 4,
        )
        ring = ImageChops.lighter(outer_ring, inner_ring)
    # El anillo giratorio se tiñe con un degradado cónico que rota.
    conic = _conic(colors, height) if border == "spin" else None

    dark_text = _luminance(colors) > 0.62
    text_color = TEXT_COLOR_VALUES.get(
        text_color_key, (17, 17, 17) if dark_text else (255, 255, 255)
    )
    if stroke_color_key == "none":
        stroke_color = None
    elif stroke_color_key == "palette":
        stroke_color = colors[0]
    else:
        # Si el color de letra es explícito, el contorno automático contrasta
        # con la letra, no con la paleta.
        explicit_text = TEXT_COLOR_VALUES.get(text_color_key)
        if stroke_color_key == "auto" and explicit_text is not None:
            default_stroke = (
                (8, 8, 12) if _luminance([explicit_text]) > 0.5 else (255, 255, 255)
            )
        else:
            default_stroke = (255, 255, 255) if dark_text else (8, 8, 12)
        stroke_color = TEXT_COLOR_VALUES.get(stroke_color_key, default_stroke)

    flat_text = Image.new("RGBA", (width, height), (*text_color, 255))
    flat_stroke = (
        Image.new("RGBA", (width, height), (*stroke_color, 235))
        if stroke_color is not None
        else None
    )
    solid_bg = Image.new("RGBA", (badge_width, height), (*colors[0], 255))

    badge_alpha = Image.new("L", (width, height), 0)
    badge_alpha.paste(badge, (badge_x, 0))
    shadow = text_layer["shadow"]
    if background != "transparent":
        shadow = shadow.copy()
        shadow.putalpha(
            ImageChops.multiply(shadow.getchannel("A"), badge_alpha)
        )
    if not with_shadow:
        # La sombra es una capa aparte: si se apaga, el texto queda limpio.
        shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    total_frames = FRAME_COUNT if frame_limit is None else max(1, min(FRAME_COUNT, frame_limit))
    static_mask = text_layer["mask"]
    static_outline = text_layer["outline"]
    per_chunk: list[list[bytes]] = [[] for _ in range(count)]
    joined_frames: list[bytes] = []
    for index in range(total_frames):
        phase = (index / FRAME_COUNT) * turns
        sweep = _horizontal_gradient(colors, badge_width, height, phase)
        mask, outline_layer, frame_shadow, cursor = _motion_layers(
            static_mask,
            static_outline,
            shadow,
            motion,
            index,
            total_frames,
            width,
            height,
        )

        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        if background == "transparent":
            text_sweep = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            text_sweep.paste(sweep, (badge_x, 0))
            canvas.alpha_composite(frame_shadow)
            if text_color_key == "auto":
                glyphs = _tinted(text_sweep, mask)
            else:
                glyphs = _tinted(flat_text, mask)
            canvas.alpha_composite(glyphs)
        else:
            shape = Image.new("RGBA", (badge_width, height), (0, 0, 0, 0))
            if background == "solid":
                fill = solid_bg
            elif background == "pulse":
                fill = Image.new(
                    "RGBA", (badge_width, height), (*_cyclic_color(colors, phase), 255)
                )
            else:
                fill = sweep
            shape.paste(fill, (0, 0), badge)
            full_shape = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            full_shape.paste(shape, (badge_x, 0))
            canvas.alpha_composite(full_shape)
            canvas.alpha_composite(frame_shadow)
            canvas.alpha_composite(_tinted(flat_text, mask))

        if flat_stroke is not None:
            canvas.alpha_composite(_tinted(flat_stroke, outline_layer))
        # Cursor de la máquina de escribir: solo mientras se escribe el texto.
        if cursor is not None:
            x, top, bottom = cursor
            bar_width = max(2, int(height * 0.022))
            cursor_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            ImageDraw.Draw(cursor_layer).rectangle(
                (x, top, x + bar_width, bottom), fill=(*text_color, 220)
            )
            canvas.alpha_composite(cursor_layer)
        # Los emojis premium conservan su color y su animación, y van encima
        # del texto como una letra más.
        if text_layer["placements"]:
            emoji_shadow, emoji_art = _premium_art(
                text_layer["placements"],
                width,
                height,
                index,
                total_frames,
                reveal_x=cursor[0] if cursor is not None else None,
                with_shadow=with_shadow,
            )
            canvas.alpha_composite(emoji_shadow)
            canvas.alpha_composite(emoji_art)

        if border != "none":
            # El borde se tiñe con un color que contraste con el relleno: si
            # usara el mismo degradado del fondo sería invisible.
            contrast = stroke_color or (
                (255, 255, 255) if dark_text else (8, 8, 12)
            )
            rim = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            if border == "spin" and conic is not None:
                texture = conic.rotate(
                    -360 * index / total_frames, resample=Image.BICUBIC
                ).resize((badge_width, height), Image.BILINEAR)
                base_rim = Image.blend(
                    texture.convert("RGB"),
                    Image.new("RGB", texture.size, contrast),
                    0.55,
                )
                rim.paste(_tinted(base_rim, ring), (badge_x, 0))
                canvas.alpha_composite(rim)
            else:
                rim_fill = Image.blend(
                    sweep.convert("RGB"),
                    Image.new("RGB", sweep.size, contrast),
                    0.55,
                )
                rim.paste(_tinted(rim_fill, ring), (badge_x, 0))
                if border == "glow":
                    canvas.alpha_composite(
                        rim.filter(ImageFilter.GaussianBlur(10))
                    )
                    canvas.alpha_composite(rim)
                elif border == "neon":
                    canvas.alpha_composite(
                        rim.filter(ImageFilter.GaussianBlur(6))
                    )
                    canvas.alpha_composite(rim)
                elif border == "pulse":
                    beat = 0.5 + 0.5 * math.sin(2 * math.pi * index / total_frames)
                    pulsing = rim.copy()
                    pulsing.putalpha(
                        pulsing.getchannel("A").point(
                            lambda value: int(value * (0.35 + 0.65 * beat))
                        )
                    )
                    canvas.alpha_composite(pulsing)
                else:
                    canvas.alpha_composite(rim)

        if motion == "scroll":
            # Marquesina infinita: se mueve el lienzo entero (fondo, borde,
            # texto y emojis) una tira completa por loop y se enrolla, así los
            # trozos siguen siendo el recorte exacto de la misma palabra y no se
            # ve ninguna unión.
            offset = int(round(-width * (index / total_frames)))
            wrapped = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            wrapped.paste(canvas, (offset, 0))
            wrapped.paste(canvas, (offset + width, 0))
            canvas = wrapped

        # Se reduce el lienzo completo antes de cortar: así el reescalado es
        # continuo y no aparece una costura en la unión de dos emojis.
        small = canvas.resize((SIZE * count, SIZE), Image.LANCZOS)
        for piece_index in range(count):
            left = piece_index * SIZE
            piece = small.crop((left, 0, left + SIZE, SIZE))
            per_chunk[piece_index].append(piece.tobytes())
        joined_frames.append(small.tobytes())
    return per_chunk, joined_frames


def render_word_frames(
    text: str,
    palette: str = "neon",
    background: str = "gradient",
    border: str = "spin",
    speed: str = "normal",
    frame_limit: int | None = None,
    emoji_images: list[list[bytes]] | None = None,
    font_key: str = "bold",
    text_color_key: str = "auto",
    stroke_color_key: str = "auto",
    motion: str = "none",
    with_shadow: bool = True,
) -> list[list[bytes]]:
    """Un fotograma por trozo: la palabra se corta en cuadrados exactos."""
    chunks, _ = _render_word(
        text,
        palette,
        background,
        border,
        speed,
        frame_limit,
        emoji_images,
        font_key,
        text_color_key=text_color_key,
        stroke_color_key=stroke_color_key,
        motion=motion,
        with_shadow=with_shadow,
    )
    return chunks


def render_word_joined(
    text: str,
    palette: str = "neon",
    background: str = "gradient",
    border: str = "spin",
    speed: str = "normal",
    emoji_images: list[list[bytes]] | None = None,
    font_key: str = "bold",
    text_color_key: str = "auto",
    stroke_color_key: str = "auto",
    motion: str = "none",
    with_shadow: bool = True,
) -> list[bytes]:
    """La palabra entera en una sola tira (para la vista previa sin huecos)."""
    _, joined = _render_word(
        text,
        palette,
        background,
        border,
        speed,
        None,
        emoji_images,
        font_key,
        text_color_key=text_color_key,
        stroke_color_key=stroke_color_key,
        motion=motion,
        with_shadow=with_shadow,
    )
    return joined


# --------------------------------------------------------------------------- #
# Efectos sobre el logo de un canal
# --------------------------------------------------------------------------- #
def _scale(image: Image.Image, factor: float) -> Image.Image:
    """Escala manteniendo el lienzo y centrando el resultado."""
    width = max(1, int(image.width * factor))
    height = max(1, int(image.height * factor))
    resized = image.resize((width, height), Image.LANCZOS)
    out = Image.new("RGBA", image.size, (0, 0, 0, 0))
    out.alpha_composite(
        resized, ((image.width - width) // 2, (image.height - height) // 2)
    )
    return out


def _stretch(image: Image.Image, scale_x: float, scale_y: float) -> Image.Image:
    """Estira de forma independiente en X e Y, centrando el resultado."""
    width = max(1, int(image.width * scale_x))
    height = max(1, int(image.height * scale_y))
    resized = image.resize((width, height), Image.LANCZOS)
    out = Image.new("RGBA", image.size, (0, 0, 0, 0))
    out.alpha_composite(
        resized, ((image.width - width) // 2, (image.height - height) // 2)
    )
    return out


def _fit(image: Image.Image, size: int) -> Image.Image:
    """Encaja la imagen entera en un lienzo de `size`, reduciendo si hace falta.

    Se usa para girar formas cuadradas/redondeadas sin cortarles las esquinas.
    """
    if image.width > size or image.height > size:
        ratio = size / max(image.width, image.height)
        image = image.resize(
            (max(1, int(image.width * ratio)), max(1, int(image.height * ratio))),
            Image.LANCZOS,
        )
    if image.size == (size, size):
        return image
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.alpha_composite(image, ((size - image.width) // 2, (size - image.height) // 2))
    return out


def _logo_frame(
    logo: Image.Image,
    effect: str,
    t: float,
    colors: list[tuple[int, int, int]],
    conic: Image.Image,
    ring: Image.Image,
    shape: str = "circle",
) -> Image.Image:
    size = logo.width

    if effect == "shine":
        frame = logo.copy()
        band = Image.new("L", (size, size), 0)
        x = -260 + t * (size + 520)
        skew = 100
        ImageDraw.Draw(band).polygon(
            [(x, 0), (x + 70, 0), (x + 70 + skew, size), (x + skew, size)],
            fill=210,
        )
        band = band.filter(ImageFilter.GaussianBlur(12))
        alpha = ImageChops.multiply(band, logo.getchannel("A"))
        white = Image.new("RGBA", (size, size), (255, 255, 255, 255))
        white.putalpha(alpha.point(lambda value: int(value * 0.8)))
        frame.alpha_composite(white)
        return frame

    if effect in ("pulse", "pop"):
        waves = 2 if effect == "pop" else 1
        amount = 0.13 if effect == "pop" else 0.09
        factor = 1 + amount * math.sin(2 * math.pi * waves * t)
        return _scale(logo, factor)

    if effect == "coin":
        angle = 2 * math.pi * t
        cosine = math.cos(angle)
        width_factor = max(0.05, abs(cosine))
        # La cara trasera se ve un poco más oscura, como una moneda real.
        front = cosine >= 0
        brightness = (0.80 if front else 0.68) + 0.20 * width_factor
        image = ImageEnhance.Brightness(logo).enhance(brightness)
        image = image.resize((max(1, int(size * width_factor)), size), Image.LANCZOS)
        left = (size - image.width) // 2
        frame = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        frame.alpha_composite(image, (left, 0))

        # Máscara con la forma actual de la moneda (achatada por el giro).
        coin_mask = Image.new("L", (size, size), 0)
        coin_mask.paste(image.getchannel("A"), (left, 0))

        # Brillo metálico que recorre la cara siguiendo el movimiento.
        band = Image.new("L", (size, size), 0)
        center = size / 2 + 0.30 * size * math.sin(angle)
        band_width = max(8, int(size * 0.12 * width_factor) + 10)
        skew = 70
        ImageDraw.Draw(band).polygon(
            [
                (center - band_width / 2, 0),
                (center + band_width / 2, 0),
                (center + band_width / 2 + skew, size),
                (center - band_width / 2 + skew, size),
            ],
            fill=200,
        )
        band = band.filter(ImageFilter.GaussianBlur(10))
        alpha = ImageChops.multiply(band, coin_mask)
        shine_strength = 0.20 + 0.65 * width_factor
        white = Image.new("RGBA", (size, size), (255, 255, 255, 255))
        white.putalpha(alpha.point(lambda value: int(value * shine_strength)))
        frame.alpha_composite(white)

        # Destello en el canto cuando la moneda queda casi de perfil.
        edge = max(0.0, 1 - width_factor / 0.45)
        if edge > 0:
            glint = Image.new("L", (size, size), 0)
            line_x = left + image.width // 2
            ImageDraw.Draw(glint).rectangle(
                (line_x - 2, int(size * 0.10), line_x + 2, int(size * 0.90)),
                fill=235,
            )
            glint = glint.filter(ImageFilter.GaussianBlur(3))
            solid_coin = coin_mask.point(lambda value: 255 if value else 0)
            glint = ImageChops.multiply(glint, solid_coin)
            white_edge = Image.new("RGBA", (size, size), (255, 255, 255, 255))
            white_edge.putalpha(glint.point(lambda value: int(value * edge * 0.9)))
            frame.alpha_composite(white_edge)
        return frame

    if effect == "spin":
        if shape == "circle":
            return logo.rotate(-360 * t, resample=Image.BICUBIC)
        # Al girar, la diagonal de un cuadrado es mayor que su lado: se rota
        # expandiendo el lienzo y se reduce para que las esquinas no se corten.
        return _fit(logo.rotate(-360 * t, resample=Image.BICUBIC, expand=True), size)

    if effect == "bounce":
        lift = int(24 * abs(math.sin(2 * math.pi * t)))
        squash = 1 + 0.04 * math.cos(4 * math.pi * t)
        image = _scale(logo, squash)
        frame = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        frame.paste(image, (0, -lift), image)
        return frame

    if effect in ("wobble", "swing"):
        amplitude = 7 if effect == "wobble" else 10
        waves = 2 if effect == "wobble" else 1
        angle = amplitude * math.sin(2 * math.pi * waves * t)
        if shape == "circle":
            return logo.rotate(angle, resample=Image.BICUBIC)
        return _fit(logo.rotate(angle, resample=Image.BICUBIC, expand=True), size)

    if effect == "shake":
        dx = int(7 * math.sin(2 * math.pi * 24 * t))
        dy = int(6 * math.cos(2 * math.pi * 30 * t))
        frame = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        frame.paste(logo, (dx, dy), logo)
        return frame

    if effect == "rainbow":
        hue, saturation, value = logo.convert("HSV").split()
        shift = int(256 * t) % 256
        hue = hue.point(lambda channel: (channel + shift) % 256)
        frame = Image.merge("HSV", (hue, saturation, value)).convert("RGBA")
        frame.putalpha(logo.getchannel("A"))
        return frame

    if effect == "glow":
        color = _cyclic_color(colors, t)
        strength = 90 + int(70 * (0.5 + 0.5 * math.sin(2 * math.pi * t)))
        silhouette = logo.getchannel("A").filter(ImageFilter.GaussianBlur(14))
        glow = Image.new("RGBA", (size, size), (*color, 255))
        glow.putalpha(silhouette.point(lambda value: int(value * strength / 255)))
        frame = _scale(glow, 1.06)
        frame.alpha_composite(logo)
        return frame

    if effect == "ring":
        rim = _tinted(conic.rotate(360 * t, resample=Image.BICUBIC), ring)
        frame = rim
        inner = _scale(logo, 0.82)
        frame.alpha_composite(
            inner, ((size - inner.width) // 2, (size - inner.height) // 2)
        )
        return frame

    if effect == "drop":
        # Gota/onda: crece y se encoge como una onda, con ondas expansivas detrás.
        wave = 0.5 - 0.5 * math.cos(2 * math.pi * t)
        factor = 0.90 + 0.20 * wave
        frame = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        color = _cyclic_color(colors, t)
        draw = ImageDraw.Draw(frame)
        for index in range(3):
            progress = (t + index / 3) % 1.0
            radius = int(size * (0.30 + 0.32 * progress))
            alpha = int(120 * (1 - progress))
            if alpha > 0:
                draw.ellipse(
                    (
                        size / 2 - radius,
                        size / 2 - radius,
                        size / 2 + radius,
                        size / 2 + radius,
                    ),
                    outline=(*color, alpha),
                    width=max(2, int(size * 0.02)),
                )
        frame.alpha_composite(_stretch(logo, factor, 2 - factor))
        return frame

    if effect == "float":
        dy = int(14 * math.sin(2 * math.pi * t))
        frame = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        frame.paste(logo, (0, dy), logo)
        return frame

    if effect == "flip":
        cosine = math.cos(2 * math.pi * t)
        width_factor = max(0.06, abs(cosine))
        image = logo
        if cosine < 0:
            image = logo.transpose(Image.FLIP_LEFT_RIGHT)
        image = image.resize((max(1, int(size * width_factor)), size), Image.LANCZOS)
        frame = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        frame.alpha_composite(image, ((size - image.width) // 2, 0))
        return ImageEnhance.Brightness(frame).enhance(
            0.82 + 0.18 * width_factor
        )

    if effect == "jelly":
        amount = 0.10 * math.sin(2 * math.pi * t)
        return _stretch(logo, 1 + amount, 1 - amount)

    if effect == "heartbeat":
        beat = max(0.0, math.sin(4 * math.pi * t))
        return _scale(logo, 1 + 0.16 * beat ** 2)

    if effect == "orbit":
        dx = int(size * 0.06 * math.cos(2 * math.pi * t))
        dy = int(size * 0.06 * math.sin(2 * math.pi * t))
        frame = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        frame.paste(logo, (dx, dy), logo)
        return frame

    if effect == "ripple":
        frame = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        color = _cyclic_color(colors, t)
        draw = ImageDraw.Draw(frame)
        for index in range(3):
            progress = (t + index / 3) % 1.0
            radius = int(size * (0.28 + 0.32 * progress))
            alpha = int(150 * (1 - progress))
            if alpha > 0:
                draw.ellipse(
                    (
                        size / 2 - radius,
                        size / 2 - radius,
                        size / 2 + radius,
                        size / 2 + radius,
                    ),
                    outline=(*color, alpha),
                    width=max(2, int(size * 0.02)),
                )
        frame.alpha_composite(logo)
        return frame

    if effect == "sparkle":
        frame = logo.copy()
        stars = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(stars)
        for index in range(4):
            angle = 2 * math.pi * (t + index / 4)
            cx = size / 2 + size * 0.34 * math.cos(angle)
            cy = size / 2 + size * 0.34 * math.sin(angle)
            twinkle = 0.5 + 0.5 * math.sin(4 * math.pi * (t + index / 4))
            radius = max(2, int(size * 0.05 * twinkle))
            alpha = int(230 * twinkle)
            draw.line(
                (cx - radius, cy, cx + radius, cy),
                fill=(255, 255, 255, alpha),
                width=max(1, int(size * 0.012)),
            )
            draw.line(
                (cx, cy - radius, cx, cy + radius),
                fill=(255, 255, 255, alpha),
                width=max(1, int(size * 0.012)),
            )
        frame.alpha_composite(stars)
        return frame

    if effect == "neon":
        color = _cyclic_color(colors, t)
        strength = 0.5 + 0.5 * math.sin(2 * math.pi * t)
        silhouette = logo.getchannel("A").filter(
            ImageFilter.GaussianBlur(int(size * 0.05))
        )
        glow = Image.new("RGBA", (size, size), (*color, 255))
        glow.putalpha(
            silhouette.point(lambda value: int(value * (0.40 + 0.55 * strength)))
        )
        frame = _scale(glow, 1.12)
        frame.alpha_composite(logo)
        return frame

    if effect == "spring":
        # Rebote elástico amortiguado (como el emoji de CryptoPJ).
        kf = (
            (0.000, 1.000), (0.167, 0.750), (0.211, 1.000), (0.254, 1.107),
            (0.339, 0.947), (0.425, 1.026), (0.510, 0.987), (0.596, 1.006),
            (0.681, 0.997), (0.767, 1.002), (0.852, 0.999), (0.876, 0.999),
            (0.994, 1.000),
        )
        factor = 1.0
        for i in range(len(kf) - 1):
            t0, v0 = kf[i]
            t1, v1 = kf[i + 1]
            if t <= t1 or i == len(kf) - 2:
                span = max(1e-6, t1 - t0)
                p = max(0.0, min(1.0, (t - t0) / span))
                factor = v0 + (v1 - v0) * p
                break
        return _scale(logo, factor)

    if effect == "wiggle":
        # Meneo vivo con giro de moneda visto desde arriba: rota, se aplana al
        # girar, muestra la cara opuesta y se menea a la vez.
        cycle = 2 * math.pi * t
        angle = 360 * t + 16 * math.sin(cycle)
        factor = 0.92 + 0.10 * (0.5 + 0.5 * math.sin(2 * cycle))
        cosine = math.cos(2 * math.pi * t)
        coin_x = 0.26 + 0.74 * abs(cosine)
        dy = int(-math.sin(cycle) * size * 0.035)
        face = logo.transpose(Image.FLIP_LEFT_RIGHT) if cosine < 0 else logo
        image = _stretch(face, factor * coin_x, factor)
        if shape == "circle":
            rotated = image.rotate(angle, resample=Image.BICUBIC)
        else:
            rotated = _fit(
                image.rotate(angle, resample=Image.BICUBIC, expand=True), size
            )
        frame = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        frame.paste(rotated, (0, dy), rotated)
        return frame

    if effect == "levitate":
        # Levitación suave con giro de moneda: flota y gira mostrando la cara opuesta.
        kf = (
            (0.000, 0.010), (0.139, 0.115), (0.206, -0.045), (0.356, -0.115),
            (0.517, 0.082), (0.667, -0.049), (0.817, 0.049), (0.956, 0.010),
            (1.000, 0.010),
        )
        dyf = 0.0
        for i in range(len(kf) - 1):
            t0, v0 = kf[i]
            t1, v1 = kf[i + 1]
            if t <= t1 or i == len(kf) - 2:
                span = max(1e-6, t1 - t0)
                p = max(0.0, min(1.0, (t - t0) / span))
                p = p * p * (3 - 2 * p)
                dyf = v0 + (v1 - v0) * p
                break
        dy = int(dyf * size * 0.22)
        factor = 1.04 + 0.015 * math.sin(2 * math.pi * t)
        cosine = math.cos(2 * math.pi * t)
        coin_x = 0.26 + 0.74 * abs(cosine)
        face = logo.transpose(Image.FLIP_LEFT_RIGHT) if cosine < 0 else logo
        image = _stretch(face, factor * coin_x, factor)
        frame = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        frame.paste(image, (0, dy), image)
        return frame

    return logo.copy()


def _logo_background(
    size: int,
    colors: list[tuple[int, int, int]],
    background: str,
    phase: float,
) -> Image.Image | None:
    """Fondo redondeado (degradado animado o color sólido) para la imagen."""
    if background == "transparent":
        return None
    mask = _rounded_mask(size, size, int(size * ROUND_RADIUS))
    if background == "solid":
        fill = Image.new("RGB", (size, size), colors[0])
    elif background == "pulse":
        fill = Image.new("RGB", (size, size), _cyclic_color(colors, phase))
    else:
        fill = _horizontal_gradient(colors, size, size, phase)
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    layer.paste(fill, (0, 0), mask)
    return layer


def render_logo_frames(
    logo: Image.Image,
    effect: str = "shine",
    palette: str = "neon",
    shape: str = "circle",
    background: str = "transparent",
) -> list[bytes]:
    effect = effect if effect in LOGO_EFFECTS else "shine"
    background = background if background in BACKGROUNDS else "transparent"
    palette_data = PALETTES.get(palette) or PALETTES["neon"]
    colors = [_rgb(color) for color in palette_data["colors"]]
    turns = SPEED_TURNS["normal"]

    conic = _conic(colors, logo.width)
    ring = _shape_ring_mask(logo.width, shape, ring_width=28, dashed=False)

    frames: list[bytes] = []
    for index in range(FRAME_COUNT):
        t = index / FRAME_COUNT
        frame = _logo_frame(logo, effect, t, colors, conic, ring, shape)
        if background != "transparent":
            layer = _logo_background(
                logo.width, colors, background, t * turns
            )
            if layer is not None:
                layer.alpha_composite(frame)
                frame = layer
        frames.append(frame.resize((SIZE, SIZE), Image.LANCZOS).tobytes())
    return frames


def generate_logo(
    source: bytes, effect: str = "shine", palette: str = "neon"
) -> bytes:
    """Anima la foto de un canal (círculo) con el efecto indicado."""
    logo = round_image(source, CANVAS)
    frames = render_logo_frames(logo, effect, palette, "circle")
    return encode_webm(frames)


def generate_image_emoji(
    source: bytes,
    shape: str = "circle",
    effect: str = "shine",
    palette: str = "neon",
    background: str = "transparent",
) -> bytes:
    """Anima una imagen y, si se pide, la compone sobre un fondo de color."""
    # Un poco de margen para que los efectos que crecen no se corten.
    logo = _scale(shape_image(source, CANVAS, shape), 0.88)
    frames = render_logo_frames(logo, effect, palette, shape, background)
    return encode_webm(frames)


# --------------------------------------------------------------------------- #
# Encoding
# --------------------------------------------------------------------------- #
def _ffmpeg() -> str:
    binary = shutil.which("ffmpeg")
    if not binary:
        raise EmojiError(
            "ffmpeg no está instalado. Añádelo a la imagen (apt-get install ffmpeg)."
        )
    return binary


def _encode(
    frames: list[bytes], crf: int, width: int = SIZE, height: int = SIZE
) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
        target = Path(tmp.name)
    try:
        command = [
            _ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgba",
            "-s",
            f"{width}x{height}",
            "-r",
            str(FPS),
            "-i",
            "-",
            "-an",
            "-c:v",
            "libvpx-vp9",
            "-pix_fmt",
            "yuva420p",
            "-b:v",
            "0",
            "-crf",
            str(crf),
            "-row-mt",
            "1",
            "-threads",
            "2",
            "-metadata:s:v:0",
            "alpha_mode=1",
            str(target),
        ]
        process = subprocess.run(
            command,
            input=b"".join(frames),
            capture_output=True,
            check=False,
        )
        if process.returncode != 0:
            detail = process.stderr.decode("utf-8", "replace").strip()
            raise EmojiError(f"ffmpeg falló: {detail[-400:]}")
        return target.read_bytes()
    finally:
        target.unlink(missing_ok=True)


def encode_webm(
    frames: list[bytes],
    target_bytes: int = TARGET_BYTES,
    max_bytes: int = MAX_BYTES,
    width: int = SIZE,
    height: int = SIZE,
) -> bytes:
    best: bytes | None = None
    for crf in CRF_ATTEMPTS:
        data = _encode(frames, crf, width, height)
        if best is None or len(data) < len(best):
            best = data
        if len(data) <= target_bytes:
            return data
    assert best is not None
    if len(best) <= max_bytes:
        return best
    raise EmojiError(
        f"El WebM queda en {len(best) // 1024} KB y Telegram admite {max_bytes // 1024} KB. "
        "Prueba un texto más corto o un estilo más simple."
    )


MAX_PREVIEW_WIDTH = 1024  # la tira unida no debe salir gigantesca en el chat
PREVIEW_BG = (22, 24, 31)  # fondo opaco de la vista previa (MP4 no lleva alfa)
# Tablero para la vista previa cuando el fondo elegido es transparente: así se
# entiende que el hueco no es negro, sino transparencia.
PREVIEW_CHECKER = ((58, 62, 74), (44, 47, 56))


def _preview_backdrop(width: int, height: int) -> Image.Image:
    if width <= 0 or height <= 0:
        return Image.new("RGBA", (max(1, width), max(1, height)), (*PREVIEW_BG, 255))
    board = Image.new("RGBA", (width, height), (*PREVIEW_BG, 255))
    cell = max(6, height // 16)
    painter = ImageDraw.Draw(board)
    for y in range(0, height, cell):
        for x in range(0, width, cell):
            if (x // cell + y // cell) % 2 == 0:
                painter.rectangle(
                    (x, y, x + cell - 1, y + cell - 1), fill=(*PREVIEW_CHECKER[0], 255)
                )
    return board


def _encode_preview(frames: list[bytes], width: int, height: int) -> bytes:
    """MP4 H.264 para la vista previa: Telegram lo reproduce en el chat.

    Los stickers se codifican en WebM VP9 con alfa, pero `sendAnimation` no
    reproduce bien WebM: se ve como un archivo o solo el primer fotograma.
    """
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        target = Path(tmp.name)
    try:
        command = [
            _ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{width}x{height}",
            "-r",
            str(FPS),
            "-i",
            "-",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "23",
            "-preset",
            "veryfast",
            "-movflags",
            "+faststart",
            str(target),
        ]
        process = subprocess.run(
            command, input=b"".join(frames), capture_output=True, check=False
        )
        if process.returncode != 0:
            detail = process.stderr.decode("utf-8", "replace").strip()
            raise EmojiError(f"ffmpeg falló al crear la vista previa: {detail[-400:]}")
        return target.read_bytes()
    finally:
        target.unlink(missing_ok=True)


def generate_example(
    text: str = "Abcd",
    palette: str = "neon",
    background: str = "gradient",
    border: str = "none",
    font_key: str = "bold",
    text_color_key: str = "auto",
    stroke_color_key: str = "auto",
    motion: str = "none",
    frames: int = EXAMPLE_FRAMES,
    moving: bool = True,
    with_shadow: bool = True,
) -> list[bytes]:
    """Muestras de un estilo del menú: uno o varios emojis de 100x100.

    El texto se dibuja grande y se reparte en varios emojis con los mismos
    cortes que el emoji real, para que las letras se lean al tamaño del chat.
    Si `moving` es True la muestra se anima; si no, se repite el mismo
    fotograma para que quede quieta (por ejemplo, la de las fuentes).
    """
    chunks, _ = _render_word(
        text,
        palette,
        background,
        border,
        "normal",
        frames if moving else 1,
        None,
        font_key,
        text_color_key=text_color_key,
        stroke_color_key=stroke_color_key,
        motion=motion,
        with_shadow=with_shadow,
    )
    if moving:
        return [encode_webm(chunk) for chunk in chunks if chunk]
    return [encode_webm(chunk * frames) for chunk in chunks if chunk]


def generate_word(
    text: str,
    palette: str = "neon",
    background: str = "gradient",
    border: str = "spin",
    speed: str = "normal",
    emoji_images: list[list[bytes]] | None = None,
    font_key: str = "bold",
    text_color_key: str = "auto",
    stroke_color_key: str = "auto",
    motion: str = "none",
    with_shadow: bool = True,
) -> list[bytes]:
    """Un .webm por trozo; el fondo y el efecto son continuos entre trozos."""
    return [
        encode_webm(frames)
        for frames in render_word_frames(
            text,
            palette,
            background,
            border,
            speed,
            emoji_images=emoji_images,
            font_key=font_key,
            text_color_key=text_color_key,
            stroke_color_key=stroke_color_key,
            motion=motion,
            with_shadow=with_shadow,
        )
    ]


def generate_word_preview(
    text: str,
    palette: str = "neon",
    background: str = "gradient",
    border: str = "spin",
    speed: str = "normal",
    emoji_images: list[list[bytes]] | None = None,
    font_key: str = "bold",
    text_color_key: str = "auto",
    stroke_color_key: str = "auto",
    motion: str = "none",
    with_shadow: bool = True,
) -> bytes:
    """Vista previa en una sola tira, sin huecos entre trozos."""
    frames = render_word_joined(
        text,
        palette,
        background,
        border,
        speed,
        emoji_images,
        font_key,
        text_color_key=text_color_key,
        stroke_color_key=stroke_color_key,
        motion=motion,
        with_shadow=with_shadow,
    )
    width = len(frames[0]) // (SIZE * 4)
    height = SIZE
    if width > MAX_PREVIEW_WIDTH:
        scale = MAX_PREVIEW_WIDTH / width
        new_width = max(2, int(width * scale) // 2 * 2)
        new_height = max(2, int(round(height * scale)) // 2 * 2)
        frames = [
            Image.frombytes("RGBA", (width, height), frame)
            .resize((new_width, new_height), Image.LANCZOS)
            .tobytes()
            for frame in frames
        ]
        width, height = new_width, new_height

    # MP4 no admite alfa: se aplana sobre un fondo (tablero si el estilo es
    # transparente) para que la vista previa se reproduzca inline y no parezca
    # un archivo estático.
    backdrop = (
        _preview_backdrop(width, height)
        if background == "transparent"
        else Image.new("RGBA", (width, height), (*PREVIEW_BG, 255))
    )
    flat: list[bytes] = []
    for frame in frames:
        image = Image.frombytes("RGBA", (width, height), frame)
        canvas = backdrop.copy()
        canvas.alpha_composite(image)
        flat.append(canvas.convert("RGB").tobytes())
    return _encode_preview(flat, width, height)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Genera un custom emoji animado.")
    parser.add_argument("text", nargs="?")
    parser.add_argument("-o", "--output", default="emoji.webm")
    parser.add_argument("--palette", default="neon", choices=sorted(PALETTES))
    parser.add_argument("--bg", default="gradient", choices=sorted(BACKGROUNDS))
    parser.add_argument("--border", default="spin", choices=sorted(BORDERS))
    parser.add_argument("--speed", default="normal", choices=sorted(SPEED_TURNS))
    parser.add_argument(
        "--font",
        dest="font_key",
        default="bold",
        choices=sorted(FONT_CHOICES),
        help="tipografía del texto",
    )
    parser.add_argument("--logo", help="anima la foto de un canal con --effect")
    parser.add_argument("--effect", default="shine", choices=sorted(LOGO_EFFECTS))
    args = parser.parse_args()

    if args.logo:
        data = generate_logo(Path(args.logo).read_bytes(), args.effect, args.palette)
        Path(args.output).write_bytes(data)
        print(f"OK: {args.output} ({len(data) / 1024:.1f} KB, {FRAME_COUNT} frames)")
    else:
        if not args.text:
            parser.error("indica un texto o usa --logo <imagen>")
        chunks = generate_word(
            args.text,
            args.palette,
            args.bg,
            args.border,
            args.speed,
            font_key=args.font_key,
        )
        target = Path(args.output)
        if len(chunks) == 1:
            targets = [target]
        else:
            targets = [
                target.with_name(f"{target.stem}_{index + 1}{target.suffix}")
                for index in range(len(chunks))
            ]
        for path, data in zip(targets, chunks):
            path.write_bytes(data)
            print(
                f"OK: {path} ({len(data) / 1024:.1f} KB, {FRAME_COUNT} frames)"
            )
