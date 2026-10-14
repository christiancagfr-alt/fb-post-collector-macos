"""Find exact post IDs in candidate Page timelines without editing input sheets."""
import base64
import logging
import re
import time
from urllib.parse import parse_qs, urlparse

from . import db
from .fields import assign_default_write_columns, column_to_index, field_write_pairs, index_to_column
from .services.errors import SheetWriteError, UserVisibleError
from .services.scraper import FacebookScraper, close_driver, ensure_logged_in, make_driver, page_state, validate_url
from .services.sheets import SheetsClient, extract_spreadsheet_id
from .services.task_control import TaskCancelled, cancellable_sleep, check_cancelled, configure

HEADERS = ["查询贴文 ID", "来源专页链接", "来源专页名称", "查找状态"]


def validate_lookup_config(project, fields):
    names = [str(project.get(key) or "").strip() for key in
             ("page_source_worksheet_name", "post_id_worksheet_name", "page_output_worksheet_name")]
    if any(not name or len(name) > 100 or re.search(r"[\[\]:*?/\\]", name) for name in names):
        raise ValueError("请填写三个有效的分表名称（最长 100 字，不能含 []:*?/\\）。")
    if len({name.casefold() for name in names}) != 3:
        raise ValueError("专页列表、贴文 ID 和采集结果必须使用三个不同的分表，避免覆盖输入。")
    enabled = [dict(field) for field in fields if field.get("enabled")]
    assign_default_write_columns(enabled, "E")
    columns = [column_to_index(field.get("write_column")) for field in enabled]
    if any(column < 5 or column > 702 for column in columns) or len(columns) != len(set(columns)):
        raise ValueError("采集字段写入列必须在 E–ZZ 之间且不能重复；A–D 为固定查找记录。")


def numeric_profile_id(url):
    parsed = urlparse(url)
    value = (parse_qs(parsed.query).get("id") or [""])[0]
    if re.fullmatch(r"[0-9]+", value):
        return value
    path = parsed.path.strip("/")
    return path if re.fullmatch(r"[0-9]+", path) else ""


def verified_post_id(story, profile_id):
    """Use returned outer-post identity and actor, never a requested URL as proof."""
    if str(story.get("author_id") or "") != profile_id:
        return ""
    raw = str(story.get("lookup_post_id", story.get("post_id")) or "")
    if re.fullmatch(r"[0-9]+", raw):
        return raw
    composite = re.fullmatch(r"([0-9]+)_([0-9]+)", raw)
    if composite:
        return composite[2] if composite[1] == profile_id else ""
    try:
        decoded = base64.b64decode(raw, validate=True).decode("utf-8")
    except (ValueError, UnicodeError):
        decoded = ""
    match = re.fullmatch(r"S:_I([0-9]+):VK:([0-9]+)", decoded)
    if match:
        return match[2] if match[1] == profile_id else ""
    # Timeline story IDs can be opaque; only accept the returned outer permalink.
    url = str(story.get("lookup_post_url", story.get("post_url")) or "")
    try:
        validate_url(url)
    except UserVisibleError:
        return ""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if parsed.path.rstrip("/") in ("/story.php", "/permalink.php"):
        candidate = (query.get("story_fbid") or [""])[0]
        owner = (query.get("id") or [""])[0]
        return candidate if owner == profile_id and re.fullmatch(r"[0-9]+", candidate) else ""
    match = re.fullmatch(r"/([0-9]+)/(?:posts|permalink)/([0-9]+)/?", parsed.path)
    return match[2] if match and match[1] == profile_id else ""


def run_lookup_project(project, run_id, counts):
    # Import here to keep the dispatcher free of a circular module import.
    from .runner import enabled_fields, note_progress, project_for_account, should_stop, wait_until_runnable

    fields = enabled_fields(project)
    validate_lookup_config(project, fields)
    sheets = SheetsClient()
    scraper = FacebookScraper()
    book = project.get("spreadsheet_id") or extract_spreadsheet_id(project.get("spreadsheet_url", ""))
    # Existing SheetsClient methods surround names with quotes in A1 notation.
    source, ids_sheet, output = [project[key].strip().replace("'", "''") for key in
                                ("page_source_worksheet_name", "post_id_worksheet_name", "page_output_worksheet_name")]
    limit = min(1000, max(1, int(project.get("lookup_page_limit") or 100)))
    driver = None

    def report(message):
        note_progress(run_id, counts, message)

    def boundary():
        if not wait_until_runnable(run_id):
            raise TaskCancelled()
        check_cancelled()

    configure(stop=lambda: should_stop(run_id), report=report)
    try:
        boundary()
        report("读取贴文 ID 和候选专页列表")
        id_rows = sheets.read_column(book, ids_sheet, "A", 2)
        pages = list(dict.fromkeys(row["url"] for row in sheets.read_column(book, source, "A", 2) if row["url"]))
        if not pages:
            raise UserVisibleError("专页列表 A 列没有链接，请从第 2 行开始填写。")
        targets = {}
        for row in id_rows:
            value = str(row["url"]).strip()
            if not value:
                continue
            if not re.fullmatch(r"[0-9]+", value):
                raise UserVisibleError(f"贴文 ID 分表第 {row['row_number']} 行不是完整数字 ID，请将单元格设为纯文本后重新填写。")
            targets.setdefault(value, row["row_number"])
        if not targets:
            raise UserVisibleError("贴文 ID 分表 A 列没有 ID，请从第 2 行开始填写。")
        last_column = max([4] + [column_to_index(field["write_column"]) for field in fields])
        boundary()
        sheets.ensure_worksheet(book, project["page_output_worksheet_name"].strip(), last_column)
        header_pairs = list(zip(("A", "B", "C", "D"), HEADERS)) + [(field["write_column"], field["field_label"]) for field in fields]
        existing = sheets.read_range_rows(book, output, "A", index_to_column(last_column), 1)
        header = existing[0]["values"] if existing and existing[0]["row_number"] == 1 else []
        if existing:
            # Refuse a populated, unrelated output sheet or changed column mapping.
            for column, label in header_pairs:
                index = column_to_index(column) - 1
                if index >= len(header) or header[index] != label:
                    raise UserVisibleError("结果分表已有数据，但表头与当前配置不一致。请换一个新的结果分表名称，避免覆盖。")
        boundary()
        sheets.write_mapped_values(book, output, 1, header_pairs)
        rows_by_id = {}
        completed = set()
        for row in existing:
            values = row["values"]
            if row["row_number"] < 2 or not values:
                continue
            post_id = str(values[0])
            rows_by_id.setdefault(post_id, row["row_number"])
            if len(values) > 3 and values[3] == "成功":
                completed.add(post_id)
        counts["total"] = len(targets)
        counts["skipped"] = len(set(targets) & completed)
        pending = {key: row for key, row in targets.items() if key not in completed}
        next_row = max([1] + [row["row_number"] for row in existing]) + 1

        def write_result(post_id, status, page_url="", page_name="", values=None):
            nonlocal next_row
            boundary()
            row_number = rows_by_id.get(post_id)
            if row_number is None:
                row_number = next_row
                rows_by_id[post_id] = row_number
                next_row += 1
            # USER_ENTERED requires a leading apostrophe to preserve long numeric IDs.
            pairs = [("A", "'" + post_id), ("B", page_url), ("C", page_name), ("D", status)]
            collected = dict(values or {})
            for key in ("post_id", "page_id", "author_id"):
                if str(collected.get(key) or "").isdigit():
                    collected[key] = "'" + str(collected[key])
            pairs += field_write_pairs(fields, collected)
            sheets.write_mapped_values(book, output, row_number, pairs)

        incomplete = False
        found_errors = {}
        for page_index, page_url in enumerate(pages):
            if not pending:
                break
            boundary()
            active_project = project_for_account(project, page_index)
            report(f"检查专页 {page_index + 1}/{len(pages)}，待匹配 {len(pending)} 个 ID：{page_url}")
            try:
                validate_url(page_url)
                profile_id = numeric_profile_id(page_url)
                if not profile_id:
                    raise UserVisibleError("专页链接请使用带数字 ID 的 profile.php?id=… 或 /数字ID 格式。")
                driver = make_driver(active_project)
                ensure_logged_in(driver)
                driver.get(page_url)
                cancellable_sleep(2)
                page_state(driver.current_url, driver.title, driver.page_source, driver=driver)
                cursor = None
                seen_cursors = set()
                before_time = int(time.time())
                for page_number in range(1, limit + 1):
                    boundary()
                    report(f"专页 {page_index + 1}/{len(pages)}，读取第 {page_number}/{limit} 页，待匹配 {len(pending)} 个 ID")
                    stories, info = scraper.fetch_profile_timeline(driver, active_project, profile_id, 0, before_time, cursor)
                    if not stories:
                        # Empty/error payload is not proof that all posts were searched.
                        incomplete = True
                        break
                    for story in stories:
                        boundary()
                        post_id = verified_post_id(story, profile_id)
                        if post_id not in pending:
                            continue
                        try:
                            values = dict(story, post_id=post_id, page_id=profile_id)
                            post_url = f"https://www.facebook.com/{profile_id}/posts/{post_id}"
                            values["post_url"] = post_url
                            validate_url(post_url)
                            report(f"已核实贴文 {post_id} 的来源专页，开始采集内容")
                            # DOM media fallback must operate on the matched post, not the Page timeline.
                            driver.get(post_url)
                            page_state(driver.current_url, driver.title, driver.page_source, driver=driver)
                            result = scraper.enrich_prefetched(values, post_url, driver, active_project)
                            boundary()
                            write_result(post_id, "成功", page_url, story.get("author_name", ""), result["values"])
                        except SheetWriteError:
                            raise
                        except Exception:
                            logging.getLogger(__name__).exception("Post lookup enrichment failed")
                            found_errors[post_id] = (page_url, story.get("author_name", ""))
                            report(f"贴文 {post_id} 已找到，但内容采集失败，可再次运行重试")
                            continue
                        source_row = pending.pop(post_id)
                        counts["success"] += 1
                        result["raw"].update({"matched_page_url": page_url, "matched_page_id": profile_id})
                        result["written"] = result["values"]
                        db.add_row_run(run_id, source_row, post_url, result)
                        report(f"贴文 {post_id} 已写入结果分表")
                    if not pending:
                        break
                    next_cursor = info.get("end_cursor")
                    if info.get("has_next_page") is False:
                        break
                    if not next_cursor or next_cursor in seen_cursors or page_number == limit:
                        incomplete = True
                        break
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
            except SheetWriteError:
                raise
            except Exception as exc:
                incomplete = True
                logging.getLogger(__name__).exception("Candidate Page lookup failed")
                detail = exc.user_message if isinstance(exc, UserVisibleError) else "读取失败，请查看软件日志"
                report(f"专页 {page_index + 1} 未完成检查：{detail}")
            finally:
                close_driver(driver)
                driver = None
        for post_id in pending:
            if post_id in found_errors:
                page_url, page_name = found_errors[post_id]
                status = "已找到，内容采集失败（可重试）"
                write_result(post_id, status, page_url, page_name)
            else:
                status = "未完成核实：部分专页访问失败或达到搜索上限" if incomplete else "未找到：当前账号可见的专页贴文中无匹配 ID"
                write_result(post_id, status)
            counts["failed"] += 1
            db.add_row_run(run_id, pending[post_id], "", {"post_id": post_id, "status": "failed", "error_message": status})
        report(f"查找完成：成功 {counts['success']}，未成功 {counts['failed']}，跳过已成功 {counts['skipped']}")
    except TaskCancelled:
        report("查找已停止，保留已写入结果；下次运行跳过成功记录")
    finally:
        close_driver(driver)
        configure()
