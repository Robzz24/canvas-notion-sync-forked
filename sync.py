import os

import requests
from notion_client import Client

CANVAS_DOMAIN = os.environ["CANVAS_DOMAIN"]  # ej. tuescuela.instructure.com
CANVAS_TOKEN = os.environ["CANVAS_TOKEN"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]

notion = Client(auth=NOTION_TOKEN)


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


def find_existing_page(canvas_id):
    result = notion.databases.query(
        database_id=NOTION_DATABASE_ID,
        filter={"property": "Canvas ID", "rich_text": {"equals": str(canvas_id)}},
    )
    return result["results"][0] if result["results"] else None


def upsert_item(item):
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

    existing = find_existing_page(canvas_id)
    if existing:
        notion.pages.update(page_id=existing["id"], properties=properties)
        print(f"Actualizado: {properties['Name']['title'][0]['text']['content']}")
    else:
        notion.pages.create(parent={"database_id": NOTION_DATABASE_ID}, properties=properties)
        print(f"Creado: {properties['Name']['title'][0]['text']['content']}")


def main():
    items = fetch_planner_items()
    print(f"{len(items)} items encontrados en Canvas Planner")
    for item in items:
        upsert_item(item)


if __name__ == "__main__":
    main()
