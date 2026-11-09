"""将 xlsx / docx / pdf 解析为可按字符分页的文本。"""
from __future__ import annotations

import asyncio
from pathlib import Path

from game_agent.agents.tools import ToolContext, ToolError
from game_agent.agents.tools.native import _resolve

MAX_PARSE_CHARS = 6000  # 为分页提示留空间，避免 execute_tool 的 8000 字符截断。
_XLSX_MAX_ROWS = 2000


def _import_or_hint(mod_name: str, pip_name: str):
    try:
        return __import__(mod_name)
    except ImportError as e:
        raise ToolError(f"后端缺少解析库：{e.name or mod_name}。修复：在 venv 执行 pip install {pip_name}")


def _cell(v) -> str:
    if v is None:
        return ""
    return str(v).replace("\t", " ").replace("\r", " ").replace("\n", "⏎")


def _positive_int(raw, default: int, name: str) -> int:
    if raw is None:
        return default
    try:
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            raise ValueError
        value = int(raw)
        if value < 1:
            raise ValueError
    except ValueError:
        raise ToolError(f"{name} 必须是大于等于 1 的整数")
    return value


def _page_text(text: str, args: dict, next_section: str = "") -> str:
    """offset 按所选文本的 Unicode 码点计数（1 基），含格式标签、Tab 和换行，不含分页头尾。"""
    offset = _positive_int(args.get("offset"), 1, "offset")
    limit = min(_positive_int(args.get("limit"), MAX_PARSE_CHARS, "limit"), MAX_PARSE_CHARS)
    total = len(text)
    if offset > total:
        raise ToolError(f"offset={offset} 超出所选文本末尾（共 {total} 字符），更换选区后请用 offset=1")
    end = min(offset - 1 + limit, total)
    header = f"[字符 {offset}-{end} / 共 {total} 字符]"
    if end < total:
        footer = f"[还有文本；保持其他参数不变，用 offset={end + 1} 继续读]"
    else:
        footer = "[本段文本已读完" + (f"；{next_section}" if next_section else "") + "]"
    return f"{header}\n{text[offset - 1:end]}\n{footer}"


async def parse_document(args: dict, t: ToolContext) -> str:
    path = _resolve(t.workspace, args["path"], t.permission_scope)
    if not path.exists():
        raise ToolError(f"文件不存在：{args['path']}")
    if path.is_dir():
        raise ToolError(f"这是目录：{args['path']}")
    suffix = path.suffix.lower()
    if suffix in (".xls", ".doc"):
        raise ToolError(f"旧版 Office 格式（{suffix}）解析不可靠，请先另存为对应的 .xlsx/.docx 再解析")
    if suffix == ".xlsx":
        return await asyncio.to_thread(_parse_xlsx, path, args)
    if suffix == ".docx":
        return await asyncio.to_thread(_parse_docx, path, args)
    if suffix == ".pdf":
        return await asyncio.to_thread(_parse_pdf, path, args)
    raise ToolError(f"不支持的格式：{suffix}（只支持 .xlsx / .docx / .pdf；csv/txt 直接用 read_file）")


def _parse_xlsx(path: Path, args: dict) -> str:
    openpyxl = _import_or_hint("openpyxl", "openpyxl")
    max_rows = min(_positive_int(args.get("max_rows"), 200, "max_rows"), _XLSX_MAX_ROWS)
    start_row = _positive_int(args.get("start_row"), 1, "start_row")
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"打开 xlsx 失败（文件可能损坏或加了密码）：{type(e).__name__}: {e}")
    try:
        sheet_names = list(wb.sheetnames)
        idx = _sheet_index(args.get("sheet"), sheet_names)
        ws = wb[sheet_names[idx]]
        total = ws.max_row if isinstance(ws.max_row, int) else None
        rows: list[str] = []
        has_more = False
        for row_no, row in enumerate(ws.iter_rows(min_row=start_row, values_only=True), start_row):
            if len(rows) >= max_rows:
                has_more = True
                break
            cells = [_cell(v) for v in row]
            while cells and cells[-1] == "":
                cells.pop()
            rows.append(f"[第 {row_no} 行]\t" + "\t".join(cells))
    finally:
        wb.close()
    n_total = f"，共约 {total} 行" if total else ""
    header = (f"[工作表 {idx + 1}/{len(sheet_names)}：{sheet_names[idx]}{n_total}，"
              f"全部工作表：{', '.join(sheet_names)}]")
    body = "\n".join(rows) if rows else "（所选行区间没有数据行）"
    next_section = (f"保持 sheet/max_rows/limit 不变，用 start_row={start_row + max_rows}、offset=1 继续读"
                    if has_more else "")
    return _page_text(f"{header}\n{body}", args, next_section)


def _sheet_index(want, sheet_names: list[str]) -> int:
    idx = _positive_int(want, 1, "sheet") - 1
    if idx >= len(sheet_names):
        raise ToolError(f"sheet={idx + 1} 超出范围，共 {len(sheet_names)} 个工作表：{', '.join(sheet_names)}")
    return idx


def _parse_docx(path: Path, args: dict) -> str:
    docx_mod = _import_or_hint("docx", "python-docx")
    from docx.text.paragraph import Paragraph

    try:
        doc = docx_mod.Document(str(path))
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"打开 docx 失败（文件可能损坏或加了密码）：{type(e).__name__}: {e}")
    parts: list[str] = []
    table_no = 0
    for block in doc.iter_inner_content():
        if isinstance(block, Paragraph):
            if block.text.strip():
                parts.append(block.text)
        else:
            table_no += 1
            parts.append(f"[表格 {table_no}：{len(block.rows)} 行 × {len(block.columns)} 列]")
            for row in block.rows:
                parts.append("\t".join(_cell(c.text) for c in row.cells))
    return _page_text("\n".join(parts) or "（文档为空）", args)


def _parse_pdf(path: Path, args: dict) -> str:
    pypdf = _import_or_hint("pypdf", "pypdf")
    page = _positive_int(args.get("page"), 1, "page")
    try:
        with path.open("rb") as stream:
            reader = pypdf.PdfReader(stream)
            if reader.is_encrypted:
                try:
                    unlocked = reader.decrypt("")
                except Exception:  # noqa: BLE001
                    raise ToolError("PDF 有密码，无法解析")
                if not unlocked:
                    raise ToolError("PDF 有密码，无法解析")
            n_pages = len(reader.pages)
            if page > n_pages:
                raise ToolError(f"page={page} 超出范围，共 {n_pages} 页")
            try:
                text = (reader.pages[page - 1].extract_text() or "").strip()
            except Exception as e:  # noqa: BLE001
                raise ToolError(f"第 {page} 页文本提取失败：{type(e).__name__}: {e}")
        title = f"[第 {page} 页 / 共 {n_pages} 页]"
        if not text:
            text = "没有可提取的文本（可能是空白页或扫描件/图片页；扫描件需要 OCR）"
        next_section = f"用 page={page + 1}、offset=1 读下一页" if page < n_pages else ""
        return _page_text(f"{title}\n--- 第 {page} 页 ---\n{text}", args, next_section)
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"解析 PDF 失败：{type(e).__name__}: {e}")
