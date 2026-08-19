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

notion = Client(auth=NOTION_TOKEN)


def get_data_source_id():
    # Notion's API separates a "database" from its "data source" (a database
    # can have multiple sources). Queries/creates go through the data source.
    database = notion.databases.retrieve(database_id=NOTION_DATABASE_ID)
    return database["data_sources"][0]["id"]


def fetch_planner_items():
    url = f"https://{CANVAS_DOMAIN}/api/v1/planner/items"
    headers = {"Authorization": f"Bearer {CANVAS_TOKEN}"}
    params = {"per_page": 50}
    items = []
    while url:
        resp = requests.get(url, headers=headers, params=params)
        resp.raise_for_status()
        items.extend(resp.json())
        # Canvas pagina con un header Link estilo GitHub
        url = resp.links.get("next", {}).get("url")
        params = None
    return items


def find_existing_page(data_source_id, canvas_id):
    result = notion.data_sources.query(
        data_source_id=data_source_id,
        filter={"property": "Canvas ID", "rich_text": {"equals": str(canvas_id)}},
    )
    return result["results"][0] if result["results"] else None


def build_canvas_link(html_url):
    # La Planner API a veces devuelve una ruta relativa (ej. "/courses/297/assignments/2447")
    # en vez de una URL absoluta. Sin el dominio, Notion no la reconoce como link clickeable.
    if not html_url:
        return None
    if html_url.startswith("http://") or html_url.startswith("https://"):
        return html_url
    return f"https://{CANVAS_DOMAIN}{html_url}"


def upsert_item(data_source_id, item):
    plannable = item.get("plannable") or {}
    canvas_id = f'{item["plannable_type"]}-{item["plannable_id"]}'

    properties = {
        "Name": {"title": [{"text": {"content": plannable.get("title", "Sin título")}}]},
        "Type": {"select": {"name": item["plannable_type"].replace("_", " ").title()}},
        "Course": {"select": {"name": item.get("context_name") or "General"}},
        "Canvas ID": {"rich_text": [{"text": {"content": canvas_id}}]},
        "Canvas Link": {"url": build_canvas_link(item.get("html_url"))},
    }
    if item.get("plannable_date"):
        properties["Due Date"] = {"date": {"start": item["plannable_date"]}}

    existing = find_existing_page(data_source_id, canvas_id)
    if existing:
        notion.pages.update(page_id=existing["id"], properties=properties)
        print(f"Actualizado: {properties['Name']['title'][0]['text']['content']}")
    else:
        notion.pages.create(
            parent={"type": "data_source_id", "data_source_id": data_source_id},
            properties=properties,
        )
        print(f"Creado: {properties['Name']['title'][0]['text']['content']}")


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
    items = fetch_planner_items()
    print(f"{len(items)} items encontrados en Canvas Planner")
    for item in items:
        upsert_item(data_source_id, item)
    archive_stale_items(data_source_id)


if __name__ == "__main__":
    main()
