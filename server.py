from __future__ import annotations

import argparse
import base64
import cgi
import csv
import io
import json
import mimetypes
import os
import re
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen


APP_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = APP_DIR / "data"
NOTES_FILE = "browser_corrections.json"
ATTACHMENTS_DIR = "correction_attachments"
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
]

FALLBACK_ATTRIBUTE_LABELS = {
    277: "Thickness",
    278: "Lambda",
    279: "Density",
    295: "μ",
}


def strip_html(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"</p\s*>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_json_if_exists(path: Path, default: dict | None = None) -> dict:
    if not path.exists():
        return default or {}
    return load_json(path)


def read_json_body(handler) -> dict:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        return {}
    return json.loads(handler.rfile.read(length).decode("utf-8"))


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def notes_as_csv(notes: list[dict]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "key",
            "record_type",
            "record_id",
            "record_name",
            "status",
            "accepted",
            "requires_correction",
            "comment",
            "attachments",
            "updated_at",
            "resolved_at",
        ],
        delimiter=";",
        lineterminator="\n",
    )
    writer.writeheader()
    for note in notes:
        writer.writerow(
            {
                "key": note.get("key", ""),
                "record_type": note.get("record_type", ""),
                "record_id": note.get("record_id", ""),
                "record_name": note.get("record_name", ""),
                "status": "resolved" if note.get("resolved") else "open",
                "accepted": "yes" if note.get("accepted") else "no",
                "requires_correction": "yes" if note.get("requires_correction") else "no",
                "comment": note.get("comment", ""),
                "attachments": ", ".join(item.get("name", "") for item in note.get("attachments", [])),
                "updated_at": note.get("updated_at", ""),
                "resolved_at": note.get("resolved_at", ""),
            }
        )
    return output.getvalue()


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


def first_parameter_value(param: dict) -> object:
    if param.get("varcharValue") not in (None, ""):
        return param["varcharValue"]
    if param.get("TextValue") not in (None, ""):
        return strip_html(str(param["TextValue"]))
    if param.get("NumberValue") is not None:
        return param["NumberValue"]
    if param.get("IntValue") is not None:
        return param["IntValue"]
    if param.get("BooleanValue") is True:
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
        return FALLBACK_ATTRIBUTE_LABELS.get(attr_id, f"Attribute {attr_id}")
    return str(attr_def.get("DispName") or attr_def.get("AttributeName") or attr_id)


def attr_is_deleted(attr_def: dict | None) -> bool:
    return bool((attr_def or {}).get("deleted") or (attr_def or {}).get("IsDeleted"))


def product_name_attribute_ids(attrs: dict[int, dict], models_payload: dict) -> list[int]:
    models = models_payload.get("models") or []
    product_model_ids = {
        int(model["Id"])
        for model in models
        if str(model.get("modelType") or model.get("ModelType") or "").lower() == "product"
    }
    candidates = []
    for attr_id, attr_def in attrs.items():
        product_model_match = not product_model_ids or attr_def.get("ProductModelId") in product_model_ids
        text = " ".join(str(attr_def.get(key) or "") for key in ("AttributeName", "DispName")).strip().lower()
        if product_model_match and text in {"nazwa nazwa", "name name", "nazwa", "name", "product name product name", "product_name name"}:
            candidates.append((0, int(attr_def.get("DisplayOrder") or 0), attr_id))
        elif product_model_match and attr_def.get("searchFlag") and ("nazwa" in text or "name" in text):
            candidates.append((1, int(attr_def.get("DisplayOrder") or 0), attr_id))
    if not candidates:
        for attr_id, attr_def in attrs.items():
            text = " ".join(str(attr_def.get(key) or "") for key in ("AttributeName", "DispName")).strip().lower()
            if text in {"nazwa nazwa", "name name", "nazwa", "name"}:
                candidates.append((2, int(attr_def.get("DisplayOrder") or 0), attr_id))
    result = [item[2] for item in sorted(candidates)]
    return result or [225]


def product_name(product: dict, attrs: dict[int, dict], name_attribute_ids: list[int] | None = None) -> str:
    name_ids = name_attribute_ids or [225]
    for attr in latest_attrs(product):
        attr_id = int(attr.get("AttributeId") or 0)
        if attr_id in name_ids:
            value = first_value(attr, attrs.get(attr_id))
            if value not in (None, "", False):
                return str(value)
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


def filter_values(record: dict, attr_defs: dict[int, dict]) -> dict[str, set[str]]:
    values: dict[str, set[str]] = {}
    for attr in latest_attrs(record):
        if int(attr.get("ParentAttributeId") or 0) != 0:
            continue
        attr_id = int(attr.get("AttributeId") or 0)
        attr_def = attr_defs.get(attr_id)
        if attr_is_deleted(attr_def):
            continue
        attr_type = str((attr_def or {}).get("AttributeType") or "").lower()
        if attr_type not in ("checkboxes", "select", "boolean", "radio"):
            continue
        value = first_value(attr, attr_def)
        if value in (None, "", False):
            continue
        key = str(attr_id)
        values.setdefault(key, set()).add("Yes" if value is True else str(value))
    return values


def filter_catalog(records: list[dict], attr_defs: dict[int, dict], selected: dict[str, list[str]] | None = None) -> list[dict]:
    selected = selected or {}
    record_values = [(record, filter_values(record, attr_defs)) for record in records]
    filter_ids = sorted({key for _, values in record_values for key in values})
    filters = []
    for filter_id in filter_ids:
        scoped_records = [
            record
            for record, values in record_values
            if filter_record_values_match(values, {key: value for key, value in selected.items() if key != filter_id})
        ]
        value_counts: dict[str, int] = {}
        for record in scoped_records:
            values = filter_values(record, attr_defs).get(filter_id, set())
            for value in values:
                value_counts[value] = value_counts.get(value, 0) + 1
        attr_id = int(filter_id)
        attr_def = attr_defs.get(attr_id)
        values = [{"value": value, "count": count} for value, count in sorted(value_counts.items(), key=lambda item: item[0].lower())]
        if not values:
            continue
        selected_values = set(selected.get(filter_id) or [])
        covers_all_scoped_records = len(values) == 1 and values[0]["count"] == len(scoped_records)
        if covers_all_scoped_records and not selected_values:
            continue
        filters.append(
            {
                "id": filter_id,
                "label": attr_label(attr_def, attr_id),
                "type": "multi" if str((attr_def or {}).get("AttributeType") or "").lower() in ("checkboxes", "boolean") else "single",
                "values": values,
            }
        )
    return sorted(filters, key=lambda item: item["label"].lower())


def filter_record_values_match(values: dict[str, set[str]], selected: dict[str, list[str]]) -> bool:
    if not selected:
        return True
    for key, wanted in selected.items():
        wanted_set = {str(item) for item in wanted if str(item)}
        if not wanted_set:
            continue
        record_values = values.get(str(key), set())
        if not record_values.intersection(wanted_set):
            return False
    return True


def matches_filters(record: dict, attr_defs: dict[int, dict], selected: dict[str, list[str]]) -> bool:
    if not selected:
        return True
    return filter_record_values_match(filter_values(record, attr_defs), selected)


def selected_filters(qs: dict[str, list[str]]) -> dict[str, list[str]]:
    filters: dict[str, list[str]] = {}
    for key, values in qs.items():
        if not key.startswith("f_"):
            continue
        filter_id = key[2:]
        selected = []
        for value in values:
            selected.extend(item for item in str(value).split("|") if item)
        if selected:
            filters[filter_id] = selected
    return filters


def fetch_remote_asset(url: str) -> tuple[bytes, str]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Unsupported asset URL")
    request = Request(url, headers={"User-Agent": "DB-Data-Browser/1.0"})
    with urlopen(request, timeout=15) as response:
        content = response.read()
        content_type = response.headers.get("Content-Type") or mimetypes.guess_type(parsed.path)[0] or "application/octet-stream"
    return content, content_type


def table_parent_ids(attr_defs: dict[int, dict], fallback_ids: list[int], names: tuple[str, ...]) -> list[int]:
    parent_ids = []
    for attr_id, attr_def in attr_defs.items():
        if attr_is_deleted(attr_def):
            continue
        attr_type = str(attr_def.get("AttributeType") or "").lower()
        text = " ".join(str(attr_def.get(key) or "") for key in ("AttributeName", "DispName")).lower()
        is_sot = bool(attr_def.get("SOTFlag")) or any(name in text for name in names)
        if attr_type == "table_model" and is_sot:
            parent_ids.append(int(attr_id))
    for fallback_id in fallback_ids:
        if fallback_id not in parent_ids:
            parent_ids.append(fallback_id)
    return parent_ids


def table_model_columns(attr_defs: dict[int, dict], parent_ids: list[int]) -> list[dict]:
    columns = []
    seen = set()
    for parent_id in parent_ids:
        parent_def = attr_defs.get(parent_id)
        target_model_id = parent_def.get("TargetModelId") if parent_def else None
        if not target_model_id:
            continue
        candidates = []
        for attr_id, attr_def in attr_defs.items():
            if attr_is_deleted(attr_def):
                continue
            if attr_def.get("ProductModelId") != target_model_id:
                continue
            if str(attr_def.get("AttributeType") or "").lower() == "table_model":
                continue
            candidates.append((int(attr_def.get("DisplayOrder") or 0), attr_id, attr_def))
        for _, attr_id, attr_def in sorted(candidates):
            key = str(attr_id)
            if key in seen:
                continue
            seen.add(key)
            columns.append(
                {
                    "key": key,
                    "label": attr_label(attr_def, attr_id),
                    "unit": attr_def.get("Unit") or "",
                    "attribute_id": attr_id,
                }
            )
    return columns


def find_attribute_ids(attr_defs: dict[int, dict], names: tuple[str, ...], types: tuple[str, ...] = (), fallback_ids: list[int] | None = None) -> list[int]:
    fallback_ids = fallback_ids or []
    normalized_types = {item.lower() for item in types}
    matches = []
    for attr_id, attr_def in attr_defs.items():
        attr_type = str(attr_def.get("AttributeType") or "").lower()
        if normalized_types and attr_type not in normalized_types:
            continue
        text = " ".join(str(attr_def.get(key) or "") for key in ("AttributeName", "DispName")).lower()
        if any(name in text for name in names):
            matches.append((int(attr_def.get("DisplayOrder") or 0), attr_id))
    result = [item[1] for item in sorted(matches)]
    for fallback_id in fallback_ids:
        if fallback_id not in result:
            result.append(fallback_id)
    return result


def first_field_value(row: dict, keys: tuple[str, ...], default: object = "") -> object:
    values = row_map(row)
    for key in keys:
        item = values.get(key)
        if item and item.get("value") not in (None, "", False):
            return item.get("value")
    normalized_keys = tuple(key.lower() for key in keys)
    for item in row.get("values") or []:
        text = " ".join(str(item.get(key) or "") for key in ("attribute_name", "label")).lower()
        if any(key in text for key in normalized_keys) and item.get("value") not in (None, "", False):
            return item.get("value")
    return default


def first_field_by_ids(row: dict, attribute_ids: list[int]) -> dict | None:
    ids = {int(attribute_id) for attribute_id in attribute_ids}
    for item in row.get("values") or []:
        if int(item.get("attribute_id") or 0) in ids:
            return item
    return None


def rows_for_parent(all_attrs: list[dict], parent_id: int, attr_defs: dict[int, dict]) -> list[dict]:
    grouped: dict[tuple[int, str], list[dict]] = {}
    for item in values_for_parent(all_attrs, parent_id, attr_defs):
        key = (int(item["row"] or 0), str(item.get("parent_hash") or ""))
        grouped.setdefault(key, []).append(item)
    rows = []
    for (row_index, row_hash), items in sorted(grouped.items()):
        rows.append({"row": row_index, "hash": str((items[0] or {}).get("hash") or row_hash), "values": items})
    return rows


def rows_for_parents(all_attrs: list[dict], parent_ids: list[int], attr_defs: dict[int, dict]) -> list[dict]:
    rows = []
    seen = set()
    for parent_id in parent_ids:
        for row in rows_for_parent(all_attrs, parent_id, attr_defs):
            marker = (parent_id, row.get("row"), row.get("hash"))
            if marker in seen:
                continue
            seen.add(marker)
            rows.append(row)
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


def parameter_media(version: dict) -> list[dict]:
    media = []
    for item in version.get("filesParameters") or []:
        url = item.get("fileUrl") or ""
        name = item.get("fileName") or Path(url).name or item.get("parameterName") or "plik"
        ext = str(name or url).rsplit(".", 1)[-1].lower() if "." in str(name or url) else ""
        media.append(
            {
                "name": name,
                "url": url,
                "kind": "image" if ext in {"jpg", "jpeg", "png", "webp", "gif"} else "file",
                "parameter": item.get("parameterName"),
            }
        )
    return media


def version_parameters(version: dict) -> dict[str, object]:
    values = {}
    for param in version.get("parameters") or []:
        name = str(param.get("parameterName") or "")
        if not name:
            continue
        value = first_parameter_value(param)
        if value not in (None, "", False):
            values[name] = value
    return values


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


def rows_as_model_table(rows: list[dict], model_columns: list[dict]) -> dict:
    ad_hoc_columns: dict[str, dict] = {}
    table_rows = []
    for row in rows:
        values = {}
        for item in row.get("values") or []:
            attr_id = item.get("attribute_id")
            key = str(attr_id or item.get("attribute_name") or "")
            if not key:
                continue
            if not any(column["key"] == key for column in model_columns):
                ad_hoc_columns.setdefault(key, {"key": key, "label": str(item.get("label") or key), "unit": ""})
            values[key] = item.get("value")
        table_rows.append({"row": row.get("row"), "hash": row.get("hash"), "values": values})
    return {
        "columns": [*model_columns, *ad_hoc_columns.values()],
        "rows": table_rows,
    }


def compact_text(value: object, limit: int = 400) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def preview_fields(items: list[dict], limit: int = 3) -> list[dict]:
    fields = []
    for item in items:
        value = item.get("value")
        if value in (None, "", False, True):
            continue
        fields.append({"label": item.get("label", ""), "value": compact_text(value, 120)})
        if len(fields) >= limit:
            break
    return fields


def safe_filename(value: str) -> str:
    name = Path(value or "attachment").name
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120] or "attachment"


def extract_ai_answer(payload: object) -> str:
    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, list):
        for item in payload:
            answer = extract_ai_answer(item)
            if answer:
                return answer
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in ("response", "answer", "output_text", "text", "content", "result"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (dict, list)):
            answer = extract_ai_answer(value)
            if answer:
                return answer
    message = payload.get("message")
    if isinstance(message, dict):
        answer = extract_ai_answer(message)
        if answer:
            return answer
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            answer = extract_ai_answer(choice)
            if answer:
                return answer
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            answer = extract_ai_answer(item)
            if answer:
                return answer
    data = payload.get("data")
    if isinstance(data, (dict, list)):
        answer = extract_ai_answer(data)
        if answer:
            return answer
    return ""


class PimData:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.product_models = load_json_if_exists(data_dir / "productsModels.json", {"models": []})
        self.product_attr_defs, _ = attribute_maps(load_json_if_exists(data_dir / "productsAttributes.json", {"attributes": []}))
        self.element_attr_defs, _ = attribute_maps(load_json_if_exists(data_dir / "buildingsElementsAttributes.json", {"attributes": []}))
        self.products = load_json_if_exists(data_dir / "products.json", {"products": []}).get("products", [])
        self.elements = load_json_if_exists(data_dir / "building_elements.json", {"buildingElements": []}).get("buildingElements", [])
        self.colors = load_json(data_dir / "colors.json").get("colors", []) if (data_dir / "colors.json").exists() else []
        self.color_groups = load_json(data_dir / "colorGroups.json").get("colorGroups", []) if (data_dir / "colorGroups.json").exists() else []
        self.product_name_attribute_ids = product_name_attribute_ids(self.product_attr_defs, self.product_models)
        self.sot_parent_ids = table_parent_ids(self.product_attr_defs, [276], ("typoszereg", "series of types", "sot"))
        self.sot_columns = table_model_columns(self.product_attr_defs, self.sot_parent_ids)
        self.element_schema = {
            "variant_parent_ids": find_attribute_ids(self.element_attr_defs, ("variant", "wariant"), ("model_array",), [283]),
            "layer_parent_ids": find_attribute_ids(self.element_attr_defs, ("layer", "warstw"), ("model_array",), [285]),
            "available_parent_ids": find_attribute_ids(self.element_attr_defs, ("available products", "dostępne produkty", "produkty"), ("model_array",), [289]),
            "product_ids": find_attribute_ids(self.element_attr_defs, ("product", "produkt"), ("product",), [290]),
            "default_ids": find_attribute_ids(self.element_attr_defs, ("default", "domyśl"), ("boolean",), [298]),
        }
        self.product_index = {int(product["Id"]): product for product in self.products}
        self.element_index = {int(element["Id"]): element for element in self.elements}
        self.color_index = {int(color["Id"]): color for color in self.colors}
        self.color_group_index = {int(group["Id"]): group for group in self.color_groups}
        self.color_group_members: dict[int, list[int]] = {}
        for group in self.color_groups:
            group_id = int(group["Id"])
            version = (group.get("dataVersions") or [{}])[-1]
            for color_id in version.get("colorList") or []:
                self.color_group_members.setdefault(int(color_id), []).append(group_id)
        self.product_color_usage = self.build_product_color_usage()

    def summary(self) -> dict:
        consistency = self.consistency_report()
        return {
            "data_dir": str(self.data_dir),
            "products": len(self.products),
            "systems": len(self.elements),
            "colors": len(self.colors),
            "color_groups": len(self.color_groups),
            "product_attributes": len(self.product_attr_defs),
            "system_attributes": len(self.element_attr_defs),
            "consistency": consistency,
        }

    def referenced_product_ids(self) -> set[int]:
        referenced = set()
        for element in self.elements:
            attrs = latest_attrs(element)
            available_rows = rows_for_parents(attrs, self.element_schema["available_parent_ids"], self.element_attr_defs)
            for available in available_rows:
                product_item = first_field_by_ids(available, self.element_schema["product_ids"])
                raw = product_item.get("raw") if product_item else {}
                product_id = raw.get("IntValue") if raw else None
                if isinstance(product_id, int):
                    referenced.add(product_id)
        return referenced

    def consistency_report(self) -> dict:
        referenced_ids = {str(item) for item in self.referenced_product_ids()}
        product_identities = self.product_identity_values()
        unused_ids = []
        if self.elements and referenced_ids:
            for product_id, identities in product_identities.items():
                if not identities.intersection(referenced_ids):
                    unused_ids.append(product_id)
        missing_ids = sorted(referenced_ids - set().union(*product_identities.values())) if product_identities else sorted(referenced_ids)
        return {
            "referenced_products": len(referenced_ids),
            "products_without_building_element": len(unused_ids),
            "building_element_product_ids_missing_in_products": len(missing_ids),
            "products_without_building_element_sample": [
                {"id": product_id, "name": product_name(self.product_index[product_id], self.product_attr_defs, self.product_name_attribute_ids)}
                for product_id in unused_ids[:12]
                if product_id in self.product_index
            ],
            "missing_product_id_sample": missing_ids[:12],
        }

    def product_identity_values(self) -> dict[int, set[str]]:
        result = {}
        for product in self.products:
            product_id = int(product["Id"])
            identities = {str(product_id)}
            for attr in latest_attrs(product):
                attr_id = int(attr.get("AttributeId") or 0)
                attr_def = self.product_attr_defs.get(attr_id)
                label = attr_label(attr_def, attr_id).lower()
                is_identity = any(token in label for token in ("pim id", "sap id", "kod", "code", "nazwa", "name"))
                if not is_identity and attr.get("ParentAttributeId") not in self.sot_parent_ids:
                    continue
                for key in ("varcharValue", "TextValue", "IntValue", "IntValue2", "NumberValue"):
                    value = attr.get(key)
                    if value not in (None, "", False):
                        identities.add(str(value))
            result[product_id] = identities
        return result

    def ai_catalog(self, mode: str = "", limit: int = 80) -> list[dict]:
        mode = mode.lower().strip()
        items = []
        if mode in ("", "products"):
            for product in self.products[:limit]:
                detail = self.product_detail(int(product["Id"]))
                values = [item.get("value") for item in detail.get("general") or []]
                features = [f"{item.get('name')}: {item.get('value')}" for item in detail.get("features") or []]
                items.append(
                    {
                        "type": "product",
                        "id": detail["id"],
                        "name": detail["name"],
                        "text": compact_text(" | ".join(str(value) for value in [*values, *features] if value), 1200),
                    }
                )
        if mode in ("", "systems"):
            for element in self.elements[:limit]:
                detail = self.system_detail(int(element["Id"]))
                values = [item.get("value") for item in detail.get("general") or []]
                layers = [
                    f"{variant.get('name')}: " + ", ".join(layer.get("name") or "" for layer in variant.get("layers") or [])
                    for variant in detail.get("system_variants") or []
                ]
                items.append(
                    {
                        "type": "building_element",
                        "id": detail["id"],
                        "name": detail["name"],
                        "text": compact_text(" | ".join(str(value) for value in [*values, *layers] if value), 1200),
                    }
                )
        if mode in ("", "colors"):
            for group in self.color_groups[:limit]:
                detail = self.color_group_detail(int(group["Id"]), compact=True)
                items.append(
                    {
                        "type": "visual_attribute_group",
                        "id": detail["id"],
                        "name": detail["name"],
                        "text": compact_text(f"{detail.get('description', '')} | items: {detail.get('count', 0)}", 600),
                    }
                )
        return items

    def list_products(self, query: str = "", filters: dict[str, list[str]] | None = None) -> dict:
        query = query.lower().strip()
        filters = filters or {}
        items = []
        query_records = []
        for product in self.products:
            detail = self.product_detail(int(product["Id"]), compact=True)
            haystack = " ".join(str(value) for value in [detail["name"], detail.get("unit"), *detail.get("categories", [])]).lower()
            if query and query not in haystack:
                continue
            query_records.append(product)
            if not matches_filters(product, self.product_attr_defs, filters):
                continue
            items.append(detail)
        return {"items": items, "filters": filter_catalog(query_records, self.product_attr_defs, filters)}

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
        sot_rows = rows_for_parents(attrs, self.sot_parent_ids, self.product_attr_defs)
        result = {
            "id": product_id,
            "name": product_name(product, self.product_attr_defs, self.product_name_attribute_ids),
            "unit": unit,
            "categories": categories,
            "attribute_count": len(attrs),
            "thumbnail": next((item["url"] for item in file_media(latest_version) if item.get("kind") == "image"), ""),
            "preview_fields": preview_fields(root_values),
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
                "sot_table": rows_as_model_table(sot_rows, self.sot_columns),
                "media": file_media(latest_version),
                "color_links": self.product_color_links(latest_version),
            }
        )
        return result

    def build_product_color_usage(self) -> dict[str, list[dict]]:
        usage: dict[str, list[dict]] = {}
        for product in self.products:
            product_id = int(product["Id"])
            product_name_value = product_name(product, self.product_attr_defs, self.product_name_attribute_ids)
            version = (product.get("dataVersions") or [{}])[-1]
            for item in version.get("colorsAttributes") or []:
                element_id = item.get("ElementId")
                element_type = item.get("ElementTypeId")
                attribute_id = item.get("AttributeId")
                if element_id is None:
                    continue
                key = f"{element_type}:{element_id}"
                usage.setdefault(key, []).append(
                    {
                        "product_id": product_id,
                        "product_name": product_name_value,
                        "relation": "structure" if attribute_id == 296 else "color_group" if attribute_id == 297 else "color",
                    }
                )
                if element_type == 2:
                    group = self.color_group_index.get(int(element_id))
                    if not group:
                        continue
                    version_group = (group.get("dataVersions") or [{}])[-1]
                    for color_id in version_group.get("colorList") or []:
                        usage.setdefault(f"1:{int(color_id)}", []).append(
                            {
                                "product_id": product_id,
                                "product_name": product_name_value,
                                "relation": "via_group",
                                "group_id": int(element_id),
                                "group_name": self.color_group_name(int(element_id)),
                            }
                        )
        for key, items in usage.items():
            seen = set()
            unique = []
            for item in items:
                marker = (item.get("product_id"), item.get("relation"), item.get("group_id"))
                if marker in seen:
                    continue
                seen.add(marker)
                unique.append(item)
            usage[key] = unique
        return usage

    def product_color_links(self, version: dict) -> dict:
        structures = []
        groups = []
        for item in version.get("colorsAttributes") or []:
            element_id = item.get("ElementId")
            if element_id is None:
                continue
            if item.get("AttributeId") == 296 or item.get("ElementTypeId") == 1:
                color = self.color_index.get(int(element_id))
                if color:
                    structures.append(self.color_detail(int(element_id), compact=True))
            elif item.get("AttributeId") == 297 or item.get("ElementTypeId") == 2:
                group_id = int(element_id)
                group = self.color_group_detail(group_id, compact=True)
                groups.append(group)
        return {"structures": structures, "groups": groups}

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

    def list_systems(self, query: str = "", filters: dict[str, list[str]] | None = None) -> dict:
        query = query.lower().strip()
        filters = filters or {}
        items = []
        query_records = []
        for element in self.elements:
            detail = self.system_detail(int(element["Id"]), compact=True)
            haystack = " ".join(str(value) for value in detail.values()).lower()
            if query and query not in haystack:
                continue
            query_records.append(element)
            if not matches_filters(element, self.element_attr_defs, filters):
                continue
            items.append(detail)
        return {"items": items, "filters": filter_catalog(query_records, self.element_attr_defs, filters)}

    def list_colors(self, query: str = "", kind: str = "") -> dict:
        query = query.lower().strip()
        kind = kind.lower().strip()
        items = []
        for color in self.colors:
            detail = self.color_detail(int(color["Id"]), compact=True)
            haystack = " ".join(str(value) for value in detail.values()).lower()
            if query and query not in haystack:
                continue
            if kind and detail.get("type") != kind:
                continue
            items.append(detail)
        return {"items": items, "groups": self.list_color_groups()["items"]}

    def color_detail(self, color_id: int, compact: bool = False) -> dict:
        color = self.color_index[color_id]
        version = (color.get("dataVersions") or [{}])[-1]
        params = version_parameters(version)
        media = parameter_media(version)
        thumbnail = next((item["url"] for item in media if item.get("parameter") == "Thumbnail"), "")
        main_texture = next((item["url"] for item in media if item.get("parameter") == "MainTexture"), "")
        material_order = ["MainTexture", "normal_map", "displacement_map", "opacity_map", "roughness_map"]
        material_maps = []
        for parameter in material_order:
            item = next((entry for entry in media if entry.get("parameter") == parameter), None)
            if item:
                material_maps.append(item)
        rgb = None
        if all(key in params for key in ("r", "g", "b")):
            rgb = {"r": params["r"], "g": params["g"], "b": params["b"]}
        color_type = str(params.get("type") or "")
        result = {
            "id": color_id,
            "name": str(params.get("name") or f"Kolor {color_id}"),
            "type": color_type,
            "rgb": rgb,
            "thumbnail": main_texture if color_type == "advanced" else thumbnail,
            "main_texture": main_texture,
            "normal_map": next((item["url"] for item in material_maps if item.get("parameter") == "normal_map"), ""),
            "displacement_map": next((item["url"] for item in material_maps if item.get("parameter") == "displacement_map"), ""),
            "opacity_map": next((item["url"] for item in material_maps if item.get("parameter") == "opacity_map"), ""),
            "roughness_map": next((item["url"] for item in material_maps if item.get("parameter") == "roughness_map"), ""),
            "material_maps": material_maps,
            "media": media,
            "used_by_products": self.product_color_usage.get(f"1:{color_id}", []),
            "groups": [
                {"id": group_id, "name": self.color_group_name(group_id)}
                for group_id in self.color_group_members.get(color_id, [])
            ],
        }
        if compact:
            return result
        result["parameters"] = [{"name": key, "value": value} for key, value in params.items()]
        return result

    def color_group_name(self, group_id: int) -> str:
        group = self.color_group_index.get(group_id)
        if not group:
            return f"Grupa {group_id}"
        version = (group.get("dataVersions") or [{}])[-1]
        params = version_parameters(version)
        return str(params.get("name") or f"Grupa {group_id}")

    def color_group_detail(self, group_id: int, compact: bool = False) -> dict:
        group = self.color_group_index[group_id]
        version = (group.get("dataVersions") or [{}])[-1]
        params = version_parameters(version)
        media = parameter_media(version)
        color_ids = [int(color_id) for color_id in version.get("colorList") or []]
        result = {
            "id": group_id,
            "name": str(params.get("name") or f"Grupa {group_id}"),
            "description": str(params.get("description") or ""),
            "count": len(color_ids),
            "color_ids": color_ids,
            "sample_colors": [self.color_detail(color_id, compact=True) for color_id in color_ids[:12] if color_id in self.color_index],
            "media": media,
            "used_by_products": self.product_color_usage.get(f"2:{group_id}", []),
            "preview_fields": [{"label": "Items", "value": len(color_ids)}, *preview_fields([{"label": key, "value": value} for key, value in params.items()], 2)],
        }
        if not compact:
            result["parameters"] = [{"name": key, "value": value} for key, value in params.items()]
            result["colors"] = [self.color_detail(color_id, compact=True) for color_id in color_ids if color_id in self.color_index]
        return result

    def list_color_groups(self) -> dict:
        items = []
        for group in self.color_groups:
            items.append(self.color_group_detail(int(group["Id"]), compact=True))
        return {"items": items}

    def system_detail(self, element_id: int, compact: bool = False) -> dict:
        element = self.element_index[element_id]
        versions = element.get("dataVersions") or [{}]
        latest_version = versions[-1] if versions else {}
        attrs = latest_attrs(element)
        root_values = values_for_parent(attrs, 0, self.element_attr_defs)
        variant_rows = rows_for_parents(attrs, self.element_schema["variant_parent_ids"], self.element_attr_defs)
        layer_rows = rows_for_parents(attrs, self.element_schema["layer_parent_ids"], self.element_attr_defs)
        available_rows = rows_for_parents(attrs, self.element_schema["available_parent_ids"], self.element_attr_defs)
        result = {
            "id": element_id,
            "name": element_name(element, self.element_attr_defs),
            "attribute_count": len(attrs),
            "type": next((item["value"] for item in root_values if item["attribute_id"] == 281), ""),
            "insulation": next((item["value"] for item in root_values if item["attribute_id"] == 292), ""),
            "bim_type": next((item["value"] for item in root_values if item["attribute_id"] == 299), ""),
            "thumbnail": next((item["url"] for item in file_media(latest_version) if item.get("kind") == "image"), ""),
            "preview_fields": preview_fields(root_values),
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
                    product_item = first_field_by_ids(available, self.element_schema["product_ids"])
                    default_item = first_field_by_ids(available, self.element_schema["default_ids"])
                    raw = product_item.get("raw") if product_item else {}
                    product_id = raw.get("IntValue") if raw else None
                    product_variant = raw.get("IntValue2") if raw else None
                    linked_product = self.product_index.get(int(product_id)) if isinstance(product_id, int) else None
                    products.append(
                        {
                            "product_id": product_id,
                            "linked_product_id": int(product_id) if linked_product else None,
                            "product_name": product_name(linked_product, self.product_attr_defs, self.product_name_attribute_ids) if linked_product else product_item.get("value") if product_item else "",
                            "variant": product_variant,
                            "default": bool(default_item.get("value")) if default_item else False,
                        }
                    )
                variant_layers.append(
                    {
                        "row": layer.get("row"),
                        "position": first_field_value(layer, ("layer_position", "pozycja warstwy", "position"), ""),
                        "name": first_field_value(layer, ("layer_name", "nazwa warstwy", "warstwa", "name"), ""),
                        "products": products,
                    }
                )
            variants.append(
                {
                    "row": variant.get("row"),
                    "name": first_field_value(variant, ("variant_name", "nazwa wariantu", "wariant", "name"), f"Wariant {variant.get('row')}"),
                    "layers": variant_layers,
                }
            )
        return variants


class NotesStore:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / NOTES_FILE
        self.attachments_dir = data_dir / ATTACHMENTS_DIR

    def load(self) -> dict:
        payload = load_json_if_exists(self.path, {"notes": []})
        payload.setdefault("notes", [])
        for note in payload["notes"]:
            note.setdefault("resolved", False)
            note.setdefault("accepted", False)
            note.setdefault("attachments", [])
        return payload

    def list_notes(self) -> dict:
        notes = sorted(self.load()["notes"], key=lambda item: item.get("updated_at", 0), reverse=True)
        return {"items": notes}

    def get_note(self, record_type: str, record_id: int) -> dict:
        key = self.note_key(record_type, record_id)
        for note in self.load()["notes"]:
            if note.get("key") == key:
                return note
        return {
            "key": key,
            "record_type": record_type,
            "record_id": record_id,
            "requires_correction": False,
            "resolved": False,
            "accepted": False,
            "attachments": [],
            "comment": "",
        }

    def save_note(self, payload: dict) -> dict:
        record_type = str(payload.get("record_type") or "")
        record_id = int(payload.get("record_id") or 0)
        key = self.note_key(record_type, record_id)
        now = int(time.time())
        existing = self.get_note(record_type, record_id)
        attachments = list(existing.get("attachments") or [])
        attachment = payload.get("attachment")
        if isinstance(attachment, dict):
            saved_attachment = self.save_attachment(key, attachment)
            if saved_attachment:
                attachments.append(saved_attachment)
        note = {
            "key": key,
            "record_type": record_type,
            "record_id": record_id,
            "record_name": str(payload.get("record_name") or ""),
            "requires_correction": bool(payload.get("requires_correction")),
            "resolved": bool(payload.get("resolved")),
            "accepted": bool(payload.get("accepted")),
            "comment": str(payload.get("comment") or "").strip(),
            "attachments": attachments,
            "updated_at": now,
        }
        if note["resolved"]:
            note["resolved_at"] = now
        data = self.load()
        notes = [item for item in data["notes"] if item.get("key") != key]
        if note["requires_correction"] or note["comment"] or note["resolved"] or note["accepted"] or note["attachments"]:
            notes.append(note)
        data["notes"] = notes
        write_json(self.path, data)
        return note

    def save_attachment(self, key: str, attachment: dict) -> dict | None:
        raw = str(attachment.get("data") or "")
        if not raw:
            return None
        if "," in raw and raw.startswith("data:"):
            raw = raw.split(",", 1)[1]
        try:
            content = base64.b64decode(raw)
        except Exception:
            return None
        folder = self.attachments_dir / safe_filename(key)
        folder.mkdir(parents=True, exist_ok=True)
        filename = f"{int(time.time())}_{safe_filename(str(attachment.get('name') or 'attachment'))}"
        path = folder / filename
        path.write_bytes(content)
        return {
            "name": str(attachment.get("name") or filename),
            "stored_name": filename,
            "content_type": str(attachment.get("content_type") or "application/octet-stream"),
            "size": len(content),
            "url": f"/api/notes/attachment/{safe_filename(key)}/{filename}",
        }

    def attachment_path(self, key: str, filename: str) -> Path:
        return self.attachments_dir / safe_filename(key) / safe_filename(filename)

    def bulk_accept(self, payload: dict) -> dict:
        record_type = str(payload.get("record_type") or "")
        accepted = bool(payload.get("accepted"))
        records = payload.get("records") or []
        count = 0
        for record in records:
            if not isinstance(record, dict):
                continue
            record_id = int(record.get("id") or 0)
            if not record_type or not record_id:
                continue
            existing = self.get_note(record_type, record_id)
            self.save_note(
                {
                    **existing,
                    "record_type": record_type,
                    "record_id": record_id,
                    "record_name": str(record.get("name") or existing.get("record_name") or ""),
                    "accepted": accepted,
                }
            )
            count += 1
        return {"updated": count, "accepted": accepted}

    def import_notes(self, payload: dict) -> dict:
        incoming = payload.get("notes") if isinstance(payload, dict) else None
        if incoming is None and isinstance(payload, dict) and isinstance(payload.get("items"), list):
            incoming = payload.get("items")
        if not isinstance(incoming, list):
            raise ValueError("Expected notes list")
        data = self.load()
        notes_by_key = {item.get("key"): item for item in data["notes"] if item.get("key")}
        imported = 0
        for item in incoming:
            if not isinstance(item, dict):
                continue
            record_type = str(item.get("record_type") or "")
            record_id = int(item.get("record_id") or 0)
            if not record_type or not record_id:
                continue
            key = self.note_key(record_type, record_id)
            note = {
                "key": key,
                "record_type": record_type,
                "record_id": record_id,
                "record_name": str(item.get("record_name") or ""),
                "requires_correction": bool(item.get("requires_correction")),
                "resolved": bool(item.get("resolved")),
                "accepted": bool(item.get("accepted")),
                "comment": str(item.get("comment") or "").strip(),
                "attachments": list(item.get("attachments") or []),
                "updated_at": int(item.get("updated_at") or time.time()),
            }
            if item.get("resolved_at"):
                note["resolved_at"] = int(item.get("resolved_at"))
            notes_by_key[key] = note
            imported += 1
        write_json(self.path, {"notes": list(notes_by_key.values())})
        return {"imported": imported, "total": len(notes_by_key)}

    @staticmethod
    def note_key(record_type: str, record_id: int) -> str:
        return f"{record_type}:{record_id}"


class AiAgent:
    def __init__(self) -> None:
        self.base_url = (os.environ.get("DB_DATA_BROWSER_AI_URL") or os.environ.get("AI_AGENT_URL") or "").rstrip("/")
        self.model = os.environ.get("DB_DATA_BROWSER_AI_MODEL") or os.environ.get("OLLAMA_MODEL") or "qwen2.5-coder:14b"

    def ollama_root(self) -> str:
        return re.sub(r"/api/(generate|tags)$", "", self.base_url)

    def status(self) -> dict:
        if not self.base_url:
            return {"available": False, "reason": "DB_DATA_BROWSER_AI_URL is not configured", "model": self.model}
        try:
            url = self.base_url if self.base_url.endswith("/api/tags") else f"{self.ollama_root()}/api/tags"
            request = Request(url, headers={"User-Agent": "DB-Data-Browser/1.0"})
            with urlopen(request, timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
            models = [item.get("name") for item in payload.get("models", [])]
            return {"available": True, "url": self.base_url, "model": self.model, "models": models}
        except Exception as error:
            return {"available": False, "url": self.base_url, "model": self.model, "reason": str(error)}

    def search(self, question: str, catalog: list[dict]) -> dict:
        status = self.status()
        if not status.get("available"):
            return {"available": False, "answer": "", "status": status}
        prompt = (
            "Jestes agentem wyszukiwania w DB Data Browser. Odpowiadaj po polsku. "
            "Znajdz pasujace produkty, elementy budowlane lub visual attributes na podstawie pól, opisów, cech i filtrów. "
            "Zwracaj konkretne nazwy, typ rekordu i ID. Jesli nie ma pewnosci, powiedz czego brakuje.\n\n"
            f"Pytanie użytkownika: {question}\n\n"
            f"Dane do przeszukania:\n{json.dumps(catalog, ensure_ascii=False)}"
        )
        endpoint = self.base_url if self.base_url.endswith("/api/generate") else f"{self.ollama_root()}/api/generate"
        body = json.dumps({"model": self.model, "prompt": prompt, "stream": False}, ensure_ascii=False).encode("utf-8")
        request = Request(endpoint, data=body, headers={"Content-Type": "application/json", "User-Agent": "DB-Data-Browser/1.0"})
        with urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
        answer = extract_ai_answer(payload)
        raw_keys = sorted(payload.keys()) if isinstance(payload, dict) else []
        if not answer:
            answer = f"AI returned an empty text response. Raw response keys: {', '.join(raw_keys) or type(payload).__name__}."
        return {"available": True, "answer": answer, "status": status, "raw_keys": raw_keys}


class DataStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.data: PimData | None = None
        self.notes = NotesStore(data_dir)
        self.ai = AiAgent()
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

    def clear(self) -> dict:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for filename in REQUIRED_FILES:
            path = self.data_dir / filename
            if path.exists():
                path.unlink()
        self.reload()
        return self.status()

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
                if parsed.path == "/api/asset":
                    self.send_asset(qs.get("url", [""])[0])
                    return
                elif parsed.path == "/api/summary":
                    payload = data.summary()
                elif parsed.path == "/api/products":
                    payload = data.list_products(query=query, filters=selected_filters(qs))
                elif parsed.path.startswith("/api/products/"):
                    payload = data.product_detail(int(unquote(parsed.path.rsplit("/", 1)[-1])))
                elif parsed.path == "/api/systems":
                    payload = data.list_systems(query=query, filters=selected_filters(qs))
                elif parsed.path.startswith("/api/systems/"):
                    payload = data.system_detail(int(unquote(parsed.path.rsplit("/", 1)[-1])))
                elif parsed.path == "/api/colors":
                    payload = data.list_colors(query=query, kind=qs.get("kind", [""])[0])
                elif parsed.path.startswith("/api/colors/"):
                    payload = data.color_detail(int(unquote(parsed.path.rsplit("/", 1)[-1])))
                elif parsed.path == "/api/color-groups":
                    payload = data.list_color_groups()
                elif parsed.path.startswith("/api/color-groups/"):
                    payload = data.color_group_detail(int(unquote(parsed.path.rsplit("/", 1)[-1])))
                else:
                    self.send_json({"error": "Not found"}, status=404)
                    return
                self.send_json(payload)
            except (KeyError, ValueError):
                self.send_json({"error": "Record not found"}, status=404)

        def send_asset(self, url: str):
            try:
                body, content_type = fetch_remote_asset(url)
            except Exception as error:
                self.send_json({"error": str(error)}, status=502)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, payload: dict, status: int = 200):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_download(self, payload: dict, filename: str):
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
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
            if parsed.path == "/api/source/clear":
                self.send_json(store.clear())
                return
            if parsed.path == "/api/notes":
                self.send_json(store.notes.save_note(read_json_body(self)))
                return
            if parsed.path == "/api/notes/import":
                try:
                    self.send_json(store.notes.import_notes(read_json_body(self)))
                except ValueError as error:
                    self.send_json({"error": str(error)}, status=400)
                return
            if parsed.path == "/api/records/accept":
                self.send_json(store.notes.bulk_accept(read_json_body(self)))
                return
            if parsed.path == "/api/ai/search":
                payload = read_json_body(self)
                question = str(payload.get("question") or "")
                mode = str(payload.get("mode") or "")
                try:
                    self.send_json(store.ai.search(question, self.ready_data().ai_catalog(mode=mode)))
                except Exception as error:
                    self.send_json({"available": False, "answer": "", "error": str(error)}, status=502)
                return
            self.send_json({"error": "Not found"}, status=404)

        def handle_api(self, parsed):
            qs = parse_qs(parsed.query)
            query = qs.get("q", [""])[0]
            try:
                if parsed.path == "/api/asset":
                    self.send_asset(qs.get("url", [""])[0])
                    return
                elif parsed.path == "/api/source/status":
                    payload = store.status()
                elif parsed.path == "/api/ai/status":
                    payload = store.ai.status()
                elif parsed.path == "/api/notes":
                    payload = store.notes.list_notes()
                elif parsed.path == "/api/notes/export":
                    self.send_download(store.notes.load(), "browser_corrections.json")
                    return
                elif parsed.path == "/api/notes/export.csv":
                    self.send_csv(notes_as_csv(store.notes.list_notes()["items"]), "browser_corrections.csv")
                    return
                elif parsed.path.startswith("/api/notes/attachment/"):
                    _, key, filename = parsed.path.rsplit("/", 2)
                    self.send_file(store.notes.attachment_path(unquote(key), unquote(filename)))
                    return
                elif parsed.path.startswith("/api/notes/"):
                    _, record_type, record_id = parsed.path.rsplit("/", 2)
                    payload = store.notes.get_note(unquote(record_type), int(unquote(record_id)))
                elif parsed.path == "/api/summary":
                    if not store.data:
                        self.send_json({"error": "Data source is not ready", **store.status()}, status=409)
                        return
                    payload = store.data.summary()
                    payload["source_status"] = store.status()
                elif parsed.path == "/api/products":
                    payload = self.ready_data().list_products(query=query, filters=selected_filters(qs))
                elif parsed.path.startswith("/api/products/"):
                    payload = self.ready_data().product_detail(int(unquote(parsed.path.rsplit("/", 1)[-1])))
                elif parsed.path == "/api/systems":
                    payload = self.ready_data().list_systems(query=query, filters=selected_filters(qs))
                elif parsed.path.startswith("/api/systems/"):
                    payload = self.ready_data().system_detail(int(unquote(parsed.path.rsplit("/", 1)[-1])))
                elif parsed.path == "/api/colors":
                    payload = self.ready_data().list_colors(query=query, kind=qs.get("kind", [""])[0])
                elif parsed.path.startswith("/api/colors/"):
                    payload = self.ready_data().color_detail(int(unquote(parsed.path.rsplit("/", 1)[-1])))
                elif parsed.path == "/api/color-groups":
                    payload = self.ready_data().list_color_groups()
                elif parsed.path.startswith("/api/color-groups/"):
                    payload = self.ready_data().color_group_detail(int(unquote(parsed.path.rsplit("/", 1)[-1])))
                else:
                    self.send_json({"error": "Not found"}, status=404)
                    return
                self.send_json(payload)
            except (KeyError, ValueError):
                self.send_json({"error": "Record not found"}, status=404)

        def send_asset(self, url: str):
            try:
                body, content_type = fetch_remote_asset(url)
            except Exception as error:
                self.send_json({"error": str(error)}, status=502)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_file(self, path: Path):
            if not path.exists() or not path.is_file():
                self.send_json({"error": "Attachment not found"}, status=404)
                return
            body = path.read_bytes()
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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

        def send_download(self, payload: dict, filename: str):
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_csv(self, text: str, filename: str):
            body = ("\ufeff" + text).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
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
