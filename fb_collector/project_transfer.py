"""Portable configuration only: no credentials, machine paths or run history."""
from .fields import DEFAULT_FIELDS, normalize_column

PROJECT_KEYS = (
    "name", "project_type", "spreadsheet_url", "spreadsheet_id", "worksheet_name",
    "page_source_worksheet_name", "page_output_worksheet_name", "page_start_at", "page_end_at",
    "link_column", "header_row", "start_row", "end_row", "max_workers", "write_start_column",
    "processed_log_column", "skip_existing_write_data", "rerun_policy", "ocr_languages",
    "whisper_language", "audio_min_like_count",
)


def export_project(project):
    return {"format": "fb-post-collector-project", "version": 1,
            "project": {key: project[key] for key in PROJECT_KEYS if key in project},
            "fields": [{key: item.get(key) for key in ("field_key", "field_label", "enabled", "write_column")}
                       for item in project.get("fields", [])]}


def validate_import(payload):
    if not isinstance(payload, dict) or payload.get("format") != "fb-post-collector-project" or payload.get("version") != 1:
        raise ValueError("不是受支持的项目配置文件")
    source = payload.get("project")
    fields = payload.get("fields")
    if not isinstance(source, dict) or not isinstance(fields, list) or len(fields) > 100:
        raise ValueError("项目或字段格式错误")
    data = {key: source[key] for key in PROJECT_KEYS if key in source}
    if any(not isinstance(value, (str, int, bool)) for value in data.values()):
        raise ValueError("项目配置值必须是文字或数字")
    if data.get("project_type") not in ("post", "page"):
        raise ValueError("不支持的项目类型")
    if data.get("rerun_policy", "skip_done") not in ("skip_done", "overwrite", "retry_failed"):
        raise ValueError("不支持的重复运行策略")
    allowed = {item[0] for item in DEFAULT_FIELDS}
    seen = set()
    cleaned = []
    for item in fields:
        if not isinstance(item, dict) or item.get("field_key") not in allowed or item["field_key"] in seen:
            raise ValueError("存在无效或重复字段")
        seen.add(item["field_key"])
        if item.get("enabled") not in (True, False, 0, 1):
            raise ValueError("字段开关格式错误")
        label = item.get("field_label") or item["field_key"]
        column = item.get("write_column") or ""
        if not isinstance(label, str) or not isinstance(column, str):
            raise ValueError("字段名称或列号格式错误")
        cleaned.append({"field_key": item["field_key"], "field_label": label[:200],
                        "enabled": bool(item.get("enabled")), "write_column": normalize_column(column)})
    data["name"] = str(data.get("name") or "导入项目")[:200] + "（导入）"
    data["skip_existing_write_data"] = "on" if data.get("skip_existing_write_data") else ""
    return data, cleaned
