from __future__ import annotations

import argparse
import base64
import cgi
import csv
import ipaddress
import io
import json
import mimetypes
import os
import re
import socket
import time
import unicodedata
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen


APP_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = APP_DIR / "data"
NOTES_FILE = "browser_corrections.json"
PROJECT_FILE = "browser_review_project.json"
ATTACHMENTS_DIR = "correction_attachments"
VALIDATION_MODEL_FILE = "browser_validation_model.json"
DATA_PATCHES_FILE = "browser_data_patches.json"
MAX_JSON_BODY_BYTES = 10 * 1024 * 1024
MAX_UPLOAD_BYTES = 250 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_REMOTE_ASSET_BYTES = 20 * 1024 * 1024
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
    if length > MAX_JSON_BODY_BYTES:
        raise ValueError("JSON body is too large")
    return json.loads(handler.rfile.read(length).decode("utf-8"))


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def typed_payload_value(value: object, attr_def: dict | None = None) -> dict:
    attr_type = normalize_query_text((attr_def or {}).get("AttributeType"))
    if attr_type in {"boolean", "checkbox", "checkboxes"}:
        if isinstance(value, bool):
            return {"BooleanValue": value}
        text = normalize_query_text(value)
        return {"BooleanValue": text in {"1", "true", "yes", "tak", "y"}}
    if attr_type in {"number", "decimal", "float", "double", "integer", "int"}:
        number = number_or_none(value)
        if number is not None:
            return {"NumberValue": number}
    if attr_type in {"select", "radio", "one of many", "one_of_many"}:
        raw = str(value or "").strip()
        for option in (attr_def or {}).get("AttributeOptions") or []:
            option_text = str(option.get("OptionName") or option.get("OptionValue") or option.get("Id"))
            if raw == str(option.get("Id")) or normalize_query_text(raw) == normalize_query_text(option_text):
                return {"IntValue": option.get("Id")}
        number = number_or_none(raw)
        if number is not None:
            return {"IntValue": int(number)}
    return {"varcharValue": "" if value is None else str(value)}


def apply_typed_value(target: dict, value: object, attr_def: dict | None = None) -> None:
    for key in ("varcharValue", "TextValue", "IntValue", "IntValue2", "NumberValue", "BooleanValue"):
        if key in target:
            target[key] = None if key != "BooleanValue" else False
    for key, typed_value in typed_payload_value(value, attr_def).items():
        target[key] = typed_value


def notes_as_csv(notes: list[dict]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "key",
            "record_type",
            "record_id",
            "record_name",
            "field_label",
            "field_key",
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
                "field_label": note.get("field_label", ""),
                "field_key": note.get("field_key", ""),
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


def selection_attributes(record: dict, attr_defs: dict[int, dict]) -> list[dict]:
    grouped: dict[int, dict] = {}
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
        kind = "many of many" if attr_type in ("checkboxes", "boolean") else "one of many"
        item = grouped.setdefault(
            attr_id,
            {
                "attribute_id": attr_id,
                "label": attr_label(attr_def, attr_id),
                "attribute_name": (attr_def or {}).get("AttributeName"),
                "selector_type": kind,
                "attribute_type": attr_type,
                "values": [],
            },
        )
        display_value = "Yes" if value is True else str(value)
        if display_value not in item["values"]:
            item["values"].append(display_value)
    return sorted(grouped.values(), key=lambda item: item["label"].lower())


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


def product_type_id(product: dict) -> int | None:
    raw = product.get("prodctTypeId", product.get("productTypeId"))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def product_ownership(product: dict) -> dict:
    type_id = product_type_id(product)
    if type_id == 1:
        return {"key": "own", "label": "Own product", "type_id": type_id}
    if type_id == 2:
        return {"key": "foreign", "label": "External product", "type_id": type_id}
    return {"key": "unknown", "label": "Unknown ownership", "type_id": type_id}


def product_ownership_filter(records: list[dict], selected: list[str] | None = None) -> dict:
    selected_set = set(selected or [])
    counts: dict[str, dict] = {}
    for product in records:
        ownership = product_ownership(product)
        item = counts.setdefault(ownership["key"], {"value": ownership["key"], "label": ownership["label"], "count": 0})
        item["count"] += 1
    return {
        "id": "_ownership",
        "label": "Product ownership",
        "type": "single",
        "values": [
            {"value": item["value"], "label": item["label"], "count": item["count"]}
            for item in sorted(counts.values(), key=lambda entry: entry["label"])
            if item["count"] or item["value"] in selected_set
        ],
    }


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
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Unsupported asset URL")
    host = parsed.hostname.strip()
    try:
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise ValueError("Asset host cannot be resolved") from error
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            raise ValueError("Asset URL host is not allowed")
    request = Request(url, headers={"User-Agent": "DB-Data-Browser/1.0"})
    with urlopen(request, timeout=15) as response:
        content = response.read(MAX_REMOTE_ASSET_BYTES + 1)
        if len(content) > MAX_REMOTE_ASSET_BYTES:
            raise ValueError("Asset is too large")
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


def number_or_none(value: object) -> float | None:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def rgb_analysis(rgb: dict | None) -> str:
    if not rgb:
        return ""
    r = number_or_none(rgb.get("r"))
    g = number_or_none(rgb.get("g"))
    b = number_or_none(rgb.get("b"))
    if r is None or g is None or b is None:
        return ""
    r = max(0, min(255, r))
    g = max(0, min(255, g))
    b = max(0, min(255, b))
    brightness = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    max_value = max(r, g, b)
    min_value = min(r, g, b)
    chroma = max_value - min_value
    if chroma < 18:
        hue = "neutral gray szary szare"
    elif max_value == r:
        hue_value = (60 * ((g - b) / chroma) + 360) % 360
        hue = "red czerwony czerwone" if hue_value < 25 or hue_value >= 340 else "orange pomaranczowy pomaranczowe" if hue_value < 55 else "yellow zolty zolte"
    elif max_value == g:
        hue_value = 60 * ((b - r) / chroma) + 120
        hue = "green zielony zielone" if hue_value < 170 else "cyan turkusowy turkusowe"
    else:
        hue_value = 60 * ((r - g) / chroma) + 240
        hue = "blue niebieski niebieskie" if hue_value < 265 else "purple violet fioletowy fioletowe"
    tone = "dark ciemny ciemne" if brightness < 0.35 else "light jasny jasne" if brightness > 0.72 else "medium sredni srednie"
    saturation = "low saturation stonowany" if chroma < 45 else "high saturation nasycony" if chroma > 120 else "medium saturation"
    return f"color analysis: {tone}, {hue}, {saturation}, brightness {round(brightness, 2)}"


def visual_attribute_ai_text(detail: dict) -> str:
    parameters = [f"{item.get('name')}: {item.get('value')}" for item in detail.get("parameters") or []]
    maps = [
        f"{item.get('parameter')}: {item.get('name') or item.get('url')}"
        for item in detail.get("material_maps") or []
    ]
    groups = [item.get("name", "") for item in detail.get("groups") or []]
    rgb = detail.get("rgb") or {}
    is_advanced = detail.get("type") == "advanced"
    fields = [
        detail.get("name", ""),
        f"type: {detail.get('type') or 'simple'}",
        "texture tekstura material map maps normal opacity roughness displacement" if is_advanced else "color kolor rgb",
        f"RGB: {rgb.get('r')}, {rgb.get('g')}, {rgb.get('b')}" if rgb else "",
        rgb_analysis(rgb),
        f"groups: {', '.join(groups)}" if groups else "",
        f"used by products: {len(detail.get('used_by_products') or [])}",
        f"maps: {', '.join(maps)}" if maps else "",
        *parameters,
    ]
    return " | ".join(str(item) for item in fields if item)


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


def ai_record_target(item_type: str) -> dict:
    if item_type == "product":
        return {"mode": "products", "kind": "detail"}
    if item_type == "building_element":
        return {"mode": "systems", "kind": "detail"}
    if item_type == "visual_attribute_group":
        return {"mode": "colors", "kind": "color_group"}
    if item_type == "visual_attribute":
        return {"mode": "colors", "kind": "detail"}
    return {"mode": "", "kind": "detail"}


def related_ai_records(question: str, answer: str, catalog: list[dict], limit: int = 12) -> list[dict]:
    haystack = f"{question} {answer}".lower().replace("_", " ")
    tokens = ai_query_tokens(question)
    scored = []
    seen = set()
    for index, item in enumerate(catalog):
        item_type = str(item.get("type") or "")
        item_id = item.get("id")
        name = str(item.get("name") or "")
        text = str(item.get("text") or "")
        key = (item_type, str(item_id))
        if not item_type or item_id in (None, "") or key in seen:
            continue
        searchable = f"{item_type} {item_id} {name} {text}".lower().replace("_", " ")
        score = 0
        if name and name.lower() in haystack:
            score += 8
        if str(item_id).lower() in haystack:
            score += 6
        score += sum(1 for token in tokens if text_has_token(searchable, token))
        if score <= 0 and index < 5:
            score = 1
        if score <= 0:
            continue
        target = ai_record_target(item_type)
        scored.append(
            (
                -score,
                index,
                {
                    "type": item_type,
                    "id": item_id,
                    "name": name,
                    "mode": target["mode"],
                    "kind": target["kind"],
                },
            )
        )
        seen.add(key)
    scored.sort(key=lambda entry: (entry[0], entry[1]))
    return [entry[2] for entry in scored[:limit]]


def ai_query_tokens(query: str) -> list[str]:
    stop_words = {"kolor", "kolory", "cecha", "cechy", "visual", "attributes", "attribute", "znajdz", "pokaż", "pokaz"}
    return [
        token for token in re.findall(r"[\w.-]+", query.lower().replace("_", " "))
        if len(token) > 2 and token not in stop_words
    ]


def matches_ai_query(text: str, query: str) -> bool:
    normalized = text.lower().replace("_", " ")
    tokens = ai_query_tokens(query)
    if not tokens:
        return False
    return any(token in normalized for token in tokens)


def ai_query_tokens(query: str) -> list[str]:
    groups = ai_query_token_groups(query)
    tokens = []
    for group in groups:
        for token in group:
            if token not in tokens:
                tokens.append(token)
    return tokens


def ai_query_token_groups(query: str) -> list[list[str]]:
    stop_words = {
        "kolor", "kolory", "kolorow", "kolorach", "cecha", "cechy", "visual", "attributes", "attribute",
        "znajdz", "pokaz", "odcien", "odcieniu", "odcienie", "barwa", "barwie", "zakres", "ktore",
    }
    aliases = {
        "czerwony": ["red", "czerwony", "czerwone", "czerwona", "czerwonym", "czerwonymi", "czerwieni"],
        "niebieski": ["blue", "niebieski", "niebieskie", "niebieska", "niebieskim", "niebieskimi"],
        "zielony": ["green", "zielony", "zielone", "zielona", "zielonym", "zielonymi"],
        "zolty": ["yellow", "zolty", "zolte", "zolta", "zoltym", "zoltymi"],
        "pomaranczowy": ["orange", "pomaranczowy", "pomaranczowe", "pomaranczowa", "pomaranczowym"],
        "fioletowy": ["purple", "violet", "fioletowy", "fioletowe", "fioletowa", "fioletowym"],
        "szary": ["gray", "grey", "szary", "szare", "szara", "szarym", "szarymi"],
        "jasny": ["light", "jasny", "jasne", "jasna", "jasnym", "jasnymi"],
        "ciemny": ["dark", "ciemny", "ciemne", "ciemna", "ciemnym", "ciemnymi"],
    }
    reverse_aliases = {variant: values for values in aliases.values() for variant in values}
    groups = []
    for token in re.findall(r"[\w.-]+", query.lower().replace("_", " ")):
        if len(token) <= 2 or token in stop_words:
            continue
        groups.append(reverse_aliases.get(token, [token]))
    return groups


def matches_ai_query(text: str, query: str) -> bool:
    normalized = text.lower().replace("_", " ")
    groups = ai_query_token_groups(query)
    if not groups:
        return False
    return all(any(text_has_token(normalized, token) for token in group) for group in groups)


def text_has_token(text: str, token: str) -> bool:
    return re.search(rf"(^|[^\w.-]){re.escape(token)}($|[^\w.-])", text) is not None


def normalize_query_text(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").lower())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", text.replace("_", " ")).strip()


def value_is_present(value: object) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def validation_field_key(item: dict) -> str:
    return normalize_query_text(item.get("key") or item.get("name") or item.get("label")).replace(" ", "_")


def builtin_validation_fields() -> list[dict]:
    return [
        {
            "key": "lambda",
            "label": "Lambda",
            "record_type": "product",
            "locations": ["sot", "features", "general"],
            "aliases": [
                "lambda",
                "lamda",
                "wspolczynnik przewodzenia",
                "conductivity",
                "thermal conductivity",
                "278",
            ],
        },
        {
            "key": "thickness",
            "label": "Thickness",
            "record_type": "product",
            "locations": ["sot", "features", "general"],
            "aliases": ["thickness", "grubosc", "grubosci", "277"],
        },
        {
            "key": "density",
            "label": "Density",
            "record_type": "product",
            "locations": ["sot", "features", "general"],
            "aliases": ["density", "gestosc", "279"],
        },
        {
            "key": "documents",
            "label": "Documents",
            "record_type": "product",
            "locations": ["documents"],
            "aliases": ["document", "documents", "dokument", "dokumenty", "plik", "pliki"],
        },
        {
            "key": "layers",
            "label": "Layers",
            "record_type": "system",
            "locations": ["layers"],
            "aliases": ["layer", "layers", "warstwa", "warstwy"],
        },
        {
            "key": "products",
            "label": "Products",
            "record_type": "system",
            "locations": ["products"],
            "aliases": ["assigned products", "available products", "layer products", "przypisane produkty", "dostepne produkty", "produkty w warstwach"],
        },
    ]


def normalize_validation_field(item: dict) -> dict:
    key = validation_field_key(item)
    label = str(item.get("label") or item.get("name") or key).strip() or key
    aliases = item.get("aliases") if isinstance(item.get("aliases"), list) else []
    aliases = [str(alias) for alias in aliases if str(alias).strip()]
    locations = item.get("locations") if isinstance(item.get("locations"), list) else []
    locations = [normalize_query_text(location) for location in locations if str(location).strip()]
    record_type = normalize_query_text(item.get("record_type") or item.get("type") or "product")
    if record_type in {"products", "produkt"}:
        record_type = "product"
    if record_type in {"systems", "building_element", "building elements", "systemy"}:
        record_type = "system"
    return {
        "key": key,
        "label": label,
        "record_type": record_type or "product",
        "aliases": list(dict.fromkeys([label, key, *aliases])),
        "locations": locations or ["general", "features", "sot"],
    }


def normalize_validation_model(payload: dict | None) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    fields = [normalize_validation_field(item) for item in builtin_validation_fields()]
    custom_fields = payload.get("fields") if isinstance(payload.get("fields"), list) else []
    for item in custom_fields:
        if isinstance(item, dict):
            fields.append(normalize_validation_field(item))
    merged: dict[str, dict] = {}
    for field in fields:
        existing = merged.get(field["key"])
        if not existing:
            merged[field["key"]] = field
            continue
        existing["aliases"] = list(dict.fromkeys([*existing.get("aliases", []), *field.get("aliases", [])]))
        existing["locations"] = list(dict.fromkeys([*existing.get("locations", []), *field.get("locations", [])]))
        existing["label"] = field.get("label") or existing.get("label")
    return {
        "fields": list(merged.values()),
        "source": payload.get("source") or "",
        "updated_at": int(payload.get("updated_at") or 0),
    }


def field_matches_query(field: dict, query: str) -> bool:
    normalized = normalize_query_text(query)
    aliases = [normalize_query_text(alias) for alias in field.get("aliases") or []]
    return any(alias and (alias in normalized or text_has_token(normalized, alias)) for alias in aliases)


def validation_intent(query: str) -> dict:
    normalized = normalize_query_text(query)
    row_tokens = ("wariant", "wariantow", "variant", "variants", "typoszereg", "sot", "wiersz", "row", "rows")
    missing_tokens = ("bez", "brak", "brakuje", "missing", "empty", "puste", "pusty", "not set", "nie ma")
    all_rows_tokens = ("dla wszystkich", "wszystkich wariant", "all variants", "every variant", "kazdy wariant")
    relation = "missing"
    if any(token in normalized for token in all_rows_tokens) or "nie jest dla wszystkich" in normalized:
        relation = "missing_in_some_rows"
    elif any(token in normalized for token in missing_tokens) and any(token in normalized for token in row_tokens):
        relation = "missing_in_rows"
    return {
        "relation": relation,
        "row_scope": relation in {"missing_in_rows", "missing_in_some_rows"},
        "is_validation": any(token in normalized for token in missing_tokens) or relation != "missing",
    }


def infer_validation_field_from_query(query: str) -> dict | None:
    normalized = normalize_query_text(query)
    tokens = data_query_tokens(query)
    ignored = {
        "bez", "brak", "brakuje", "missing", "empty", "puste", "pusty", "not", "set", "nie", "ma",
        "wariant", "wariantow", "variant", "variants", "typoszereg", "sot", "wiersz", "row", "rows",
        "maja", "wartosc", "wartosci", "jakies", "komorka", "komorkach", "cell", "cells",
        "to",
    }
    field_tokens = [token for token in tokens if token not in ignored]
    if not field_tokens:
        return None
    phrase = " ".join(field_tokens[:6])
    record_type = "system" if any(token in normalized for token in ("system", "systemy", "building element", "element")) else "product"
    return normalize_validation_field(
        {
            "key": phrase.replace(" ", "_"),
            "label": phrase,
            "record_type": record_type,
            "locations": ["general", "features", "sot"],
            "aliases": [phrase, *field_tokens],
        }
    )


def data_query_tokens(query: str) -> list[str]:
    stop_words = {
        "znajdz", "znajdź", "pokaz", "pokaż", "wybierz", "wszystkie", "ktore", "które", "maja", "mają",
        "jest", "sa", "są", "oraz", "albo", "dla", "danych", "parametr", "parametry", "parametrach",
        "ma", "wartosc", "wartosci", "jakies", "komorka", "komorkach", "cell", "cells",
        "to",
        "produkty", "produkt", "kolory", "kolor", "visual", "attribute", "attributes",
    }
    return [
        token for token in re.findall(r"[\w.-]+", normalize_query_text(query))
        if len(token) > 1 and token not in stop_words
    ]


def parse_numeric_conditions(query: str) -> list[dict]:
    normalized = normalize_query_text(query)
    conditions = []
    pattern = r"\b([a-z][\w.-]*)\s*(>=|<=|>|<|=|:)\s*(-?\d+(?:[\.,]\d+)?)"
    for name, operator, raw_value in re.findall(pattern, normalized):
        value = number_or_none(raw_value)
        if value is None:
            continue
        conditions.append({"name": name, "operator": "=" if operator == ":" else operator, "value": value})
    return conditions


def numeric_comparisons(query: str) -> list[dict]:
    normalized = normalize_query_text(query)
    comparisons = []
    pattern = r"(>=|<=|>|<|=|:)\s*(-?\d+(?:[\.,]\d+)?)"
    for operator, raw_value in re.findall(pattern, normalized):
        value = number_or_none(raw_value)
        if value is not None:
            comparisons.append({"operator": "=" if operator == ":" else operator, "value": value})
    word_patterns = [
        (r"(?:mniejsz\w* niz|less than|below|ponizej)\s*(-?\d+(?:[\.,]\d+)?)", "<"),
        (r"(?:wieksz\w* niz|greater than|above|powyzej)\s*(-?\d+(?:[\.,]\d+)?)", ">"),
        (r"(?:nie wieksz\w* niz|at most|maksymalnie)\s*(-?\d+(?:[\.,]\d+)?)", "<="),
        (r"(?:nie mniejsz\w* niz|at least|minimum|minimalnie)\s*(-?\d+(?:[\.,]\d+)?)", ">="),
    ]
    for pattern, operator in word_patterns:
        for raw_value in re.findall(pattern, normalized):
            value = number_or_none(raw_value)
            if value is not None:
                comparisons.append({"operator": operator, "value": value})
    return comparisons


def numeric_conditions_for_field(query: str, field: dict) -> list[dict]:
    named = [
        condition for condition in parse_numeric_conditions(query)
        if field_matches_query(field, condition.get("name") or "")
    ]
    if named:
        return named
    comparisons = numeric_comparisons(query)
    if comparisons and field_matches_query(field, query):
        return [{"name": field["key"], **comparisons[0]}]
    return []


def parse_text_conditions(query: str) -> list[dict]:
    normalized = normalize_query_text(query)
    conditions = []
    pattern = r"\b([a-z][\w.-]*(?:\s+[a-z][\w.-]*){0,3})\s*(?:=|:|jest|rowna sie|równa się)\s*\"?([\w./ -]{1,60})\"?"
    for name, value in re.findall(pattern, normalized):
        name = " ".join(data_query_tokens(name)) or name
        value = value.strip(" .,:;")
        if value and number_or_none(value) is None:
            conditions.append({"name": name.strip(), "operator": "contains", "value": value})
    return conditions


def numeric_condition_matches(actual: object, operator: str, expected: float) -> bool:
    value = number_or_none(actual)
    if value is None:
        return False
    if operator == ">":
        return value > expected
    if operator == ">=":
        return value >= expected
    if operator == "<":
        return value < expected
    if operator == "<=":
        return value <= expected
    return value == expected


def text_condition_matches(actual: object, expected: str) -> bool:
    return normalize_query_text(expected) in normalize_query_text(actual)


def implicit_numeric_conditions_for_field(query: str, field: dict) -> list[dict]:
    if parse_numeric_conditions(query):
        return []
    normalized = normalize_query_text(query)
    if not field_matches_query(field, normalized):
        return []
    values = [number_or_none(item) for item in re.findall(r"(?<![a-z])-?\d+(?:[\.,]\d+)?(?![a-z])", normalized)]
    values = [value for value in values if value is not None]
    if len(values) != 1:
        return []
    return [{"name": field["key"], "operator": "=", "value": values[0]}]


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

    def data_query(self, question: str, model: dict | None = None, limit: int = 100) -> dict | None:
        validation_result = self.validation_query(question, model, limit)
        if validation_result:
            return validation_result
        ownership_result = self.product_ownership_query(question, limit)
        if ownership_result:
            return ownership_result
        product_result = self.product_value_query(question, model, limit)
        if product_result:
            return product_result
        color_result = self.color_parameter_query(question, limit)
        if color_result:
            return color_result
        return None

    def validation_query(self, question: str, model: dict | None = None, limit: int = 100) -> dict | None:
        intent = validation_intent(question)
        if not intent["is_validation"]:
            return None
        normalized_model = normalize_validation_model(model)
        fields = [field for field in normalized_model["fields"] if field_matches_query(field, question)]
        if not fields:
            inferred_field = infer_validation_field_from_query(question)
            if not inferred_field:
                return None
            fields = [inferred_field]
        field = fields[0]
        if field["record_type"] == "system":
            return self.validate_system_field(question, field, intent, limit)
        return self.validate_product_field(question, field, intent, limit)

    def validate_product_field(self, question: str, field: dict, intent: dict, limit: int) -> dict:
        findings = []
        related_records = []
        checked = 0
        for product in self.products:
            product_id = int(product["Id"])
            detail = self.product_detail(product_id)
            if intent["row_scope"]:
                row_result = self.validate_product_rows(detail, field)
                if not row_result["checked_rows"]:
                    continue
                checked += 1
                if row_result["missing_rows"]:
                    findings.append(
                        {
                            "type": "product",
                            "id": product_id,
                            "name": detail["name"],
                            "field": field["label"],
                            "issue": "missing_in_rows",
                            "checked_rows": row_result["checked_rows"],
                            "missing_rows": row_result["missing_rows"],
                            "present_rows": row_result["present_rows"],
                            "message": f"{field['label']} is missing in rows: {', '.join(str(row) for row in row_result['missing_rows'][:12])}",
                        }
                    )
            else:
                checked += 1
                if not self.product_has_field(detail, field):
                    findings.append(
                        {
                            "type": "product",
                            "id": product_id,
                            "name": detail["name"],
                            "field": field["label"],
                            "issue": "missing",
                            "message": f"{field['label']} is missing.",
                        }
                    )
            if findings and findings[-1]["id"] == product_id:
                related_records.append({"type": "product", "id": product_id, "name": detail["name"], "mode": "products", "kind": "detail"})
            if len(findings) >= limit:
                break
        answer = (
            f"Validation rule: {field['label']}. Checked {checked} products and found {len(findings)} records with missing data."
            if findings
            else f"Validation rule: {field['label']}. Checked {checked} products and found no missing data."
        )
        return {
            "available": True,
            "validation": True,
            "rule": {"field": field, "intent": intent},
            "question": question,
            "answer": answer,
            "findings": findings,
            "related_records": related_records,
        }

    def validate_product_rows(self, detail: dict, field: dict) -> dict:
        rows = []
        locations = set(field.get("locations") or [])
        if locations.intersection({"sot", "typoszereg", "variants", "warianty"}):
            rows.extend(detail.get("sot") or [])
            if not rows:
                rows.extend(detail.get("variants") or [])
        if not rows and "features" in locations:
            rows.extend(detail.get("custom_attributes") or [])
        checked_rows = []
        missing_rows = []
        present_rows = []
        for index, row in enumerate(rows):
            row_number = row.get("row")
            label = int(row_number) + 1 if isinstance(row_number, int) else index + 1
            checked_rows.append(label)
            if self.row_has_field(row, field):
                present_rows.append(label)
            else:
                missing_rows.append(label)
        return {"checked_rows": checked_rows, "missing_rows": missing_rows, "present_rows": present_rows}

    def row_has_field(self, row: dict, field: dict) -> bool:
        aliases = [normalize_query_text(alias) for alias in field.get("aliases") or []]
        for item in row.get("values") or []:
            labels = [
                item.get("label"),
                item.get("attribute_name"),
                item.get("attribute_id"),
            ]
            haystack = " ".join(normalize_query_text(label) for label in labels)
            if any(alias and (alias in haystack or text_has_token(haystack, alias)) for alias in aliases):
                return value_is_present(item.get("value"))
        return False

    def product_has_field(self, detail: dict, field: dict) -> bool:
        aliases = [normalize_query_text(alias) for alias in field.get("aliases") or []]
        for item in detail.get("general") or []:
            haystack = normalize_query_text(f"{item.get('label')} {item.get('attribute_name')} {item.get('attribute_id')}")
            if any(alias and (alias in haystack or text_has_token(haystack, alias)) for alias in aliases):
                return value_is_present(item.get("value"))
        for feature in detail.get("features") or []:
            haystack = normalize_query_text(feature.get("name"))
            if any(alias and (alias in haystack or text_has_token(haystack, alias)) for alias in aliases):
                return value_is_present(feature.get("value"))
        if "documents" in set(field.get("locations") or []):
            return bool((detail.get("documents") or []) or (detail.get("media") or []))
        return bool(self.validate_product_rows(detail, field)["present_rows"])

    def validate_system_field(self, question: str, field: dict, intent: dict, limit: int) -> dict:
        findings = []
        related_records = []
        checked = 0
        locations = set(field.get("locations") or [])
        for element in self.elements:
            element_id = int(element["Id"])
            detail = self.system_detail(element_id)
            checked += 1
            missing = False
            if locations.intersection({"layers", "warstwy"}):
                missing = not any(variant.get("layers") for variant in detail.get("system_variants") or [])
            elif locations.intersection({"products", "produkty"}):
                layers = [layer for variant in detail.get("system_variants") or [] for layer in variant.get("layers") or []]
                missing = any(not layer.get("products") for layer in layers) if layers else True
            else:
                missing = not any(
                    field_matches_query(field, f"{item.get('label')} {item.get('value')}")
                    for item in detail.get("general") or []
                )
            if missing:
                findings.append(
                    {
                        "type": "system",
                        "id": element_id,
                        "name": detail["name"],
                        "field": field["label"],
                        "issue": "missing",
                        "message": f"{field['label']} is missing or incomplete.",
                    }
                )
                related_records.append({"type": "system", "id": element_id, "name": detail["name"], "mode": "systems", "kind": "detail"})
            if len(findings) >= limit:
                break
        answer = f"Validation rule: {field['label']}. Checked {checked} building elements and found {len(findings)} records with missing data."
        return {
            "available": True,
            "validation": True,
            "rule": {"field": field, "intent": intent},
            "question": question,
            "answer": answer,
            "findings": findings,
            "related_records": related_records,
        }

    def product_ownership_query(self, question: str, limit: int = 100) -> dict | None:
        normalized = normalize_query_text(question)
        if not any(token in normalized for token in ("produkt", "produkty", "product", "products")):
            return None
        own_tokens = ("wlasne", "wlasny", "wlasnych", "own", "company product", "firmowe", "firmy")
        foreign_tokens = ("obce", "obcy", "obcych", "external", "foreign", "third party", "cudze")
        wanted = ""
        if any(token in normalized for token in own_tokens):
            wanted = "own"
        elif any(token in normalized for token in foreign_tokens):
            wanted = "foreign"
        else:
            return None
        findings = []
        related_records = []
        checked = 0
        for product in self.products:
            checked += 1
            ownership = product_ownership(product)
            if ownership["key"] != wanted:
                continue
            detail = self.product_detail(int(product["Id"]), compact=True)
            findings.append(
                {
                    "type": "product",
                    "id": detail["id"],
                    "name": detail["name"],
                    "field": "Product ownership",
                    "issue": "matched",
                    "message": f"{ownership['label']} (prodctTypeId={ownership.get('type_id')})",
                }
            )
            related_records.append({"type": "product", "id": detail["id"], "name": detail["name"], "mode": "products", "kind": "detail"})
            if len(findings) >= limit:
                break
        answer = f"Data query: checked {checked} products and selected {len(findings)} {wanted} products."
        return {
            "available": True,
            "validation": True,
            "query_type": "product_ownership",
            "question": question,
            "answer": answer,
            "findings": findings,
            "related_records": related_records,
        }

    def product_value_query(self, question: str, model: dict | None = None, limit: int = 100) -> dict | None:
        normalized = normalize_query_text(question)
        if not any(token in normalized for token in ("produkt", "produkty", "product", "products")):
            return None
        numeric_conditions = parse_numeric_conditions(question)
        text_conditions = parse_text_conditions(question)
        normalized_model = normalize_validation_model(model)
        fields = [field for field in normalized_model["fields"] if field.get("record_type") == "product" and field_matches_query(field, question)]
        if not fields and (numeric_conditions or text_conditions):
            condition_names = [condition["name"] for condition in [*numeric_conditions, *text_conditions]]
            fields = [
                field for field in normalized_model["fields"]
                if field.get("record_type") == "product"
                and any(field_matches_query(field, name) for name in condition_names)
            ]
        if not fields:
            inferred_field = infer_validation_field_from_query(question)
            if inferred_field and inferred_field.get("record_type") == "product":
                fields = [inferred_field]
        if not fields:
            return None
        field = fields[0]
        numeric_conditions = numeric_conditions_for_field(question, field)
        if not numeric_conditions:
            numeric_conditions = implicit_numeric_conditions_for_field(question, field)
        has_positive_intent = any(token in normalized for token in ("ma ", "maja", "maja wartosc", "wartosc", "wartosci", "z ", "gdzie"))
        if not has_positive_intent and not numeric_conditions and not text_conditions:
            return None
        findings = []
        related_records = []
        checked = 0
        for product in self.products:
            product_id = int(product["Id"])
            detail = self.product_detail(product_id)
            checked += 1
            matches = self.product_field_matches_query(detail, field, numeric_conditions, text_conditions)
            if not matches:
                continue
            findings.append(
                {
                    "type": "product",
                    "id": product_id,
                    "name": detail["name"],
                    "field": field["label"],
                    "issue": "matched",
                    "message": self.product_value_match_summary(field, matches, numeric_conditions, text_conditions),
                    "matches": matches[:8],
                }
            )
            related_records.append({"type": "product", "id": product_id, "name": detail["name"], "mode": "products", "kind": "detail"})
            if len(findings) >= limit:
                break
        answer = f"Data query: checked {checked} products and selected {len(findings)} records matching {field['label']}."
        return {
            "available": True,
            "validation": True,
            "query_type": "product_values",
            "rule": {"field": field, "numeric_conditions": numeric_conditions, "text_conditions": text_conditions},
            "question": question,
            "answer": answer,
            "findings": findings,
            "related_records": related_records,
        }

    def product_field_matches_query(self, detail: dict, field: dict, numeric_conditions: list[dict], text_conditions: list[dict]) -> list[dict]:
        values = self.product_field_values(detail, field)
        present_values = [item for item in values if value_is_present(item.get("value"))]
        if not present_values:
            return []
        if numeric_conditions:
            matches = []
            for item in present_values:
                for condition in numeric_conditions:
                    if self.condition_applies_to_field(condition, field) and numeric_condition_matches(item.get("value"), condition["operator"], condition["value"]):
                        matches.append({**item, "condition": f"{condition['name']} {condition['operator']} {condition['value']}"})
            return matches
        if text_conditions:
            matches = []
            for item in present_values:
                for condition in text_conditions:
                    if self.condition_applies_to_field(condition, field) and text_condition_matches(item.get("value"), condition["value"]):
                        matches.append({**item, "condition": f"{condition['name']} contains {condition['value']}"})
            return matches
        return present_values

    def condition_applies_to_field(self, condition: dict, field: dict) -> bool:
        aliases = [normalize_query_text(alias) for alias in field.get("aliases") or []]
        name = normalize_query_text(condition.get("name"))
        return not name or any(alias and (alias in name or name in alias or text_has_token(name, alias)) for alias in aliases)

    def product_field_values(self, detail: dict, field: dict) -> list[dict]:
        aliases = [normalize_query_text(alias) for alias in field.get("aliases") or []]
        values = []
        for item in detail.get("general") or []:
            haystack = normalize_query_text(f"{item.get('label')} {item.get('attribute_name')} {item.get('attribute_id')}")
            if any(alias and (alias in haystack or text_has_token(haystack, alias)) for alias in aliases):
                values.append({"location": "general", "label": item.get("label"), "value": item.get("value")})
        for feature in detail.get("features") or []:
            haystack = normalize_query_text(feature.get("name"))
            if any(alias and (alias in haystack or text_has_token(haystack, alias)) for alias in aliases):
                values.append({"location": "features", "label": feature.get("name"), "value": feature.get("value")})
        for row in [*(detail.get("sot") or []), *(detail.get("variants") or [])]:
            row_number = row.get("row")
            row_label = int(row_number) + 1 if isinstance(row_number, int) else row_number
            for item in row.get("values") or []:
                haystack = normalize_query_text(f"{item.get('label')} {item.get('attribute_name')} {item.get('attribute_id')}")
                if any(alias and (alias in haystack or text_has_token(haystack, alias)) for alias in aliases):
                    values.append({"location": "row", "row": row_label, "label": item.get("label"), "value": item.get("value")})
        return values

    def product_value_match_summary(self, field: dict, matches: list[dict], numeric_conditions: list[dict], text_conditions: list[dict]) -> str:
        samples = []
        for item in matches[:4]:
            row = f"row {item.get('row')}: " if item.get("row") not in (None, "") else ""
            samples.append(f"{row}{item.get('label')}: {item.get('value')}")
        suffix = ""
        if numeric_conditions:
            suffix = " | conditions: " + ", ".join(f"{item['name']} {item['operator']} {item['value']}" for item in numeric_conditions)
        elif text_conditions:
            suffix = " | conditions: " + ", ".join(f"{item['name']} contains {item['value']}" for item in text_conditions)
        return f"{field['label']} values: {', '.join(samples)}{suffix}"

    def color_parameter_query(self, question: str, limit: int = 100) -> dict | None:
        normalized = normalize_query_text(question)
        color_intent_tokens = (
            "kolor", "kolory", "barwa", "barwy", "visual", "attribute", "attributes", "rgb",
            "tekstura", "tekstury", "texture", "material", "map", "advanced",
        )
        if not any(token in normalized for token in color_intent_tokens):
            return None
        conditions = parse_numeric_conditions(question)
        tokens = data_query_tokens(question)
        condition_names = {condition["name"] for condition in conditions}
        tokens = [token for token in tokens if token not in condition_names and number_or_none(token) is None]
        findings = []
        related_records = []
        checked = 0
        for color in self.colors:
            color_id = int(color["Id"])
            detail = self.color_detail(color_id)
            checked += 1
            if not self.color_matches_conditions(detail, conditions):
                continue
            if tokens and not self.color_matches_terms(detail, tokens):
                continue
            findings.append(
                {
                    "type": "color",
                    "id": color_id,
                    "name": detail["name"],
                    "field": "Visual attribute",
                    "issue": "matched",
                    "message": self.color_match_summary(detail, conditions, tokens),
                }
            )
            related_records.append({"type": "visual_attribute", "id": color_id, "name": detail["name"], "mode": "colors", "kind": "detail"})
            if len(findings) >= limit:
                break
        if not findings and not conditions and not tokens:
            return None
        answer = f"Data query: checked {checked} visual attributes and selected {len(findings)} matching colors."
        return {
            "available": True,
            "validation": True,
            "query_type": "color_parameters",
            "question": question,
            "answer": answer,
            "findings": findings,
            "related_records": related_records,
        }

    def color_matches_conditions(self, detail: dict, conditions: list[dict]) -> bool:
        if not conditions:
            return True
        params = {normalize_query_text(item.get("name")): item.get("value") for item in detail.get("parameters") or []}
        rgb = detail.get("rgb") or {}
        for condition in conditions:
            name = normalize_query_text(condition["name"])
            value = params.get(name)
            if value is None and name in {"r", "red"}:
                value = rgb.get("r")
            if value is None and name in {"g", "green"}:
                value = rgb.get("g")
            if value is None and name in {"b", "blue"}:
                value = rgb.get("b")
            if not numeric_condition_matches(value, condition["operator"], condition["value"]):
                return False
        return True

    def color_matches_terms(self, detail: dict, tokens: list[str]) -> bool:
        haystack = normalize_query_text(
            " ".join(
                str(value)
                for value in [
                    visual_attribute_ai_text(detail),
                    detail.get("name"),
                    detail.get("type"),
                    *(f"{item.get('name')} {item.get('value')}" for item in detail.get("parameters") or []),
                ]
            )
        )
        query = " ".join(tokens)
        if matches_ai_query(haystack, query):
            return True
        return all(token in haystack for token in tokens)

    def color_match_summary(self, detail: dict, conditions: list[dict], tokens: list[str]) -> str:
        parts = []
        rgb = detail.get("rgb") or {}
        if rgb:
            parts.append(f"RGB {rgb.get('r')}, {rgb.get('g')}, {rgb.get('b')}")
        if detail.get("type"):
            parts.append(f"type: {detail.get('type')}")
        if conditions:
            parts.append(
                "conditions: " + ", ".join(f"{item['name']} {item['operator']} {item['value']}" for item in conditions)
            )
        if tokens:
            parts.append("matched terms: " + ", ".join(tokens[:8]))
        return " | ".join(parts) or "Matched visual attribute."

    def ai_catalog(self, mode: str = "", limit: int = 80, query: str = "") -> list[dict]:
        mode = mode.lower().strip()
        query = query.lower().strip()
        items = []
        if mode in ("", "products"):
            product_candidates = self.products
            if query:
                matched = []
                for product in self.products:
                    detail = self.product_detail(int(product["Id"]), compact=True)
                    haystack = " ".join(str(value) for value in [detail["name"], detail.get("unit"), *detail.get("categories", [])]).lower()
                    if query in haystack:
                        matched.append(product)
                product_candidates = matched or product_candidates
            for product in product_candidates[:limit]:
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
            element_candidates = self.elements
            if query:
                matched = []
                for element in self.elements:
                    detail = self.system_detail(int(element["Id"]), compact=True)
                    haystack = " ".join(str(value) for value in [detail["name"], *detail.get("preview_fields", [])]).lower()
                    if query in haystack:
                        matched.append(element)
                element_candidates = matched or element_candidates
            for element in element_candidates[:limit]:
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
            group_details = [self.color_group_detail(int(group["Id"]), compact=True) for group in self.color_groups]
            if query:
                matched_groups = [
                    detail for detail in group_details
                    if matches_ai_query(
                        f"{detail.get('name', '')} {detail.get('description', '')} "
                        f"{' '.join(item.get('name', '') for item in detail.get('sample_colors') or [])}",
                        query,
                    )
                ]
                group_details = matched_groups
            for detail in group_details[: max(8, limit // 3)]:
                sample_names = ", ".join(item.get("name", "") for item in detail.get("sample_colors") or [])
                group_fields = [
                    detail.get("description", ""),
                    f"items: {detail.get('count', 0)}",
                    f"sample visual attributes: {sample_names}" if sample_names else "",
                    f"used by products: {len(detail.get('used_by_products') or [])}",
                ]
                items.append(
                    {
                        "type": "visual_attribute_group",
                        "id": detail["id"],
                        "name": detail["name"],
                        "text": compact_text(" | ".join(item for item in group_fields if item), 1200),
                    }
                )
            color_details = [self.color_detail(int(color["Id"])) for color in self.colors]
            if query:
                matched_colors = [
                    detail for detail in color_details
                    if matches_ai_query(visual_attribute_ai_text(detail), query)
                ]
                color_details = matched_colors or color_details
            else:
                simple = [detail for detail in color_details if detail.get("type") != "advanced"]
                advanced = [detail for detail in color_details if detail.get("type") == "advanced"]
                color_details = [*simple[: limit // 2], *advanced[: limit // 2]]
            for detail in color_details[:limit]:
                items.append(
                    {
                        "type": "visual_attribute",
                        "id": detail["id"],
                        "name": detail["name"],
                        "text": compact_text(visual_attribute_ai_text(detail), 1600),
                    }
                )
        return items

    def list_products(self, query: str = "", filters: dict[str, list[str]] | None = None) -> dict:
        query = query.lower().strip()
        filters = filters or {}
        ownership_filter = filters.pop("_ownership", [])
        items = []
        query_records = []
        for product in self.products:
            detail = self.product_detail(int(product["Id"]), compact=True)
            haystack = " ".join(str(value) for value in [detail["name"], detail.get("unit"), detail.get("ownership", {}).get("label"), *detail.get("categories", [])]).lower()
            if query and query not in haystack:
                continue
            query_records.append(product)
            if ownership_filter and product_ownership(product)["key"] not in ownership_filter:
                continue
            if not matches_filters(product, self.product_attr_defs, filters):
                continue
            items.append(detail)
        return {"items": items, "filters": [product_ownership_filter(query_records, ownership_filter), *filter_catalog(query_records, self.product_attr_defs, filters)]}

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
            "ownership": product_ownership(product),
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
                "selection_attributes": selection_attributes(product, self.product_attr_defs),
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

    def edit_field_info(self, record_type: str, record_id: int, field_key: str) -> dict:
        record, attr_defs = self.edit_record(record_type, record_id)
        record_name = self.edit_record_name(record_type, record_id)
        if record_type == "color":
            parameter = self.find_color_parameter(record, field_key)
            value = first_parameter_value(parameter) if parameter else None
            parameter_name = parameter.get("parameterName") if parameter else field_key.split(":", 1)[-1]
            return {
                "record_name": record_name,
                "field_label": parameter_name,
                "current_value": value,
                "input_type": "text",
                "options": [],
                "field_type": "parameter",
            }
        attr = self.find_edit_attribute(record, attr_defs, field_key)
        attr_id = int(attr.get("AttributeId") or 0) if attr else 0
        attr_def = attr_defs.get(attr_id)
        return {
            "record_name": record_name,
            "field_label": attr_label(attr_def, attr_id) if attr_id else field_key,
            "current_value": first_value(attr, attr_def) if attr else None,
            "input_type": self.edit_input_type(attr_def),
            "options": [
                {"id": option.get("Id"), "label": str(option.get("OptionName") or option.get("OptionValue") or option.get("Id"))}
                for option in (attr_def or {}).get("AttributeOptions") or []
            ],
            "field_type": str((attr_def or {}).get("AttributeType") or ""),
            "attribute_id": attr_id,
        }

    def edit_record(self, record_type: str, record_id: int) -> tuple[dict, dict[int, dict]]:
        if record_type == "product":
            return self.product_index[record_id], self.product_attr_defs
        if record_type == "system":
            return self.element_index[record_id], self.element_attr_defs
        if record_type == "color":
            return self.color_index[record_id], {}
        raise ValueError("Unsupported editable record type")

    def edit_record_name(self, record_type: str, record_id: int) -> str:
        if record_type == "product":
            return product_name(self.product_index[record_id], self.product_attr_defs, self.product_name_attribute_ids)
        if record_type == "system":
            return element_name(self.element_index[record_id], self.element_attr_defs)
        if record_type == "color":
            return self.color_detail(record_id, compact=True)["name"]
        return str(record_id)

    def edit_input_type(self, attr_def: dict | None) -> str:
        attr_type = normalize_query_text((attr_def or {}).get("AttributeType"))
        if attr_type in {"select", "radio", "one of many", "one_of_many"}:
            return "select"
        if attr_type in {"checkboxes", "many of many", "many_of_many"}:
            return "multi"
        if attr_type in {"boolean", "checkbox"}:
            return "boolean"
        if attr_type in {"number", "decimal", "float", "double", "integer", "int"}:
            return "number"
        return "text"

    def find_color_parameter(self, color: dict, field_key: str) -> dict | None:
        parameter_name = field_key.split(":", 1)[-1] if field_key.startswith("parameter:") else field_key
        version = (color.get("dataVersions") or [{}])[-1]
        for parameter in version.get("parameters") or []:
            if normalize_query_text(parameter.get("parameterName")) == normalize_query_text(parameter_name):
                return parameter
        return None

    def find_edit_attribute(self, record: dict, attr_defs: dict[int, dict], field_key: str) -> dict | None:
        attrs = latest_attrs(record)
        row_match = re.search(r":row:([^:]+):column:(\d+)", field_key)
        if row_match:
            row = int(number_or_none(row_match.group(1)) or 0)
            attr_id = int(row_match.group(2))
            return next((attr for attr in attrs if int(attr.get("AttributeId") or 0) == attr_id and int(attr.get("RowI") or 0) == row), None)
        selector_match = re.match(r"selector:(\d+)", field_key)
        if selector_match:
            attr_id = int(selector_match.group(1))
            return next((attr for attr in attrs if int(attr.get("AttributeId") or 0) == attr_id and int(attr.get("ParentAttributeId") or 0) == 0), None)
        label = field_key.split(":", 1)[-1] if ":" in field_key else field_key
        label = label.rsplit(":", 1)[-1] if ":row:" in field_key else label
        for attr in attrs:
            attr_id = int(attr.get("AttributeId") or 0)
            attr_def = attr_defs.get(attr_id)
            if normalize_query_text(attr_label(attr_def, attr_id)) == normalize_query_text(label):
                return attr
        return None

    def corrected_payload(self, filename: str, patches: list[dict]) -> dict:
        payload = load_json(self.data_dir / filename)
        if filename == "products.json":
            records = payload.get("products") or []
            record_type = "product"
            attr_defs = self.product_attr_defs
        elif filename == "building_elements.json":
            records = payload.get("buildingElements") or []
            record_type = "system"
            attr_defs = self.element_attr_defs
        elif filename == "colors.json":
            records = payload.get("colors") or []
            record_type = "color"
            attr_defs = {}
        else:
            return payload
        by_id = {int(item["Id"]): item for item in records if item.get("Id") is not None}
        for patch in patches:
            if patch.get("record_type") != record_type:
                continue
            record = by_id.get(int(patch.get("record_id") or 0))
            if not record:
                continue
            self.apply_patch_to_record(record_type, record, attr_defs, patch)
        return payload

    def apply_patch_to_record(self, record_type: str, record: dict, attr_defs: dict[int, dict], patch: dict) -> None:
        if record_type == "color":
            parameter = self.find_color_parameter(record, str(patch.get("field_key") or ""))
            if parameter:
                apply_typed_value(parameter, patch.get("new_value"), None)
            return
        attr = self.find_edit_attribute(record, attr_defs, str(patch.get("field_key") or ""))
        if not attr:
            return
        attr_id = int(attr.get("AttributeId") or 0)
        apply_typed_value(attr, patch.get("new_value"), attr_defs.get(attr_id))

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
            name_item = values.get("attribute_name") or {}
            prefix_item = values.get("field_prefix") or {}
            suffix_item = values.get("field_suffix") or {}
            prefix = prefix_item.get("value") or ""
            suffix = suffix_item.get("value") or ""
            if not name and not value:
                continue
            features.append(
                {
                    "name": name or f"Cecha {row.get('row')}",
                    "prefix": prefix,
                    "value": value,
                    "suffix": suffix,
                    "row": row.get("row"),
                    "name_attribute_id": name_item.get("attribute_id"),
                    "value_attribute_id": value_item.get("attribute_id") if value_item else None,
                    "prefix_attribute_id": prefix_item.get("attribute_id"),
                    "suffix_attribute_id": suffix_item.get("attribute_id"),
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
            color_id = int(color["Id"])
            detail = self.color_detail(color_id, compact=True)
            if kind and detail.get("type") != kind:
                continue
            if query:
                full_detail = self.color_detail(color_id)
                haystack = f"{visual_attribute_ai_text(full_detail)} {' '.join(str(value) for value in full_detail.values())}".lower()
                if query not in haystack and not matches_ai_query(haystack, query):
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
                "selection_attributes": selection_attributes(element, self.element_attr_defs),
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

    def get_note(self, record_type: str, record_id: int, field_key: str = "") -> dict:
        key = self.note_key(record_type, record_id, field_key)
        for note in self.load()["notes"]:
            if note.get("key") == key:
                return note
        return {
            "key": key,
            "record_type": record_type,
            "record_id": record_id,
            "field_key": field_key,
            "field_label": "",
            "requires_correction": False,
            "resolved": False,
            "accepted": False,
            "attachments": [],
            "comment": "",
        }

    def save_note(self, payload: dict) -> dict:
        record_type = str(payload.get("record_type") or "")
        record_id = int(payload.get("record_id") or 0)
        field_key = str(payload.get("field_key") or "").strip()
        field_label = str(payload.get("field_label") or "").strip()
        key = self.note_key(record_type, record_id, field_key)
        now = int(time.time())
        existing = self.get_note(record_type, record_id, field_key)
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
            "field_key": field_key,
            "field_label": field_label,
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
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise ValueError("Attachment is too large")
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
        base = self.attachments_dir.resolve()
        path = (base / safe_filename(key) / safe_filename(filename)).resolve()
        if base not in path.parents:
            raise ValueError("Invalid attachment path")
        return path

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
            field_key = str(item.get("field_key") or "").strip()
            key = self.note_key(record_type, record_id, field_key)
            note = {
                "key": key,
                "record_type": record_type,
                "record_id": record_id,
                "record_name": str(item.get("record_name") or ""),
                "field_key": field_key,
                "field_label": str(item.get("field_label") or ""),
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
    def note_key(record_type: str, record_id: int, field_key: str = "") -> str:
        if field_key:
            return f"{record_type}:{record_id}:field:{safe_filename(field_key)}"
        return f"{record_type}:{record_id}"


class ReviewProjectStore:
    def __init__(self, data_dir: Path, notes: NotesStore) -> None:
        self.path = data_dir / PROJECT_FILE
        self.notes = notes

    def load(self) -> dict:
        payload = load_json_if_exists(self.path, {})
        created_at = int(payload.get("created_at") or time.time())
        return {
            "name": str(payload.get("name") or "BuildData-AI Data Validator review"),
            "client": str(payload.get("client") or ""),
            "reviewer": str(payload.get("reviewer") or ""),
            "status": str(payload.get("status") or "in_review"),
            "description": str(payload.get("description") or ""),
            "due_date": str(payload.get("due_date") or ""),
            "created_at": created_at,
            "updated_at": int(payload.get("updated_at") or created_at),
            "configured": self.path.exists(),
        }

    def save(self, payload: dict) -> dict:
        current = self.load()
        now = int(time.time())
        allowed_statuses = {"draft", "in_review", "client_review", "completed", "on_hold"}
        status = str(payload.get("status") or current["status"] or "in_review")
        if status not in allowed_statuses:
            status = "in_review"
        project = {
            **current,
            "name": str(payload.get("name") or current["name"]).strip() or "BuildData-AI Data Validator review",
            "client": str(payload.get("client") if payload.get("client") is not None else current["client"]).strip(),
            "reviewer": str(payload.get("reviewer") if payload.get("reviewer") is not None else current["reviewer"]).strip(),
            "status": status,
            "description": str(payload.get("description") if payload.get("description") is not None else current["description"]).strip(),
            "due_date": str(payload.get("due_date") if payload.get("due_date") is not None else current["due_date"]).strip(),
            "updated_at": now,
        }
        write_json(self.path, project)
        return project

    def summary(self, data: PimData | None, source_status: dict) -> dict:
        notes = self.notes.list_notes()["items"]
        return {
            "project": self.load(),
            "progress": project_progress(notes, data),
            "source": source_status,
            "storage": {
                "project_file": str(self.path),
                "notes_file": str(self.notes.path),
                "attachments_dir": str(self.notes.attachments_dir),
            },
        }

    def export_bundle(self, data: PimData | None, source_status: dict) -> dict:
        notes = self.notes.list_notes()["items"]
        return {
            "project": self.load(),
            "progress": project_progress(notes, data),
            "notes": notes,
            "source": source_status,
            "exported_at": int(time.time()),
        }

    def import_bundle(self, payload: dict, data: PimData | None, source_status: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("Expected review project JSON object")
        project = payload.get("project")
        if not isinstance(project, dict):
            project = payload
        saved_project = self.save(project)
        imported_notes = {"imported": 0, "total": len(self.notes.list_notes()["items"])}
        if isinstance(payload.get("notes"), list):
            imported_notes = self.notes.import_notes({"notes": payload["notes"]})
        result = self.summary(data, source_status)
        result["imported_notes"] = imported_notes
        result["project"] = saved_project
        return result


class ValidationModelStore:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / VALIDATION_MODEL_FILE

    def load(self) -> dict:
        payload = load_json_if_exists(self.path, {})
        model = normalize_validation_model(payload)
        model["configured"] = self.path.exists()
        model["model_file"] = str(self.path)
        return model

    def save(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("Expected validation model JSON object")
        if payload.get("url"):
            payload = self.model_from_url(str(payload.get("url") or ""))
        elif payload.get("text") and not payload.get("fields"):
            payload = self.model_from_text(str(payload.get("text") or ""), source=str(payload.get("source") or "manual text"))
        payload = normalize_validation_model({**payload, "updated_at": int(time.time())})
        write_json(self.path, payload)
        payload["configured"] = True
        payload["model_file"] = str(self.path)
        return payload

    def model_from_url(self, url: str) -> dict:
        body, content_type = fetch_remote_asset(url)
        text = body.decode("utf-8", errors="replace")
        if "html" in content_type:
            text = strip_html(text)
        model = self.model_from_text(text, source=url)
        model["url"] = url
        return model

    def model_from_text(self, text: str, source: str = "") -> dict:
        fields = []
        seen = set()
        for line in text.splitlines():
            raw = re.sub(r"\s+", " ", strip_html(line)).strip()
            if not raw or len(raw) > 180:
                continue
            if raw.startswith(("-", "*")):
                raw = raw[1:].strip()
            if ":" in raw:
                name, aliases_text = raw.split(":", 1)
                aliases = re.split(r"[,;/|]", aliases_text)
            else:
                name, aliases = raw, []
            key = normalize_query_text(name).replace(" ", "_")
            if not key or key in seen:
                continue
            seen.add(key)
            fields.append(
                {
                    "key": key,
                    "label": name.strip(),
                    "record_type": "product",
                    "locations": ["general", "features", "sot"],
                    "aliases": [name.strip(), *[alias.strip() for alias in aliases if alias.strip()]],
                }
            )
            if len(fields) >= 80:
                break
        return {"fields": fields, "source": source}


class DataPatchStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.path = data_dir / DATA_PATCHES_FILE

    def load(self) -> dict:
        payload = load_json_if_exists(self.path, {"patches": []})
        payload.setdefault("patches", [])
        return payload

    def list_patches(self) -> dict:
        patches = sorted(self.load()["patches"], key=lambda item: item.get("updated_at", 0), reverse=True)
        return {"items": patches, "patch_file": str(self.path)}

    def get_patch(self, record_type: str, record_id: int, field_key: str) -> dict:
        key = self.patch_key(record_type, record_id, field_key)
        for patch in self.load()["patches"]:
            if patch.get("key") == key:
                return patch
        return {
            "key": key,
            "record_type": record_type,
            "record_id": record_id,
            "field_key": field_key,
            "field_label": "",
            "original_value": None,
            "new_value": "",
            "active": False,
        }

    def save_patch(self, payload: dict, data: PimData) -> dict:
        record_type = str(payload.get("record_type") or "")
        record_id = int(payload.get("record_id") or 0)
        field_key = str(payload.get("field_key") or "").strip()
        if not record_type or not record_id or not field_key:
            raise ValueError("Patch requires record type, record id and field key")
        current = self.get_patch(record_type, record_id, field_key)
        field_info = data.edit_field_info(record_type, record_id, field_key)
        original_value = current.get("original_value")
        if original_value is None:
            original_value = field_info.get("current_value")
        now = int(time.time())
        patch = {
            **current,
            "key": self.patch_key(record_type, record_id, field_key),
            "record_type": record_type,
            "record_id": record_id,
            "record_name": str(payload.get("record_name") or field_info.get("record_name") or ""),
            "field_key": field_key,
            "field_label": str(payload.get("field_label") or field_info.get("field_label") or ""),
            "field_info": field_info,
            "original_value": original_value,
            "new_value": payload.get("new_value"),
            "comment": str(payload.get("comment") or "").strip(),
            "active": True,
            "updated_at": now,
        }
        data_payload = self.load()
        data_payload["patches"] = [item for item in data_payload["patches"] if item.get("key") != patch["key"]]
        data_payload["patches"].append(patch)
        write_json(self.path, data_payload)
        return patch

    def revert_patch(self, payload: dict) -> dict:
        record_type = str(payload.get("record_type") or "")
        record_id = int(payload.get("record_id") or 0)
        field_key = str(payload.get("field_key") or "").strip()
        key = str(payload.get("key") or self.patch_key(record_type, record_id, field_key))
        data_payload = self.load()
        patches = []
        reverted = None
        for patch in data_payload["patches"]:
            if patch.get("key") == key:
                patch = {**patch, "active": False, "reverted_at": int(time.time()), "updated_at": int(time.time())}
                reverted = patch
            patches.append(patch)
        data_payload["patches"] = patches
        write_json(self.path, data_payload)
        return reverted or {"key": key, "active": False}

    def active_patches(self, record_type: str | None = None) -> list[dict]:
        patches = [item for item in self.load()["patches"] if item.get("active")]
        if record_type:
            patches = [item for item in patches if item.get("record_type") == record_type]
        return patches

    @staticmethod
    def patch_key(record_type: str, record_id: int, field_key: str) -> str:
        return f"{record_type}:{record_id}:field:{safe_filename(field_key)}"


def project_progress(notes: list[dict], data: PimData | None) -> dict:
    totals = {
        "product": len(data.products) if data else 0,
        "system": len(data.elements) if data else 0,
        "visual_attribute": (len(data.colors) + len(data.color_groups)) if data else 0,
    }
    accepted = {key: 0 for key in totals}
    open_corrections = {key: 0 for key in totals}
    resolved_corrections = {key: 0 for key in totals}
    notes_count = {key: 0 for key in totals}
    ignored_notes = 0
    for note in notes:
        raw_type = str(note.get("record_type") or "")
        record_type = "visual_attribute" if raw_type in {"color", "color_group"} else raw_type
        if record_type not in totals:
            continue
        if data and not note_exists_in_data(note, data):
            ignored_notes += 1
            continue
        notes_count[record_type] += 1
        if note.get("accepted") and not note.get("field_key"):
            accepted[record_type] += 1
        if note.get("requires_correction"):
            if note.get("resolved"):
                resolved_corrections[record_type] += 1
            else:
                open_corrections[record_type] += 1
    total_records = sum(totals.values())
    total_accepted = sum(accepted.values())
    return {
        "totals": totals,
        "accepted": accepted,
        "open_corrections": open_corrections,
        "resolved_corrections": resolved_corrections,
        "notes": notes_count,
        "total_records": total_records,
        "total_accepted": total_accepted,
        "acceptance_percent": round((total_accepted / total_records) * 100, 1) if total_records else 0,
        "open_corrections_total": sum(open_corrections.values()),
        "resolved_corrections_total": sum(resolved_corrections.values()),
        "ignored_notes": ignored_notes,
        "notes_total": sum(notes_count.values()),
    }


def note_exists_in_data(note: dict, data: PimData) -> bool:
    try:
        record_id = int(note.get("record_id") or 0)
    except (TypeError, ValueError):
        return False
    record_type = str(note.get("record_type") or "")
    if record_type == "product":
        return record_id in data.product_index
    if record_type == "system":
        return record_id in data.element_index
    if record_type == "color":
        return record_id in data.color_index
    if record_type == "color_group":
        return record_id in data.color_group_index
    return False


class AiAgent:
    def __init__(self) -> None:
        self.base_url = (os.environ.get("DB_DATA_BROWSER_AI_URL") or os.environ.get("AI_AGENT_URL") or "").rstrip("/")
        self.model = os.environ.get("DB_DATA_BROWSER_AI_MODEL") or os.environ.get("OLLAMA_MODEL") or "qwen2.5-coder:14b"

    def ollama_root(self) -> str:
        return re.sub(r"/api/(generate|tags)$", "", self.base_url)

    def status(self) -> dict:
        if not self.base_url:
            return {"available": False, "message": "AI unavailable"}
        try:
            url = self.base_url if self.base_url.endswith("/api/tags") else f"{self.ollama_root()}/api/tags"
            request = Request(url, headers={"User-Agent": "DB-Data-Browser/1.0"})
            with urlopen(request, timeout=2) as response:
                response.read()
            return {"available": True, "message": "AI available"}
        except Exception:
            return {"available": False, "message": "AI unavailable"}

    def search(self, question: str, catalog: list[dict]) -> dict:
        status = self.status()
        fallback_records = related_ai_records(question, "", catalog)
        if not status.get("available"):
            answer = "AI is unavailable."
            if fallback_records:
                answer = "AI is unavailable. Matching records are listed below."
            return {"available": False, "answer": answer, "status": status, "related_records": fallback_records}
        prompt = (
            "Jestes agentem wyszukiwania w BuildData-AI Data Validator. Odpowiadaj po polsku. "
            "Znajdz pasujace produkty, elementy budowlane lub visual attributes na podstawie pól, opisów, cech i filtrów. "
            "Zwracaj konkretne nazwy, typ rekordu i ID. Dla kolorow i tekstur analizuj RGB, jasnosc, odcien, typ simple/advanced oraz mapy materialowe. "
            "Jesli powolujesz sie na rekord, podaj jego typ i ID. Jesli nie ma pewnosci, powiedz czego brakuje.\n\n"
            f"Pytanie użytkownika: {question}\n\n"
            f"Dane do przeszukania:\n{json.dumps(catalog, ensure_ascii=False)}"
        )
        endpoint = self.base_url if self.base_url.endswith("/api/generate") else f"{self.ollama_root()}/api/generate"
        body = json.dumps({"model": self.model, "prompt": prompt, "stream": False}, ensure_ascii=False).encode("utf-8")
        request = Request(endpoint, data=body, headers={"Content-Type": "application/json", "User-Agent": "DB-Data-Browser/1.0"})
        try:
            with urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except TimeoutError:
            answer = "AI did not answer in time. Try a shorter question or narrow the filters."
            if fallback_records:
                answer = "AI did not answer in time. Matching records are listed below."
            return {"available": True, "answer": answer, "timed_out": True, "status": status, "related_records": fallback_records}
        except socket.timeout:
            answer = "AI did not answer in time. Try a shorter question or narrow the filters."
            if fallback_records:
                answer = "AI did not answer in time. Matching records are listed below."
            return {"available": True, "answer": answer, "timed_out": True, "status": status, "related_records": fallback_records}
        except Exception:
            answer = "AI is temporarily unavailable. Try again later."
            if fallback_records:
                answer = "AI is temporarily unavailable. Matching records are listed below."
            return {"available": False, "answer": answer, "status": {"available": False, "message": "AI unavailable"}, "related_records": fallback_records}
        answer = extract_ai_answer(payload)
        if not answer:
            answer = "AI did not return a clear answer. Try asking more specifically."
            if fallback_records:
                answer = "Matching records are listed below."
        return {"available": True, "answer": answer, "status": status, "related_records": related_ai_records(question, answer, catalog)}


class DataStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.data: PimData | None = None
        self.notes = NotesStore(data_dir)
        self.project = ReviewProjectStore(data_dir, self.notes)
        self.validation_model = ValidationModelStore(data_dir)
        self.patches = DataPatchStore(data_dir)
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
        skipped = []
        for filename, content in uploaded_files:
            safe_name = Path(filename).name
            if safe_name not in REQUIRED_FILES:
                skipped.append(safe_name)
                continue
            if not content:
                skipped.append(safe_name)
                continue
            (self.data_dir / safe_name).write_bytes(content)
            saved.append(safe_name)
        self.reload()
        status = self.status()
        status["saved"] = saved
        status["skipped"] = skipped
        return status

    def clear(self, reset_review: bool = False) -> dict:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for filename in REQUIRED_FILES:
            path = self.data_dir / filename
            if path.exists():
                path.unlink()
        if reset_review:
            for filename in (NOTES_FILE, PROJECT_FILE, VALIDATION_MODEL_FILE, DATA_PATCHES_FILE):
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

    def corrected_payload(self, filename: str) -> dict:
        if not self.data:
            raise ValueError("Data source is not ready")
        return self.data.corrected_payload(filename, self.patches.active_patches())

    def corrected_zip(self) -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for filename in ("products.json", "building_elements.json", "colors.json"):
                path = self.data_dir / filename
                if not path.exists():
                    continue
                body = json.dumps(self.corrected_payload(filename), ensure_ascii=False, indent=2).encode("utf-8")
                archive.writestr(filename, body)
            archive.writestr("browser_data_patches.json", json.dumps(self.patches.load(), ensure_ascii=False, indent=2).encode("utf-8"))
        return output.getvalue()


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
            try:
                parsed = urlparse(self.path)
                if parsed.path == "/api/source/upload":
                    self.handle_upload()
                    return
                if parsed.path == "/api/source/clear":
                    self.send_json(store.clear(bool(read_json_body(self).get("reset_review"))))
                    return
                if parsed.path == "/api/notes":
                    self.send_json(store.notes.save_note(read_json_body(self)))
                    return
                if parsed.path == "/api/project":
                    store.project.save(read_json_body(self))
                    self.send_json(store.project.summary(store.data, store.status()))
                    return
                if parsed.path == "/api/project/import":
                    self.send_json(store.project.import_bundle(read_json_body(self), store.data, store.status()))
                    return
                if parsed.path == "/api/notes/import":
                    self.send_json(store.notes.import_notes(read_json_body(self)))
                    return
                if parsed.path == "/api/records/accept":
                    self.send_json(store.notes.bulk_accept(read_json_body(self)))
                    return
                if parsed.path == "/api/patches":
                    self.send_json(store.patches.save_patch(read_json_body(self), self.ready_data()))
                    return
                if parsed.path == "/api/patches/revert":
                    self.send_json(store.patches.revert_patch(read_json_body(self)))
                    return
                if parsed.path == "/api/validation/model":
                    self.send_json(store.validation_model.save(read_json_body(self)))
                    return
                if parsed.path == "/api/validation/query":
                    payload = read_json_body(self)
                    question = str(payload.get("question") or "")
                    result = self.ready_data().data_query(question, store.validation_model.load())
                    self.send_json(result or {"validation": False, "answer": "No validation rule matched this question.", "findings": [], "related_records": []})
                    return
                if parsed.path == "/api/ai/search":
                    payload = read_json_body(self)
                    question = str(payload.get("question") or "")
                    mode = str(payload.get("mode") or "")
                    data_result = self.ready_data().data_query(question, store.validation_model.load())
                    if data_result:
                        self.send_json(data_result)
                        return
                    self.send_json(store.ai.search(question, self.ready_data().ai_catalog(mode=mode, query=question)))
                    return
                self.send_json({"error": "Not found"}, status=404)
            except ValueError as error:
                self.send_json({"error": str(error)}, status=400)
            except Exception as error:
                self.send_json({"error": str(error)}, status=502)

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
                elif parsed.path == "/api/validation/model":
                    payload = store.validation_model.load()
                elif parsed.path == "/api/project":
                    payload = store.project.summary(store.data, store.status())
                elif parsed.path == "/api/project/export":
                    self.send_download(store.project.export_bundle(store.data, store.status()), "db_data_browser_review_project.json")
                    return
                elif parsed.path == "/api/notes":
                    payload = store.notes.list_notes()
                elif parsed.path == "/api/patches":
                    payload = store.patches.list_patches()
                elif parsed.path.startswith("/api/patches/"):
                    _, record_type, record_id = parsed.path.rsplit("/", 2)
                    field_key = qs.get("field_key", [""])[0]
                    payload = store.patches.get_patch(unquote(record_type), int(unquote(record_id)), field_key)
                    if not payload.get("active"):
                        field_info = self.ready_data().edit_field_info(unquote(record_type), int(unquote(record_id)), field_key)
                        payload = {**payload, "field_info": field_info, "field_label": field_info.get("field_label"), "original_value": field_info.get("current_value")}
                elif parsed.path == "/api/notes/export":
                    self.send_download(store.notes.load(), "browser_corrections.json")
                    return
                elif parsed.path == "/api/notes/export.csv":
                    self.send_csv(notes_as_csv(store.notes.list_notes()["items"]), "browser_corrections.csv")
                    return
                elif parsed.path == "/api/export/products.json":
                    self.send_download(store.corrected_payload("products.json"), "products.json")
                    return
                elif parsed.path == "/api/export/building_elements.json":
                    self.send_download(store.corrected_payload("building_elements.json"), "building_elements.json")
                    return
                elif parsed.path == "/api/export/colors.json":
                    self.send_download(store.corrected_payload("colors.json"), "colors.json")
                    return
                elif parsed.path == "/api/export/corrected-data.zip":
                    self.send_binary(store.corrected_zip(), "application/zip", "corrected-data.zip")
                    return
                elif parsed.path.startswith("/api/notes/attachment/"):
                    _, key, filename = parsed.path.rsplit("/", 2)
                    self.send_file(store.notes.attachment_path(unquote(key), unquote(filename)))
                    return
                elif parsed.path.startswith("/api/notes/"):
                    _, record_type, record_id = parsed.path.rsplit("/", 2)
                    payload = store.notes.get_note(unquote(record_type), int(unquote(record_id)), qs.get("field_key", [""])[0])
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
            content_length = int(self.headers.get("Content-Length") or "0")
            if content_length > MAX_UPLOAD_BYTES:
                self.send_json({"error": "Upload is too large"}, status=413)
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

        def send_binary(self, body: bytes, content_type: str, filename: str):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
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
    print(f"BuildData-AI Data Validator: http://{args.host}:{args.port}")
    print(f"Data source: {data_dir}")
    server.serve_forever()


if __name__ == "__main__":
    main()
