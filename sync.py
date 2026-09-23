import os
import time
from datetime import datetime, timedelta, timezone

import requests
from notion_client import Client

CANVAS_DOMAIN = os.environ["CANVAS_DOMAIN"]
CANVAS_TOKEN = os.environ["CANVAS_TOKEN"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
ARCHIVE_OVERDUE_AFTER_DAYS = int(os.environ.get("ARCHIVE_OVERDUE_AFTER_DAYS", "30"))
ANNOUNCEMENTS_LOOKBACK_DAYS = int(os.environ.get("ANNOUNCEMENTS_LOOKBACK_DAYS", "30"))
NOTION_WRITE_DELAY = float(os.environ.get("NOTION_WRITE_DELAY", "0.4"))

CANVAS_HEADERS = {"Authorization": f"Bearer {CANVAS_TOKEN}"}
notion = Client(auth=NOTION_TOKEN)

_data_source_id = None
_database_properties = None

MODULE_RESOURCE_TYPE_LABELS = {
    "File": "File",
    "Page": "Page",
    "ExternalUrl": "External Url",
    "ExternalTool": "External Tool",
}


def get_data_source_id():
    global _data_source_id

    if _data_source_id is None:
        database = notion.databases.retrieve(database_id=NOTION_DATABASE_ID)
        _data_source_id = database["data_sources"][0]["id"]

    return _data_source_id


def get_database_properties():
    global _database_properties

    if _database_properties is None:
        data_source = notion.data_sources.retrieve(
            data_source_id=get_data_source_id()
        )
        _database_properties = data_source.get("properties", {})
        print(
            "Propiedades de Notion detectadas: "
            f"{', '.join(_database_properties) or '(ninguna)'}"
        )

    return _database_properties


def property_name(*names, property_type=None):
    props = get_database_properties()

    for name in names:
        if name in props and (
            property_type is None or props[name].get("type") == property_type
        ):
            return name

    if property_type:
        for name, definition in props.items():
            if definition.get("type") == property_type:
                return name

    return None


def select_option_name(prop, preferred):
    definition = get_database_properties().get(prop, {})
    config = definition.get("select") or definition.get("status") or {}
    options = config.get("options", [])
    names = {option.get("name") for option in options}

    if preferred in names:
        return preferred

    aliases = {
        "Done": ("Listo", "Completado", "Hecho"),
        "Assignment": ("Assignment", "Tarea"),
        "Announcement": ("Announcement", "Anuncio"),
        "Course Grade": ("Course Grade", "Nota general", "Calificación"),
    }

    for candidate in aliases.get(preferred, ()):
        if candidate in names:
            return candidate

    return None


def add_property(properties, logical_name, value, property_type=None):
    actual = property_name(logical_name, property_type=property_type)

    if not actual:
        print(f"Omitiendo propiedad no disponible: {logical_name}")
        return

    properties[actual] = value


def notion_write(operation, *args, **kwargs):
    result = operation(*args, **kwargs)
    time.sleep(NOTION_WRITE_DELAY)
    return result


def canvas_get_paginated(url, params=None):
    items = []

    while url:
        response = requests.get(
            url,
            headers=CANVAS_HEADERS,
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        items.extend(response.json())
        url = response.links.get("next", {}).get("url")
        params = None

    return items


def fetch_planner_items():
    return canvas_get_paginated(
        f"https://{CANVAS_DOMAIN}/api/v1/planner/items",
        {"per_page": 50},
    )


def fetch_active_courses():
    return canvas_get_paginated(
        f"https://{CANVAS_DOMAIN}/api/v1/courses",
        {"enrollment_state": "active", "per_page": 50},
    )


def fetch_announcements(course_ids):
    if not course_ids:
        return []

    start_date = (
        datetime.now(timezone.utc).date()
        - timedelta(days=ANNOUNCEMENTS_LOOKBACK_DAYS)
    ).isoformat()

    return canvas_get_paginated(
        f"https://{CANVAS_DOMAIN}/api/v1/announcements",
        {
            "context_codes[]": [f"course_{course_id}" for course_id in course_ids],
            "start_date": start_date,
            "per_page": 50,
        },
    )


def fetch_module_resources(course_ids):
    resources = []

    for course_id in course_ids:
        modules = canvas_get_paginated(
            f"https://{CANVAS_DOMAIN}/api/v1/courses/{course_id}/modules",
            {"per_page": 50},
        )

        for module in modules:
            items = canvas_get_paginated(
                f"https://{CANVAS_DOMAIN}/api/v1/courses/{course_id}"
                f"/modules/{module['id']}/items",
                {"per_page": 50},
            )

            resources.extend(
                (course_id, item)
                for item in items
                if item.get("type") in MODULE_RESOURCE_TYPE_LABELS
            )

    return resources


def fetch_assignment_grades(course_ids):
    grades = {}

    for course_id in course_ids:
        assignments = canvas_get_paginated(
            f"https://{CANVAS_DOMAIN}/api/v1/courses/{course_id}/assignments",
            {"per_page": 50, "include[]": "submission"},
        )

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
    enrollments = canvas_get_paginated(
        f"https://{CANVAS_DOMAIN}/api/v1/users/self/enrollments",
        {
            "per_page": 50,
            "state[]": "active",
            "type[]": "StudentEnrollment",
        },
    )

    return {
        item["course_id"]: (item.get("grades") or {})
        for item in enrollments
    }


def format_grade(score, points_possible, letter):
    text = f"{score}/{points_possible}" if points_possible else str(score)
    return f"{text} ({letter})" if letter and str(letter) != str(score) else text


def build_canvas_link(value):
    if not value:
        return None

    if value.startswith(("http://", "https://")):
        return value

    return f"https://{CANVAS_DOMAIN}{value}"


def find_existing_page(data_source_id, canvas_id):
    canvas_property = property_name("Canvas ID", property_type="rich_text")

    if not canvas_property:
        return None

    result = notion.data_sources.query(
        data_source_id=data_source_id,
        filter={
            "property": canvas_property,
            "rich_text": {"equals": str(canvas_id)},
        },
    )

    return result["results"][0] if result["results"] else None


def get_notion_status(page):
    status_property = property_name("Status", property_type="status")
    status = (
        page.get("properties", {}).get(status_property) or {}
    ).get("status")

    return status.get("name") if status else None


def status_is_done(name):
    return name in {"Done", "Listo", "Hecho", "Completado"}


def mark_canvas_complete(item):
    override = item.get("planner_override") or {}

    payload = {
        "plannable_type": item["plannable_type"],
        "plannable_id": item["plannable_id"],
        "marked_complete": True,
    }

    if override.get("id"):
        url = (
            f"https://{CANVAS_DOMAIN}/api/v1/planner/overrides/"
            f"{override['id']}"
        )
        response = requests.put(
            url,
            headers=CANVAS_HEADERS,
            json=payload,
            timeout=30,
        )
    else:
        response = requests.post(
            f"https://{CANVAS_DOMAIN}/api/v1/planner/overrides",
            headers=CANVAS_HEADERS,
            json=payload,
            timeout=30,
        )

    response.raise_for_status()


def make_properties(
    title,
    item_type,
    course,
    canvas_id,
    link=None,
    due_date=None,
    grade=None,
    done=False,
):
    properties = {}

    title_property = property_name(
        "Name",
        "Title",
        "Título",
        property_type="title",
    )
    if title_property:
        properties[title_property] = {
            "title": [{"text": {"content": title}}]
        }

    type_property = property_name("Type", "Tipo", property_type="select")
    if type_property:
        option = select_option_name(type_property, item_type)

        if option:
            properties[type_property] = {"select": {"name": option}}
        else:
            print(f"Omitiendo opción no disponible para Type: {item_type}")

    # Course permite opciones nuevas; Notion las crea automáticamente.
    course_property = property_name("Course", "Curso", property_type="select")
    if course_property:
        properties[course_property] = {"select": {"name": course}}

    add_property(
        properties,
        "Canvas ID",
        {"rich_text": [{"text": {"content": canvas_id}}]},
        "rich_text",
    )

    if link:
        add_property(
            properties,
            "Canvas Link",
            {"url": link},
            "url",
        )

    if due_date:
        add_property(
            properties,
            "Due Date",
            {"date": {"start": due_date}},
            "date",
        )

    if grade:
        add_property(
            properties,
            "Grade",
            {"rich_text": [{"text": {"content": grade}}]},
            "rich_text",
        )

    if done:
        status_property = property_name("Status", property_type="status")

        if status_property:
            option = select_option_name(status_property, "Done")

            if option:
                properties[status_property] = {"status": {"name": option}}

    return properties


def upsert_page(data_source_id, canvas_id, properties, label, item=None):
    existing = find_existing_page(data_source_id, canvas_id)

    if existing and item is not None:
        current_done = status_is_done(get_notion_status(existing))
        canvas_done = bool(
            (item.get("planner_override") or {}).get("marked_complete")
        )

        if current_done and not canvas_done:
            mark_canvas_complete(item)

    if existing:
        notion_write(
            notion.pages.update,
            page_id=existing["id"],
            properties=properties,
        )
        print(f"Actualizado: {label}")
    else:
        notion_write(
            notion.pages.create,
            parent={
                "type": "data_source_id",
                "data_source_id": data_source_id,
            },
            properties=properties,
        )
        print(f"Creado: {label}")


def upsert_item(data_source_id, item, assignment_grades):
    plannable = item.get("plannable") or {}
    title = plannable.get("title", "Sin título")
    item_type = item["plannable_type"].replace("_", " ").title()

    grade = (
        assignment_grades.get(item["plannable_id"])
        if item["plannable_type"] == "assignment"
        else None
    )
    grade_text = (
        format_grade(
            grade["score"],
            grade["points_possible"],
            grade["letter"],
        )
        if grade
        else None
    )

    canvas_id = f'{item["plannable_type"]}-{item["plannable_id"]}'

    properties = make_properties(
        title=title,
        item_type=item_type,
        course=item.get("context_name") or "General",
        canvas_id=canvas_id,
        link=build_canvas_link(item.get("html_url")),
        due_date=item.get("plannable_date"),
        grade=grade_text,
        done=bool(
            (item.get("planner_override") or {}).get("marked_complete")
        ),
    )

    upsert_page(data_source_id, canvas_id, properties, title, item)


def upsert_announcement(data_source_id, announcement, course_names):
    context = announcement.get("context_code", "")
    course_id = (
        int(context.split("_", 1)[1])
        if context.startswith("course_")
        else None
    )
    canvas_id = f'announcement-{announcement["id"]}'

    properties = make_properties(
        title=announcement.get("title", "Sin título"),
        item_type="Announcement",
        course=course_names.get(course_id, "General"),
        canvas_id=canvas_id,
        link=build_canvas_link(announcement.get("html_url")),
        due_date=announcement.get("posted_at"),
    )

    upsert_page(
        data_source_id,
        canvas_id,
        properties,
        announcement.get("title", "Sin título"),
    )


def upsert_module_resource(data_source_id, course_id, item, course_names):
    canvas_id = f'module_item-{item["id"]}'
    title = item.get("title", "Sin título")

    properties = make_properties(
        title=title,
        item_type=MODULE_RESOURCE_TYPE_LABELS[item["type"]],
        course=course_names.get(course_id, "General"),
        canvas_id=canvas_id,
        link=build_canvas_link(
            item.get("html_url") or item.get("external_url")
        ),
    )

    upsert_page(data_source_id, canvas_id, properties, title)


def upsert_course_grade(data_source_id, course_id, course_name, course_grades):
    grade = course_grades.get(course_id) or {}
    score = grade.get("current_score")
    letter = grade.get("current_grade")

    if score is None and letter is None:
        return

    text = f"{score}%" if score is not None else ""
    text = f"{text} ({letter})" if letter and text else (letter or text)
    canvas_id = f"course_grade-{course_id}"

    properties = make_properties(
        title=f"Nota general: {course_name}",
        item_type="Course Grade",
        course=course_name,
        canvas_id=canvas_id,
        link=build_canvas_link(grade.get("html_url")),
        grade=text,
    )

    upsert_page(data_source_id, canvas_id, properties, course_name)


def archive_stale_items(data_source_id):
    due_property = property_name("Due Date", "Fecha", property_type="date")
    status_property = property_name("Status", "Estado", property_type="status")

    if not due_property or not status_property:
        print("Archivado omitido: faltan las propiedades Due Date/Status.")
        return

    done_option = select_option_name(status_property, "Done")

    if not done_option:
        print("Archivado omitido: Status no tiene una opción de completado.")
        return

    today = datetime.now(timezone.utc).date().isoformat()
    cutoff = (
        datetime.now(timezone.utc).date()
        - timedelta(days=ARCHIVE_OVERDUE_AFTER_DAYS)
    ).isoformat()

    stale_filter = {
        "or": [
            {
                "and": [
                    {"property": due_property, "date": {"before": today}},
                    {
                        "property": status_property,
                        "status": {"equals": done_option},
                    },
                ]
            },
            {"property": due_property, "date": {"before": cutoff}},
        ]
    }

    cursor = None
    count = 0

    while True:
        kwargs = {
            "data_source_id": data_source_id,
            "filter": stale_filter,
        }

        if cursor:
            kwargs["start_cursor"] = cursor

        result = notion.data_sources.query(**kwargs)

        for page in result["results"]:
            notion_write(
                notion.pages.update,
                page_id=page["id"],
                archived=True,
            )
            count += 1

        if not result.get("has_more"):
            break

        cursor = result.get("next_cursor")

    print(f"{count} items archivados (vencidos)")


def main():
    data_source_id = get_data_source_id()
    courses = fetch_active_courses()
    course_names = {
        course["id"]: course.get("name", "General")
        for course in courses
    }
    course_ids = list(course_names)

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
        upsert_module_resource(
            data_source_id,
            course_id,
            item,
            course_names,
        )

    for course_id, course_name in course_names.items():
        upsert_course_grade(
            data_source_id,
            course_id,
            course_name,
            course_grades,
        )

    archive_stale_items(data_source_id)


if __name__ == "__main__":
    main()
