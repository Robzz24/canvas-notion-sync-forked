# canvas-notion-sync

Sincroniza tareas, quizzes, discusiones y eventos de Canvas Instructure hacia una base de datos de Notion, corriendo automáticamente cada 3 horas vía GitHub Actions.

Guía completa: ver el post "Canvas + Notion: guía de sincronización" en Notion.

## Setup

1. **Fork / clona este repo.**
2. En GitHub, ve a **Settings → Secrets and variables → Actions** y agrega estos 4 secrets:
   - `CANVAS_DOMAIN` — ej. `tuescuela.instructure.com` (sin `https://`)
   - `CANVAS_TOKEN` — tu Canvas Access Token (Account → Settings → New Access Token)
   - `NOTION_TOKEN` — el Internal Integration Secret de tu integración de Notion
   - `NOTION_DATABASE_ID` — el ID de tu base de datos "Canvas Sync" en Notion
   - `ARCHIVE_OVERDUE_AFTER_DAYS` (opcional) — días de gracia antes de archivar tareas vencidas sin completar. Default: `30`.
3. Confirma que la base de datos de Notion tenga estas propiedades: `Name` (title), `Course` (select), `Type` (select), `Due Date` (date), `Status` (status), `Canvas Link` (url), `Canvas ID` (rich text).
4. Confirma que tu integración de Notion esté conectada a esa base de datos (`···` → Connections).
5. Ve a la pestaña **Actions** del repo y corre el workflow "Sync Canvas to Notion" manualmente (`Run workflow`) para probarlo.

## Correr localmente (opcional)

```bash
pip install -r requirements.txt
export CANVAS_DOMAIN="tuescuela.instructure.com"
export CANVAS_TOKEN="..."
export NOTION_TOKEN="..."
export NOTION_DATABASE_ID="..."
python sync.py
```

## Cómo funciona

- Llama a `GET /api/v1/planner/items` de Canvas, que agrupa tareas, quizzes, discusiones con fecha y eventos de calendario en una sola respuesta paginada.
- Por cada item, hace upsert en Notion usando `Canvas ID` (`plannable_type-plannable_id`) como llave para evitar duplicados en corridas repetidas.
- Al final de cada corrida, archiva (Notion `archived: true`, recuperable desde la papelera) las tareas vencidas: de inmediato si ya están en `Done`, o después de `ARCHIVE_OVERDUE_AFTER_DAYS` días si nunca se marcaron como completadas.
- Es de un solo sentido: Canvas → Notion. Notion nunca escribe de vuelta a Canvas.
