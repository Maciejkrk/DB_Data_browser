from __future__ import annotations

import argparse
import cgi
import json
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


APP_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = APP_DIR / "data"
REQUIRED_FILES = [
    "productsModels.json",
    "productsAttributes.json",
    "products.json",
    "buildingsElementsModels.json",
    "buildingsElementsAttributes.json",
    "building_elements.json",
    "colors.json",
    "colorParameters.json",
    "colorGroups.json",
    "colorGroupParameters.json",
]
CORE_FILES = [
    "productsAttributes.json",
    "products.json",
    "buildingsElementsAttributes.json",
    "building_elements.json",
]


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
        attr_def = attr_defs.get(attr_id)
        if (attr_def or {}).get("AttributeType") == "Checkboxes" and attr.get("BooleanValue") is not True:
            continue
        value = first_value(attr, attr_def)
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
                "raw": {
                    "varcharValue": attr.get("varcharValue"),
                    "TextValue": attr.get("TextValue"),
                    "IntValue": attr.get("IntValue"),
                    "IntValue2": attr.get("IntValue2"),
                    "NumberValue": attr.get("NumberValue"),
                    "BooleanValue": attr.get("BooleanValue"),
                },
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


def row_map(row: dict) -> dict[str, dict]:
    return {str(item.get("attribute_name") or item.get("attribute_id")): item for item in row.get("values") or []}


def field_value(row: dict, key: str, default: object = "") -> object:
    item = row_map(row).get(key)
    return item.get("value") if item else default


def file_media(version: dict) -> list[dict]:
    media = []
    for item in version.get("filesAttributes") or []:
        url = item.get("fileUrl") or ""
        name = item.get("uploadedfileName") or item.get("fileName") or Path(url).name
        ext = str(name or url).rsplit(".", 1)[-1].lower() if "." in str(name or url) else ""
        media.append(
            {
                "name": name or "plik",
                "url": url,
                "kind": "image" if ext in {"jpg", "jpeg", "png", "webp", "gif"} else "file",
                "attribute_id": item.get("AttributeId"),
                "row": item.get("RowI") or 0,
            }
        )
    return media


def rows_as_table(rows: list[dict]) -> dict:
    columns: dict[str, str] = {}
    table_rows = []
    for row in rows:
        values = {}
        for item in row.get("values") or []:
            key = str(item.get("attribute_name") or item.get("attribute_id"))
            columns.setdefault(key, str(item.get("label") or key))
            values[key] = item.get("value")
        table_rows.append({"row": row.get("row"), "hash": row.get("hash"), "values": values})
    return {
        "columns": [{"key": key, "label": label} for key, label in columns.items()],
        "rows": table_rows,
    }


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
        versions = product.get("dataVersions") or product.get("DataVersions") or [{}]
        latest_version = versions[-1] if versions else {}
        attrs = latest_attrs(product)
        root_values = values_for_parent(attrs, 0, self.product_attr_defs)
        categories = [str(item["value"]) for item in root_values if item["attribute_id"] == 230]
        unit = next((item["value"] for item in root_values if item["attribute_id"] == 231), "")
        custom_rows = rows_for_parent(attrs, 232, self.product_attr_defs)
        product_info_rows = rows_for_parent(attrs, 233, self.product_attr_defs)
        package_rows = rows_for_parent(attrs, 234, self.product_attr_defs)
        palette_rows = rows_for_parent(attrs, 235, self.product_attr_defs)
        variant_rows = rows_for_parent(attrs, 236, self.product_attr_defs)
        document_rows = rows_for_parent(attrs, 237, self.product_attr_defs)
        sot_rows = rows_for_parent(attrs, 276, self.product_attr_defs)
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
                "custom_attributes": custom_rows,
                "features": self.product_features(custom_rows),
                "product_information": product_info_rows,
                "packages": package_rows,
                "package_table": rows_as_table(package_rows),
                "palettes": palette_rows,
                "palette_table": rows_as_table(palette_rows),
                "variants": variant_rows,
                "variant_table": rows_as_table(variant_rows),
                "documents": document_rows,
                "document_table": rows_as_table(document_rows),
                "sot": sot_rows,
                "sot_table": rows_as_table(sot_rows),
                "media": file_media(latest_version),
            }
        )
        return result

    def product_features(self, rows: list[dict]) -> list[dict]:
        features = []
        for row in rows:
            values = row_map(row)
            name = (values.get("attribute_name") or {}).get("value")
            value_item = values.get("value") or values.get("www_value")
            value = value_item.get("value") if value_item else ""
            prefix = (values.get("field_prefix") or {}).get("value") or ""
            suffix = (values.get("field_suffix") or {}).get("value") or ""
            if not name and not value:
                continue
            features.append(
                {
                    "name": name or f"Cecha {row.get('row')}",
                    "prefix": prefix,
                    "value": value,
                    "suffix": suffix,
                    "row": row.get("row"),
                }
            )
        return features

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
        versions = element.get("dataVersions") or [{}]
        latest_version = versions[-1] if versions else {}
        attrs = latest_attrs(element)
        root_values = values_for_parent(attrs, 0, self.element_attr_defs)
        variant_rows = rows_for_parent(attrs, 283, self.element_attr_defs)
        layer_rows = rows_for_parent(attrs, 285, self.element_attr_defs)
        available_rows = rows_for_parent(attrs, 289, self.element_attr_defs)
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
                "variants": variant_rows,
                "layers": layer_rows,
                "available_products": available_rows,
                "system_variants": self.system_variants(variant_rows, layer_rows, available_rows),
                "files": latest_version.get("filesAttributes", []),
                "media": file_media(latest_version),
            }
        )
        return result

    def system_variants(self, variant_rows: list[dict], layer_rows: list[dict], available_rows: list[dict]) -> list[dict]:
        layers_by_variant: dict[str, list[dict]] = {}
        for layer in layer_rows:
            parent_hash = str((layer.get("values") or [{}])[0].get("parent_hash") or "")
            layers_by_variant.setdefault(parent_hash, []).append(layer)

        products_by_layer: dict[str, list[dict]] = {}
        for available in available_rows:
            parent_hash = str((available.get("values") or [{}])[0].get("parent_hash") or "")
            products_by_layer.setdefault(parent_hash, []).append(available)

        variants = []
        for variant in variant_rows:
            variant_hash = str(variant.get("hash") or "")
            variant_layers = []
            for layer in sorted(layers_by_variant.get(variant_hash, []), key=lambda item: item.get("row") or 0):
                layer_hash = str(layer.get("hash") or "")
                products = []
                for available in products_by_layer.get(layer_hash, []):
                    product_item = next((item for item in available.get("values") or [] if item.get("attribute_id") == 290), None)
                    default_item = next((item for item in available.get("values") or [] if item.get("attribute_id") == 298), None)
                    raw = product_item.get("raw") if product_item else {}
                    product_id = raw.get("IntValue") if raw else None
                    product_variant = raw.get("IntValue2") if raw else None
                    linked_product = self.product_index.get(int(product_id)) if isinstance(product_id, int) else None
                    products.append(
                        {
                            "product_id": product_id,
                            "product_name": product_name(linked_product, self.product_attr_defs) if linked_product else product_item.get("value") if product_item else "",
                            "variant": product_variant,
                            "default": bool(default_item.get("value")) if default_item else False,
                        }
                    )
                variant_layers.append(
                    {
                        "row": layer.get("row"),
                        "position": field_value(layer, "layer_position"),
                        "name": field_value(layer, "layer_name"),
                        "products": products,
                    }
                )
            variants.append(
                {
                    "row": variant.get("row"),
                    "name": field_value(variant, "variant_name", f"Wariant {variant.get('row')}"),
                    "layers": variant_layers,
                }
            )
        return variants


class DataStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.data: PimData | None = None
        self.reload()

    def status(self) -> dict:
        files = []
        for filename in REQUIRED_FILES:
            path = self.data_dir / filename
            files.append(
                {
                    "name": filename,
                    "exists": path.exists(),
                    "size": path.stat().st_size if path.exists() else 0,
                    "required_for_browser": filename in CORE_FILES,
                }
            )
        missing = [item["name"] for item in files if not item["exists"]]
        missing_core = [filename for filename in CORE_FILES if not (self.data_dir / filename).exists()]
        return {
            "data_dir": str(self.data_dir),
            "ready": self.data is not None,
            "files": files,
            "missing": missing,
            "missing_core": missing_core,
        }

    def upload_files(self, uploaded_files: list[tuple[str, bytes]]) -> dict:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        saved = []
        for filename, content in uploaded_files:
            safe_name = Path(filename).name
            if safe_name not in REQUIRED_FILES:
                continue
            if not content:
                continue
            (self.data_dir / safe_name).write_bytes(content)
            saved.append(safe_name)
        self.reload()
        status = self.status()
        status["saved"] = saved
        return status

    def reload(self) -> None:
        if all((self.data_dir / filename).exists() for filename in CORE_FILES):
            self.data = PimData(self.data_dir)
        else:
            self.data = None


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


def make_store_handler(store: DataStore):
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

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path == "/api/source/upload":
                self.handle_upload()
                return
            self.send_json({"error": "Not found"}, status=404)

        def handle_api(self, parsed):
            qs = parse_qs(parsed.query)
            query = qs.get("q", [""])[0]
            try:
                if parsed.path == "/api/source/status":
                    payload = store.status()
                elif parsed.path == "/api/summary":
                    if not store.data:
                        self.send_json({"error": "Data source is not ready", **store.status()}, status=409)
                        return
                    payload = store.data.summary()
                    payload["source_status"] = store.status()
                elif parsed.path == "/api/products":
                    payload = self.ready_data().list_products(query=query, category=qs.get("category", [""])[0])
                elif parsed.path.startswith("/api/products/"):
                    payload = self.ready_data().product_detail(int(unquote(parsed.path.rsplit("/", 1)[-1])))
                elif parsed.path == "/api/systems":
                    payload = self.ready_data().list_systems(query=query)
                elif parsed.path.startswith("/api/systems/"):
                    payload = self.ready_data().system_detail(int(unquote(parsed.path.rsplit("/", 1)[-1])))
                else:
                    self.send_json({"error": "Not found"}, status=404)
                    return
                self.send_json(payload)
            except (KeyError, ValueError):
                self.send_json({"error": "Record not found"}, status=404)

        def ready_data(self) -> PimData:
            if not store.data:
                raise ValueError("Data source is not ready")
            return store.data

        def handle_upload(self):
            content_type = self.headers.get("Content-Type", "")
            if not content_type.startswith("multipart/form-data"):
                self.send_json({"error": "Expected multipart/form-data"}, status=400)
                return
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": content_type,
                    "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
                },
            )
            uploaded = []
            fields = form["files"] if "files" in form else []
            if not isinstance(fields, list):
                fields = [fields]
            for field in fields:
                if not getattr(field, "filename", None):
                    continue
                uploaded.append((field.filename, field.file.read()))
            self.send_json(store.upload_files(uploaded))

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
    store = DataStore(data_dir)
    server = ThreadingHTTPServer((args.host, args.port), make_store_handler(store))
    print(f"DB Data Browser: http://{args.host}:{args.port}")
    print(f"Data source: {data_dir}")
    server.serve_forever()


if __name__ == "__main__":
    main()
