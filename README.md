# canvas-notion-sync

Sincroniza tareas, quizzes, discusiones, eventos, anuncios, recursos de módulos y notas de Canvas Instructure hacia una base de datos de Notion, corriendo automáticamente cada 3 horas vía GitHub Actions.

Guía completa: ver el post "Canvas + Notion: guía de sincronización" en Notion.

## Setup

1. **Fork / clona este repo.**
2. En GitHub, ve a **Settings → Secrets and variables → Actions** y agrega estos 4 secrets:
   - `CANVAS_DOMAIN` — ej. `tuescuela.instructure.com` (sin `https://`)
   - `CANVAS_TOKEN` — tu Canvas Access Token (Account → Settings → New Access Token)
   - `NOTION_TOKEN` — el Internal Integration Secret de tu integración de Notion
   - `NOTION_DATABASE_ID` — el ID de tu base de datos "Canvas Sync" en Notion
   - `ARCHIVE_OVERDUE_AFTER_DAYS` (opcional) — días de gracia antes de archivar tareas vencidas sin completar. Default: `30`.
   - `ANNOUNCEMENTS_LOOKBACK_DAYS` (opcional) — cuántos días atrás de anuncios traer en cada corrida. Default: `30`.
3. Confirma que la base de datos de Notion tenga estas propiedades: `Name` (title), `Course` (select), `Type` (select), `Due Date` (date), `Status` (status), `Canvas Link` (url), `Canvas ID` (rich text), `Grade` (rich text).
   - `Type` va a ir recibiendo valores nuevos automáticamente (Notion los crea solo): `Assignment`, `Quiz`, `Discussion Topic`, `Calendar Event`, `Announcement`, `File`, `Page`, `External Url`, `External Tool`, `Course Grade`.
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
- También trae los cursos activos y sus anuncios (`GET /api/v1/announcements`, últimos `ANNOUNCEMENTS_LOOKBACK_DAYS` días) y los sincroniza como items con `Type = Announcement`.
- Recorre los módulos de cada curso (`GET /api/v1/courses/:id/modules` + `.../items`) y sincroniza los recursos de contenido (archivos, páginas, enlaces/herramientas externas) con `Type = File/Page/External Url/External Tool`. Los items de módulo que son en realidad tareas/quizzes/discusiones se ignoran acá porque ya llegan por el planner.
- Trae las notas: por actividad (`GET /api/v1/courses/:id/assignments?include[]=submission`, propia entrega del estudiante) se guardan en la propiedad `Grade` del item de esa tarea; y la nota general por materia (`GET /api/v1/users/self/enrollments`) se sincroniza como una fila aparte por curso con `Type = Course Grade`, que se actualiza en el lugar en cada corrida.
- Al final de cada corrida, archiva (Notion `archived: true`, recuperable desde la papelera) las tareas vencidas: de inmediato si ya están en `Done`, o después de `ARCHIVE_OVERDUE_AFTER_DAYS` días si nunca se marcaron como completadas. Anuncios, recursos y notas generales no tienen `Due Date`, así que nunca se archivan solos.
- Sync bidireccional parcial, limitado al flag de "completado" (nunca toca entregas ni calificaciones):
  - Canvas → Notion: si el item está marcado como hecho en el To-Do de Canvas (`planner_override.marked_complete`), se pone `Status = Done` en Notion.
  - Notion → Canvas: si el estudiante pone `Status = Done` en Notion antes de que Canvas lo sepa, se crea/actualiza un `planner_override` en Canvas con `marked_complete: true` vía `POST/PUT /api/v1/planner/overrides`.
  - Fuera de ese flag, el resto de los campos siguen siendo de un solo sentido: Canvas → Notion. Las notas son de solo lectura (Canvas → Notion), nunca se escriben calificaciones de vuelta.
