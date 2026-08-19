import os
from datetime import datetime, timedelta, timezone

import requests
from notion_client import Client

CANVAS_DOMAIN = os.environ["CANVAS_DOMAIN"]  # ej. tuescuela.instructure.com
CANVAS_TOKEN = os.environ["CANVAS_TOKEN"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]

# Tareas vencidas y sin completar se archivan solas pasado este tiempo, para
# que no se acumulen indefinidamente. Configurable vía secret opcional.
ARCHIVE_OVERDUE_AFTER_DAYS = int(os.environ.get("ARCHIVE_OVERDUE_AFTER_DAYS", "30"))

# Cuántos días atrás de anuncios traer en cada corrida. Los anuncios no
# cambian una vez posteados, así que no hace falta mirar más atrás de esto.
ANNOUNCEMENTS_LOOKBACK_DAYS = int(os.environ.get("ANNOUNCEMENTS_LOOKBACK_DAYS", "30"))

CANVAS_HEADERS = {"Authorization": f"Bearer {CANVAS_TOKEN}"}

# Tipos de item de módulo que son recursos de contenido (material subido por
# el profesor). El resto (Assignment, Quiz, Discussion, SubHeader) ya llega
# por el planner o no tiene contenido propio, así que se ignoran acá.
MODULE_RESOURCE_TYPE_LABELS = {
    "File": "File",
    "Page": "Page",
    "ExternalUrl": "External Url",
    "ExternalTool": "External Tool",
}

notion = Client(auth=NOTION_TOKEN)


def get_data_source_id():
    # Notion's API separates a "database" from its "data source" (a database
    # can have multiple sources). Queries/creates go through the data source.
    database = notion.databases.retrieve(database_id=NOTION_DATABASE_ID)
    return database["data_sources"][0]["id"]


def canvas_get_paginated(url, params=None):
    items = []
    while url:
        resp = requests.get(url, headers=CANVAS_HEADERS, params=params)
        resp.raise_for_status()
        items.extend(resp.json())
        # Canvas pagina con un header Link estilo GitHub
        url = resp.links.get("next", {}).get("url")
        params = None
    return items


def fetch_planner_items():
    url = f"https://{CANVAS_DOMAIN}/api/v1/planner/items"
    return canvas_get_paginated(url, {"per_page": 50})


def fetch_active_courses():
    url = f"https://{CANVAS_DOMAIN}/api/v1/courses"
    return canvas_get_paginated(url, {"enrollment_state": "active", "per_page": 50})


def fetch_announcements(course_ids):
    if not course_ids:
        return []
    url = f"https://{CANVAS_DOMAIN}/api/v1/announcements"
    start_date = (
        datetime.now(timezone.utc).date() - timedelta(days=ANNOUNCEMENTS_LOOKBACK_DAYS)
    ).isoformat()
    params = {
        "context_codes[]": [f"course_{course_id}" for course_id in course_ids],
        "start_date": start_date,
        "per_page": 50,
    }
    return canvas_get_paginated(url, params)


def fetch_module_resources(course_ids):
    resources = []
    for course_id in course_ids:
        modules_url = f"https://{CANVAS_DOMAIN}/api/v1/courses/{course_id}/modules"
        modules = canvas_get_paginated(modules_url, {"per_page": 50})
        for module in modules:
            items_url = (
                f"https://{CANVAS_DOMAIN}/api/v1/courses/{course_id}"
                f"/modules/{module['id']}/items"
            )
            items = canvas_get_paginated(items_url, {"per_page": 50})
            for item in items:
                if item.get("type") in MODULE_RESOURCE_TYPE_LABELS:
                    resources.append((course_id, item))
    return resources


def fetch_assignment_grades(course_ids):
    # notas de actividades individuales, vistas desde la propia entrega del
    # estudiante (no requiere acceso de profesor/gradebook).
    grades = {}
    for course_id in course_ids:
        url = f"https://{CANVAS_DOMAIN}/api/v1/courses/{course_id}/assignments"
        assignments = canvas_get_paginated(url, {"per_page": 50, "include[]": "submission"})
        for assignment in assignments:
            submission = assignment.get("submission") or {}
            if submission.get("score") is not None:
                grades[assignment["id"]] = {
                    "score": submission["score"],
                    "points_possible": assignment.get("points_possible"),
                    "letter": submission.get("grade"),
                }
    return grades


def fetch_course_grades():
    # nota general de cada materia, vía las propias inscripciones del estudiante.
    url = f"https://{CANVAS_DOMAIN}/api/v1/users/self/enrollments"
    params = {"per_page": 50, "state[]": "active", "type[]": "StudentEnrollment"}
    enrollments = canvas_get_paginated(url, params)
    return {enrollment["course_id"]: (enrollment.get("grades") or {}) for enrollment in enrollments}


def format_grade(score, points_possible, letter):
    text = f"{score}/{points_possible}" if points_possible else str(score)
    if letter and str(letter) != str(score):
        text += f" ({letter})"
    return text


def find_existing_page(data_source_id, canvas_id):
    result = notion.data_sources.query(
        data_source_id=data_source_id,
        filter={"property": "Canvas ID", "rich_text": {"equals": str(canvas_id)}},
    )
    return result["results"][0] if result["results"] else None


def get_notion_status(page):
    status = (page.get("properties", {}).get("Status") or {}).get("status")
    return status["name"] if status else None


def build_canvas_link(html_url):
    # La Planner API a veces devuelve una ruta relativa (ej. "/courses/297/assignments/2447")
    # en vez de una URL absoluta. Sin el dominio, Notion no la reconoce como link clickeable.
    if not html_url:
        return None
    if html_url.startswith("http://") or html_url.startswith("https://"):
        return html_url
    return f"https://{CANVAS_DOMAIN}{html_url}"


def mark_canvas_complete(item):
    # Refleja un "Done" puesto en Notion de vuelta a Canvas, usando el mismo
    # mecanismo que el checkbox de "marcar como hecho" en el To-Do de Canvas.
    # Nunca toca la entrega/calificación real, solo este flag del planner.
    override = item.get("planner_override") or {}
    payload = {
        "plannable_type": item["plannable_type"],
        "plannable_id": item["plannable_id"],
        "marked_complete": True,
    }
    if override.get("id"):
        url = f"https://{CANVAS_DOMAIN}/api/v1/planner/overrides/{override['id']}"
        resp = requests.put(url, headers=CANVAS_HEADERS, json=payload)
    else:
        url = f"https://{CANVAS_DOMAIN}/api/v1/planner/overrides"
        resp = requests.post(url, headers=CANVAS_HEADERS, json=payload)
    resp.raise_for_status()


def upsert_item(data_source_id, item, assignment_grades):
    plannable = item.get("plannable") or {}
    canvas_id = f'{item["plannable_type"]}-{item["plannable_id"]}'
    canvas_complete = bool((item.get("planner_override") or {}).get("marked_complete"))

    properties = {
        "Name": {"title": [{"text": {"content": plannable.get("title", "Sin título")}}]},
        "Type": {"select": {"name": item["plannable_type"].replace("_", " ").title()}},
        "Course": {"select": {"name": item.get("context_name") or "General"}},
        "Canvas ID": {"rich_text": [{"text": {"content": canvas_id}}]},
        "Canvas Link": {"url": build_canvas_link(item.get("html_url"))},
    }
    if item.get("plannable_date"):
        properties["Due Date"] = {"date": {"start": item["plannable_date"]}}

    if item["plannable_type"] == "assignment":
        grade = assignment_grades.get(item["plannable_id"])
        if grade:
            properties["Grade"] = {
                "rich_text": [
                    {
                        "text": {
                            "content": format_grade(
                                grade["score"], grade["points_possible"], grade["letter"]
                            )
                        }
                    }
                ]
            }

    existing = find_existing_page(data_source_id, canvas_id)
    name = properties["Name"]["title"][0]["text"]["content"]

    if existing:
        notion_done = get_notion_status(existing) == "Done"
        if notion_done and not canvas_complete:
            # Notion -> Canvas: el estudiante lo marcó "Done" en Notion.
            mark_canvas_complete(item)
        elif canvas_complete and not notion_done:
            # Canvas -> Notion: se marcó como hecho desde el To-Do de Canvas.
            properties["Status"] = {"status": {"name": "Done"}}
        notion.pages.update(page_id=existing["id"], properties=properties)
        print(f"Actualizado: {name}")
    else:
        if canvas_complete:
            properties["Status"] = {"status": {"name": "Done"}}
        notion.pages.create(
            parent={"type": "data_source_id", "data_source_id": data_source_id},
            properties=properties,
        )
        print(f"Creado: {name}")


def upsert_announcement(data_source_id, announcement, course_names):
    canvas_id = f'announcement-{announcement["id"]}'
    context_code = announcement.get("context_code", "")
    course_id = int(context_code.split("_", 1)[1]) if context_code.startswith("course_") else None

    properties = {
        "Name": {"title": [{"text": {"content": announcement.get("title", "Sin título")}}]},
        "Type": {"select": {"name": "Announcement"}},
        "Course": {"select": {"name": course_names.get(course_id, "General")}},
        "Canvas ID": {"rich_text": [{"text": {"content": canvas_id}}]},
        "Canvas Link": {"url": build_canvas_link(announcement.get("html_url"))},
    }
    if announcement.get("posted_at"):
        properties["Due Date"] = {"date": {"start": announcement["posted_at"]}}

    existing = find_existing_page(data_source_id, canvas_id)
    name = properties["Name"]["title"][0]["text"]["content"]

    if existing:
        notion.pages.update(page_id=existing["id"], properties=properties)
        print(f"Actualizado (anuncio): {name}")
    else:
        notion.pages.create(
            parent={"type": "data_source_id", "data_source_id": data_source_id},
            properties=properties,
        )
        print(f"Creado (anuncio): {name}")


def upsert_module_resource(data_source_id, course_id, item, course_names):
    canvas_id = f'module_item-{item["id"]}'
    link = item.get("html_url") or item.get("external_url")

    properties = {
        "Name": {"title": [{"text": {"content": item.get("title", "Sin título")}}]},
        "Type": {"select": {"name": MODULE_RESOURCE_TYPE_LABELS.get(item.get("type"), "Resource")}},
        "Course": {"select": {"name": course_names.get(course_id, "General")}},
        "Canvas ID": {"rich_text": [{"text": {"content": canvas_id}}]},
        "Canvas Link": {"url": build_canvas_link(link)},
    }

    existing = find_existing_page(data_source_id, canvas_id)
    name = properties["Name"]["title"][0]["text"]["content"]

    if existing:
        notion.pages.update(page_id=existing["id"], properties=properties)
        print(f"Actualizado (recurso): {name}")
    else:
        notion.pages.create(
            parent={"type": "data_source_id", "data_source_id": data_source_id},
            properties=properties,
        )
        print(f"Creado (recurso): {name}")


def upsert_course_grade(data_source_id, course_id, course_name, course_grades):
    grade = course_grades.get(course_id) or {}
    score = grade.get("current_score")
    letter = grade.get("current_grade")
    if score is None and letter is None:
        return  # curso sin notas cargadas todavía

    text = f"{score}%" if score is not None else ""
    if letter:
        text = f"{text} ({letter})" if text else letter

    canvas_id = f"course_grade-{course_id}"
    properties = {
        "Name": {"title": [{"text": {"content": f"Nota general: {course_name}"}}]},
        "Type": {"select": {"name": "Course Grade"}},
        "Course": {"select": {"name": course_name}},
        "Canvas ID": {"rich_text": [{"text": {"content": canvas_id}}]},
        "Canvas Link": {"url": build_canvas_link(grade.get("html_url"))},
        "Grade": {"rich_text": [{"text": {"content": text}}]},
    }

    existing = find_existing_page(data_source_id, canvas_id)

    if existing:
        notion.pages.update(page_id=existing["id"], properties=properties)
        print(f"Actualizado (nota general): {course_name}")
    else:
        notion.pages.create(
            parent={"type": "data_source_id", "data_source_id": data_source_id},
            properties=properties,
        )
        print(f"Creado (nota general): {course_name}")


def archive_stale_items(data_source_id):
    today = datetime.now(timezone.utc).date().isoformat()
    grace_cutoff = (
        datetime.now(timezone.utc).date() - timedelta(days=ARCHIVE_OVERDUE_AFTER_DAYS)
    ).isoformat()

    # Se archiva lo vencido y ya hecho de inmediato, y lo vencido sin hacer
    # después del periodo de gracia (para no perder de vista lo no entregado
    # demasiado pronto, pero tampoco acumularlo para siempre).
    stale_filter = {
        "or": [
            {
                "and": [
                    {"property": "Due Date", "date": {"before": today}},
                    {"property": "Status", "status": {"equals": "Done"}},
                ]
            },
            {"property": "Due Date", "date": {"before": grace_cutoff}},
        ]
    }

    archived_count = 0
    cursor = None
    while True:
        kwargs = {"data_source_id": data_source_id, "filter": stale_filter}
        if cursor:
            kwargs["start_cursor"] = cursor
        result = notion.data_sources.query(**kwargs)
        for page in result["results"]:
            notion.pages.update(page_id=page["id"], archived=True)
            archived_count += 1
        if not result.get("has_more"):
            break
        cursor = result.get("next_cursor")

    print(f"{archived_count} items archivados (vencidos)")


def main():
    data_source_id = get_data_source_id()

    courses = fetch_active_courses()
    course_names = {course["id"]: course.get("name", "General") for course in courses}
    course_ids = list(course_names.keys())

    assignment_grades = fetch_assignment_grades(course_ids)
    course_grades = fetch_course_grades()

    items = fetch_planner_items()
    print(f"{len(items)} items encontrados en Canvas Planner")
    for item in items:
        upsert_item(data_source_id, item, assignment_grades)

    announcements = fetch_announcements(course_ids)
    print(f"{len(announcements)} anuncios encontrados en cursos activos")
    for announcement in announcements:
        upsert_announcement(data_source_id, announcement, course_names)

    resources = fetch_module_resources(course_ids)
    print(f"{len(resources)} recursos de módulos encontrados")
    for course_id, item in resources:
        upsert_module_resource(data_source_id, course_id, item, course_names)

    for course_id, course_name in course_names.items():
        upsert_course_grade(data_source_id, course_id, course_name, course_grades)

    archive_stale_items(data_source_id)


if __name__ == "__main__":
    main()
