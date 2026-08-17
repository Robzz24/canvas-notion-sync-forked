import os

import requests
from notion_client import Client

CANVAS_DOMAIN = os.environ["CANVAS_DOMAIN"]  # ej. tuescuela.instructure.com
CANVAS_TOKEN = os.environ["CANVAS_TOKEN"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]

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


def upsert_item(data_source_id, item):
    plannable = item.get("plannable") or {}
    canvas_id = f'{item["plannable_type"]}-{item["plannable_id"]}'

    properties = {
        "Name": {"title": [{"text": {"content": plannable.get("title", "Sin título")}}]},
        "Type": {"select": {"name": item["plannable_type"].replace("_", " ").title()}},
        "Course": {"select": {"name": item.get("context_name") or "General"}},
        "Canvas ID": {"rich_text": [{"text": {"content": canvas_id}}]},
        "Canvas Link": {"url": item.get("html_url")},
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


def main():
    data_source_id = get_data_source_id()
    items = fetch_planner_items()
    print(f"{len(items)} items encontrados en Canvas Planner")
    for item in items:
        upsert_item(data_source_id, item)


if __name__ == "__main__":
    main()
