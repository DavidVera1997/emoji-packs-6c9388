# Logo de canal → custom emoji

Bot de Telegram que toma la foto de perfil de un canal, la recorta en **círculo**,
la deja en **100×100** y crea un pack de **custom emojis** (`t.me/addemoji/…`).
Cuando cambias el logo, el pack se actualiza solo. También genera **emojis
animados** con texto, degradados y bordes en movimiento, y **emojis animados a
partir de una imagen** (redondo, cuadrado o redondeado) con 21 efectos. Todo se
gestiona desde el propio chat del bot.

## Arranque con Docker

```bash
cp .env.example .env      # pon tu BOT_TOKEN
docker compose up -d --build
docker compose logs -f    # ver actividad
docker compose down       # parar
```

El contenedor se reinicia solo (`restart: unless-stopped`) y guarda el estado en
`./data/channels.json` (volumen montado, sobrevive a reinicios y rebuilds).

## Panel del bot

Abre el bot y pulsa **Start**. El panel tiene:

- **➕ Añadir a un canal** — abre el diálogo de Telegram para añadir el bot como
  administrador. En cuanto entra, crea el pack solo y te avisa por privado.
- **📋 Mis canales** — lista tus canales; al abrir uno puedes actualizarlo,
  abrir el pack en Telegram o quitarlo (borra también el pack).
- **✨ Emoji animado** — crea un emoji animado con tu texto y guárdalo en tu
  propio pack (ver abajo).
- **🖼 Emoji desde imagen** — envía una foto y conviértela en emoji animado
  eligiendo forma y efecto (ver abajo).
- **ℹ️ Ayuda** — requisitos y limitaciones.

Comandos equivalentes: `/start`, `/canales`, `/convert @micanal`,
`/animar` (también acepta `https://t.me/micanal`, el id `-100…`, o responder a
un mensaje reenviado del canal) y `/emoji`.

## Emojis animados

Pulsa **✨ Emoji animado** o escribe `/animar`:

1. Escribe el texto (1–3 caracteres se lee mejor; máximo 14). Puedes incluir
   **emojis premium** junto al texto: el bot los dibuja dentro del emoji como
   una letra más y **respeta su animación** si la tienen (los `.tgs` se
   renderizan con rlottie y los `.webm` con ffmpeg; si no se pueden animar, usa
   la miniatura estática). Los espacios se conservan con su ancho real, sin
   pegarlos ni forzarlos.
2. Elige paleta: neón, atardecer, oro, océano, fuego o hielo.
3. Elige fuente: sans negrita, sans, serif negrita, serif, mono negrita o mono.
4. Elige fondo: degradado animado, color sólido o transparente.
5. Elige borde: ninguno, anillo, giratorio, punteado o brillo.
6. Ves la vista previa real como sticker y puedes cambiar fuente, paleta,
   fondo, borde o velocidad antes de guardar.
7. **✅ Crear pack**: se crea (o reutiliza) tu pack animado personal y el emoji
   se añade al final.

Cada usuario tiene su propio pack (`anim<id>_by_<bot>`), hasta 200 emojis.
El nombre visible se puede cambiar con `ANIM_PACK_TITLE`. Los emojis animados
cumplen el formato de Telegram: 100×100, VP9/WebM, 30 fps, 2,8 s y menos de
64 KB (se baja el CRF automáticamente hasta que cabe).

### Varios emojis para una palabra

Solo hay que escribir la palabra (`PROMO`, `HOLA`, `BUENOS DÍAS`). El bot
calcula su ancho real y decide automáticamente en cuántos emojis repartirla,
sin separadores ni botones de división. Los cortes son verticales y atraviesan
una letra (o un emoji premium) por la mitad siempre que se puede: así las
letras quedan pegadas, sin huecos, y la palabra ocupa el menor número de emojis
posible (máximo 10 por palabra). La forma y el texto se centran en la tira, de
modo que tampoco quedan márgenes raros en el primer o el último trozo.

La vista previa manda **una sola tira animada** con la palabra completa (sin
huecos entre trozos), codificada en **MP4 H.264** para que Telegram la
reproduzca dentro del chat (un WebM se ve como archivo o solo el primer
fotograma). Si el texto cabe en un solo emoji, la vista previa es el sticker
real con transparencia. **✅ Crear pack** añade cada trozo al mismo pack. El
mensaje final los pega sin espacios para que se vea la palabra completa.

La palabra se dibuja una sola vez en un lienzo ancho y se corta en cuadrados
exactos. Todos los trozos comparten un único fondo y un único degradado: la
forma va del primer al último trozo, con las esquinas redondeadas solo en los
extremos. El dibujo se desplaza unos píxeles (según el espacio libre) para que
cada corte caiga sobre una letra y no en el hueco entre dos letras. Así el
efecto recorre toda la palabra sin reiniciarse en cada emoji.

### Emoji desde una imagen

Pulsa **🖼 Emoji desde imagen** o escribe `/emoji`:

1. Envía una foto (o un documento de imagen) y el bot la recorta al centro sin
   deformarla.
2. Elige la forma: **⭕ Círculo**, **⬜ Cuadrado** o **🔲 Redondeado** (cuadrado
   con esquinas redondeadas).
3. Elige el efecto de animación entre los 21 disponibles.
4. Ves la vista previa real como sticker, puedes cambiar forma o efecto y
   guardarlo con **✅ Crear pack**.

Efectos disponibles: brillo, latido, moneda, giro 360°, rebote, balanceo,
péndulo, vibración, arcoíris, aura, anillo, pop, **gota (onda)**, flotar,
voltear, gelatina, latido doble, órbita, ondas, destellos y neón. La gota hace
que la imagen aumente y disminuya como una onda; los efectos que crecen se
escalan para que no se corten en el borde del emoji.

### Animar el logo de un canal

En **📋 Mis canales**, abre un canal y pulsa **✨ Animar logo**. Están los mismos
21 efectos (brillo, latido, moneda, giro 360°, rebote, balanceo, péndulo,
vibración, arcoíris, aura, anillo, pop, gota, flotar, voltear, gelatina, latido
doble, órbita, ondas, destellos y neón). Cada uno se previsualiza con el logo
real y se guarda en un pack animado aparte (`<canal>_anim_by_<bot>`), sin tocar
el pack estático. Si cambias la foto del canal, el pack animado se regenera con
el mismo efecto.

## Requisitos

- Docker (o Python 3.9+ si prefieres ejecutarlo a mano)
- **ffmpeg** y una fuente TTF (la imagen Docker ya los incluye; sin Docker:
  `apt install ffmpeg fonts-dejavu-core`)
- Un bot de [@BotFather](https://t.me/BotFather)
- El bot debe ser **administrador del canal** (así puede leer la foto, incluso en
  canales privados)
- Para *usar* el emoji hace falta **Telegram Premium**. El pack se crea y se ve
  sin Premium, pero el custom emoji no se puede enviar sin suscripción.

## Sin Docker

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python bot.py
```

## Cómo se actualiza solo

- Al cambiar la foto, Telegram manda un update `channel_post` con `new_chat_photo`
  y el bot reconstruye el emoji al instante (el bot debe seguir siendo admin para
  recibir posts del canal).
- Como respaldo, cada `POLL_INTERVAL_SECONDS` (300 por defecto) se consulta
  `getChat` y se compara `big_file_unique_id`. Si cambió, se reemplaza el sticker.
- El sticker viejo se borra del pack, así el pack se queda con un solo emoji y no
  gasta el límite de 200.

## Detalles técnicos

- Recorte: cuadrado centrado (sin deformar) → 100×100 con LANCZOS → máscara
  con la forma elegida (círculo, cuadrado o cuadrado redondeado) dibujada a 4× y
  reducida (borde suave) → WEBP con alfa.
- Texto dividido: la palabra (con los emojis premium en línea) se renderiza
  una sola vez en un lienzo de `400×N`; el número `N` se calcula con el ancho
  real. Se corta en cuadrados de 400×400, de modo que un corte puede atravesar
  una letra. El fondo y el borde son una sola forma centrada en la tira, y el
  dibujo se desplaza para que los cortes caigan sobre tinta.
- Emojis premium: se leen de las entidades `custom_emoji` del mensaje y se pide
  su sticker con `getCustomEmojiStickers`. Si es animado (`.tgs`) se renderizan
  sus fotogramas con `rlottie-python`; si es vídeo (`.webm`) se decodifican con
  ffmpeg forzando el alfa de VP9. Los fotogramas se componen en el texto con
  Pillow y se reproducen una vez a lo largo del emoji generado. Si la
  animación no se puede leer, se usa la miniatura estática como respaldo.
- `createNewStickerSet` con `sticker_type="custom_emoji"` y
  `needs_repainting=False` (conserva los colores del logo en vez de repintarlo).
- Nombre del pack: `<canal>_by_<bot_username>`, requisito de Telegram.
- Estado en `data/channels.json` (canal → pack, sticker, `owner_id`).
- Animados: 84 frames a 400×400 (Pillow) reducidos a 100×100, tubería a
  `ffmpeg -c:v libvpx-vp9 -pix_fmt yuva420p -metadata:s:v:0 alpha_mode=1`;
  se prueba CRF 30→60 hasta bajar de 64 KB. Estado en `data/animated.json`.
- Efectos: 21 transformaciones por frame sobre el recorte a 400×400 (brillo,
  escala, rotación, HSV, moneda 3D simulada, anillo, ondas expansivas, órbitas,
  destellos, etc.). El pack del canal guarda `anim_set_name`, `anim_effect` y el
  último sticker para reemplazarlo y mantener un solo emoji.
- Alfa: se codifica VP9 con `yuva420p` y `alpha_mode=1`. El decodificador VP9
  nativo de ffmpeg no muestra el alfa; para comprobarlo hay que forzar
  `-c:v libvpx-vp9` al leer el WebM.

## Estructura

```
bot.py         # panel, wizard de emojis animados, creación/actualización de packs, polling
converter.py   # imagen → forma (círculo/cuadrado/redondeado) 100×100 WEBP
               # CLI: python converter.py in.jpg out.webp (recorte circular)
animated.py    # texto/degradados y 21 efectos → 100×100 WebM VP9
               # CLI texto:  python animated.py "HOLA" -o emoji.webm
               # (si el texto ocupa varios emojis: emoji_1.webm, emoji_2.webm…)
               # CLI logo:   python animated.py --logo logo.jpg --effect coin -o emoji.webm
store.py       # persistencia JSON
Dockerfile / docker-compose.yml
```

## Límites conocidos

- Sin Premium no se puede enviar el emoji (sí verlo).
- En emojis animados, más de 4–5 caracteres deja de leerse a 100×100; el texto
  largo se reparte en varios emojis, pero conviene usar 1–3 caracteres por
  emoji de pack.
- Los emojis premium animados (`.tgs`/`.webm`) se reproducen dentro del emoji
  generado; si su formato no se puede decodificar, se usa su primer fotograma.
- Un bot no puede poner la foto ni el emoji status de un canal; esto solo
  mantiene un pack actualizado con el logo.
- Cambiar `needs_repainting=False` a `True` haría que el logo se pinte del color
  del texto (queda monocromo).
