# Bot de ofertas Hardgamers → Discord

Revisa Hardgamers cada 15 minutos y avisa por Discord cuando aparece:

- RAM DDR5 de 8 GB o 16 GB muy por debajo del precio de mercado
- GPUs RX 9060 XT, 9070, 9070 GRE, 9070 XT, RTX 5060 Ti y RTX 5070
- Fuentes Corsair, Thermaltake, Gigabyte o Cooler Master con descuento
- Coolers/fans de 120 o 140 mm de esas marcas + ID-Cooling con descuento
- Cualquier componente con 30% o más de descuento (50% o más sale en naranja)

## Paso 1: probarlo en tu PC (importante)

1. Instalá Python 3.10+ y corré `pip install -r requirements.txt`.
2. Corré `python bot.py --inspect`. Tiene que decir "Productos leídos: N" con N mayor a 0.
3. Corré `python bot.py --dry-run` para ver en consola qué alertas mandaría.

Si dice que no leyó ningún producto, pasame la salida de `--inspect` y ajusto el lector.

## Paso 2: webhook de Discord

Canal → Editar canal → Integraciones → Webhooks → Nuevo webhook → Copiar URL.
Para probar local: `export DISCORD_WEBHOOK_URL="tu_url"` (en Windows PowerShell: `$env:DISCORD_WEBHOOK_URL="tu_url"`) y `python bot.py`.
La URL es como una contraseña: no la subas a ningún lado.

## Paso 3: que corra solo (GitHub Actions)

1. Creá un repo en GitHub (privado o público) y subí todo el contenido de esta carpeta, incluida `.github/workflows/deals.yml`.
2. Repo → Settings → Secrets and variables → Actions → New repository secret. Nombre: `DISCORD_WEBHOOK_URL`, valor: la URL del webhook.
3. Pestaña Actions → "Ofertas Hardgamers" → Run workflow para probar. Después corre solo cada 15 minutos.

## Ajustes (config.json)

- `ram_max_price`: tope absoluto en pesos para RAM (0 o null para desactivarlo).
- `ram_max_ratio_of_median`: 0.6 = avisa si la RAM cuesta 60% o menos de la mediana.
- `gpu_max_ratio_of_median`: 0.85 = avisa si la GPU está 15% o más por debajo de la mediana de su modelo.
- `min_discount_percent`: descuento mínimo para avisar (30).
- `mention`: por ejemplo `"@here"` o `"<@&ID_DEL_ROL>"` para que Discord notifique.
- `sources`: páginas que se revisan y cuántas páginas de resultados leer de cada una (`pages`). Podés pegar acá URLs de búsquedas de Hardgamers. `text=` es el texto buscado, `sort=discount&order=-1` ordena por mayor descuento y `sort=price&order=1` por menor precio.
