from __future__ import annotations

import argparse
import json
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


APP_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = APP_DIR.parent / "dane-z-PIM"


def strip_html(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"</p\s*>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def first_value(attr: dict, attr_def: dict | None = None) -> object:
    if attr.get("varcharValue") not in (None, ""):
        return attr["varcharValue"]
    if attr.get("TextValue") not in (None, ""):
        return strip_html(str(attr["TextValue"]))
    if attr.get("NumberValue") is not None:
        return attr["NumberValue"]
    if attr.get("IntValue") is not None:
        option = option_name(attr_def, attr.get("IntValue"))
        if option:
            return option
        if attr.get("IntValue2") is not None:
            return f"{attr.get('IntValue')} ({attr.get('IntValue2')})"
        return attr["IntValue"]
    if attr.get("BooleanValue") is True:
        return True
    return None


def option_name(attr_def: dict | None, option_id: object) -> str | None:
    if not attr_def:
        return None
    for option in attr_def.get("AttributeOptions") or []:
        if option.get("Id") == option_id:
            return option.get("OptionName")
    return None


def attribute_maps(attributes_payload: dict) -> tuple[dict[int, dict], dict[int, dict[int, str]]]:
    attrs = {int(item["Id"]): item for item in attributes_payload.get("attributes", [])}
    options: dict[int, dict[int, str]] = {}
    for attr in attrs.values():
        options[int(attr["Id"])] = {
            int(option["Id"]): str(option.get("OptionName") or option.get("OptionValue") or option["Id"])
            for option in attr.get("AttributeOptions") or []
        }
    return attrs, options


def attr_label(attr_def: dict | None, attr_id: int) -> str:
    if not attr_def:
        return f"Attribute {attr_id}"
    return str(attr_def.get("DispName") or attr_def.get("AttributeName") or attr_id)


def product_name(product: dict, attrs: dict[int, dict]) -> str:
    for attr in latest_attrs(product):
        if attr.get("AttributeId") == 225:
            return str(first_value(attr, attrs.get(225)) or f"Produkt {product.get('Id')}")
    return f"Produkt {product.get('Id')}"


def element_name(element: dict, attrs: dict[int, dict]) -> str:
    for attr in latest_attrs(element):
        if attr.get("AttributeId") == 280:
            return str(first_value(attr, attrs.get(280)) or f"System {element.get('Id')}")
    return f"System {element.get('Id')}"


def latest_attrs(record: dict) -> list[dict]:
    versions = record.get("dataVersions") or record.get("DataVersions") or []
    if not versions:
        return []
    return versions[-1].get("productAttributes") or versions[-1].get("ProductAttributes") or []


def values_for_parent(all_attrs: list[dict], parent_id: int, attr_defs: dict[int, dict]) -> list[dict]:
    values = []
    for attr in all_attrs:
        if int(attr.get("ParentAttributeId") or 0) != parent_id:
            continue
        attr_id = int(attr.get("AttributeId") or 0)
        value = first_value(attr, attr_defs.get(attr_id))
        if value in (None, "", False):
            continue
        values.append(
            {
                "attribute_id": attr_id,
                "label": attr_label(attr_defs.get(attr_id), attr_id),
                "attribute_name": (attr_defs.get(attr_id) or {}).get("AttributeName"),
                "value": value,
                "row": attr.get("RowI") or 0,
                "hash": attr.get("hash"),
                "parent_hash": attr.get("parentHash"),
            }
        )
    return values


def rows_for_parent(all_attrs: list[dict], parent_id: int, attr_defs: dict[int, dict]) -> list[dict]:
    grouped: dict[tuple[int, str], list[dict]] = {}
    for item in values_for_parent(all_attrs, parent_id, attr_defs):
        key = (int(item["row"] or 0), str(item.get("hash") or ""))
        grouped.setdefault(key, []).append(item)
    rows = []
    for (row_index, row_hash), items in sorted(grouped.items()):
        rows.append({"row": row_index, "hash": row_hash, "values": items})
    return rows


class PimData:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.product_attr_defs, _ = attribute_maps(load_json(data_dir / "productsAttributes.json"))
        self.element_attr_defs, _ = attribute_maps(load_json(data_dir / "buildingsElementsAttributes.json"))
        self.products = load_json(data_dir / "products.json").get("products", [])
        self.elements = load_json(data_dir / "building_elements.json").get("buildingElements", [])
        self.product_index = {int(product["Id"]): product for product in self.products}
        self.element_index = {int(element["Id"]): element for element in self.elements}

    def summary(self) -> dict:
        return {
            "data_dir": str(self.data_dir),
            "products": len(self.products),
            "systems": len(self.elements),
            "product_attributes": len(self.product_attr_defs),
            "system_attributes": len(self.element_attr_defs),
        }

    def list_products(self, query: str = "", category: str = "") -> dict:
        query = query.lower().strip()
        category = category.lower().strip()
        items = []
        categories = set()
        for product in self.products:
            detail = self.product_detail(int(product["Id"]), compact=True)
            haystack = " ".join(str(value) for value in [detail["name"], detail.get("unit"), *detail.get("categories", [])]).lower()
            for item in detail.get("categories", []):
                categories.add(str(item))
            if query and query not in haystack:
                continue
            if category and category not in " ".join(detail.get("categories", [])).lower():
                continue
            items.append(detail)
        return {"items": items, "categories": sorted(categories)}

    def product_detail(self, product_id: int, compact: bool = False) -> dict:
        product = self.product_index[product_id]
        attrs = latest_attrs(product)
        root_values = values_for_parent(attrs, 0, self.product_attr_defs)
        categories = [str(item["value"]) for item in root_values if item["attribute_id"] == 230]
        unit = next((item["value"] for item in root_values if item["attribute_id"] == 231), "")
        result = {
            "id": product_id,
            "name": product_name(product, self.product_attr_defs),
            "unit": unit,
            "categories": categories,
            "attribute_count": len(attrs),
        }
        if compact:
            return result
        result.update(
            {
                "general": root_values,
                "custom_attributes": rows_for_parent(attrs, 232, self.product_attr_defs),
                "product_information": rows_for_parent(attrs, 233, self.product_attr_defs),
                "packages": rows_for_parent(attrs, 234, self.product_attr_defs),
                "palettes": rows_for_parent(attrs, 235, self.product_attr_defs),
                "variants": rows_for_parent(attrs, 236, self.product_attr_defs),
                "documents": rows_for_parent(attrs, 237, self.product_attr_defs),
                "sot": rows_for_parent(attrs, 276, self.product_attr_defs),
            }
        )
        return result

    def list_systems(self, query: str = "") -> dict:
        query = query.lower().strip()
        items = []
        for element in self.elements:
            detail = self.system_detail(int(element["Id"]), compact=True)
            haystack = " ".join(str(value) for value in detail.values()).lower()
            if query and query not in haystack:
                continue
            items.append(detail)
        return {"items": items}

    def system_detail(self, element_id: int, compact: bool = False) -> dict:
        element = self.element_index[element_id]
        attrs = latest_attrs(element)
        root_values = values_for_parent(attrs, 0, self.element_attr_defs)
        result = {
            "id": element_id,
            "name": element_name(element, self.element_attr_defs),
            "attribute_count": len(attrs),
            "type": next((item["value"] for item in root_values if item["attribute_id"] == 281), ""),
            "insulation": next((item["value"] for item in root_values if item["attribute_id"] == 292), ""),
            "bim_type": next((item["value"] for item in root_values if item["attribute_id"] == 299), ""),
        }
        if compact:
            return result
        result.update(
            {
                "general": root_values,
                "variants": rows_for_parent(attrs, 283, self.element_attr_defs),
                "layers": rows_for_parent(attrs, 285, self.element_attr_defs),
                "available_products": rows_for_parent(attrs, 289, self.element_attr_defs),
                "files": (element.get("dataVersions") or [{}])[-1].get("filesAttributes", []),
            }
        )
        return result


def make_handler(data: PimData):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(APP_DIR / "static"), **kwargs)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                self.handle_api(parsed)
                return
            if parsed.path == "/":
                self.path = "/index.html"
            super().do_GET()

        def handle_api(self, parsed):
            qs = parse_qs(parsed.query)
            query = qs.get("q", [""])[0]
            try:
                if parsed.path == "/api/summary":
                    payload = data.summary()
                elif parsed.path == "/api/products":
                    payload = data.list_products(query=query, category=qs.get("category", [""])[0])
                elif parsed.path.startswith("/api/products/"):
                    payload = data.product_detail(int(unquote(parsed.path.rsplit("/", 1)[-1])))
                elif parsed.path == "/api/systems":
                    payload = data.list_systems(query=query)
                elif parsed.path.startswith("/api/systems/"):
                    payload = data.system_detail(int(unquote(parsed.path.rsplit("/", 1)[-1])))
                else:
                    self.send_json({"error": "Not found"}, status=404)
                    return
                self.send_json(payload)
            except (KeyError, ValueError):
                self.send_json({"error": "Record not found"}, status=404)

        def send_json(self, payload: dict, status: int = 200):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Local browser for PIM product and system data.")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="Folder with PIM JSON files.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    data = PimData(data_dir)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(data))
    print(f"DB Data Browser: http://{args.host}:{args.port}")
    print(f"Data source: {data_dir}")
    server.serve_forever()


if __name__ == "__main__":
    main()
