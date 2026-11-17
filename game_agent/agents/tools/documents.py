"""将 xlsx / docx / pdf 解析为可按字符分页的文本。

docx 除了正文段落/表格，还会把「文本以外的内容」变成模型能用的东西：
- 图片：导出到工作区 `.trace/docx-assets/<文档名>-<hash>/`，正文原位置插 `[图片 k：路径]` 标记，
  模型可用 view_image / view_images 真正看图（图片本身不进文本）。
- 内嵌对象：内嵌 Excel 直接展开成 TSV 行；内嵌 Word/PDF/其它二进制给出提示，需另存后单独解析。
- 图表：从 chart 部件的缓存数据里取系列名/类别/数值，不依赖 Excel 原件。
- 文本框、批注、页眉页脚：按原文顺序或末尾分段给出（旧实现会静默丢掉这些）。
"""
from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import Path

from game_agent.agents.tools import ToolContext, ToolError
from game_agent.agents.tools.native import _resolve

MAX_PARSE_CHARS = 6000  # 为分页提示留空间，避免 execute_tool 的 8000 字符截断。
_XLSX_MAX_ROWS = 2000
_DOCX_IMAGE_MAX = 60          # 单份文档最多导出这么多张图，防止百页图册把工作区灌满
_DOCX_EMBED_MAX_ROWS = 200    # 每个内嵌表格默认展开多少行

_NSS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "o": "urn:schemas-microsoft-com:office:office",
    "v": "urn:schemas-microsoft-com:vml",
}

_IMG_CT_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/gif": ".gif",
    "image/bmp": ".bmp", "image/tiff": ".tif", "image/webp": ".webp",
    "image/x-emf": ".emf", "image/emf": ".emf", "image/x-wmf": ".wmf", "image/wmf": ".wmf",
}
_XLSX_CONTENT_TYPES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel.sheet.macroenabled.12",
    "application/vnd.ms-excel",
}


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
        return await asyncio.to_thread(_parse_docx, path, args, t.workspace)
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


# ---------- docx：文本以外的内容 ----------


def _rel_part(part, rid):
    """按关系 id 取目标部件；外部链接/缺失关系返回 None。"""
    try:
        rel = part.rels[rid]
    except (KeyError, AttributeError):
        return None
    if getattr(rel, "is_external", False):
        return None
    return getattr(rel, "target_part", None)


def _image_dims(blob: bytes) -> str:
    try:
        from PIL import Image
        with Image.open(io.BytesIO(blob)) as im:
            return f"{im.width}×{im.height}"
    except Exception:  # noqa: BLE001 - EMF/WMF 等 Pillow 读不了的格式，尺寸信息非必需
        return ""


class _DocxAssets:
    """把 docx 里的图片落成工作区文件，并把图片/图表/内嵌对象转成一行的标记文本。"""

    def __init__(self, doc_path: Path, doc_part, workspace) -> None:
        self.doc_path = doc_path
        self.doc_part = doc_part
        self.workspace = Path(workspace) if workspace else None
        self._dir: Path | None = None
        self._exported: dict[str, tuple[str, str]] = {}   # partname -> (相对路径, 标记前缀)
        self._count = 0
        self._skipped = 0
        self._embedded_seen: set[str] = set()
        self._chart_no = 0
        self._textbox_no = 0

    # 目录与相对路径
    def _target_dir(self) -> Path:
        if self._dir is None:
            stamp = hashlib.sha256(str(self.doc_path).encode("utf-8")).hexdigest()[:8]
            base = self.workspace or self.doc_path.parent
            self._dir = base / ".trace" / "docx-assets" / f"{self.doc_path.stem}-{stamp}"
            self._dir.mkdir(parents=True, exist_ok=True)
        return self._dir

    def _display(self, p: Path) -> str:
        if self.workspace is not None:
            try:
                return p.relative_to(self.workspace).as_posix()
            except ValueError:
                pass
        return str(p)

    # 图片
    def _export_image(self, part) -> tuple[str, str] | None:
        partname = str(getattr(part, "partname", "") or "")
        if partname and partname in self._exported:
            return self._exported[partname]
        if self._count >= _DOCX_IMAGE_MAX:
            self._skipped += 1
            return None
        blob = getattr(part, "blob", b"")
        if not blob:
            return None
        ext = _IMG_CT_EXT.get(getattr(part, "content_type", ""), "")
        name = Path(partname).name or f"image{self._count + 1}"
        if not ext:
            ext = Path(name).suffix.lower() or ".bin"
        out = self._target_dir() / (Path(name).stem + ext)
        data = blob
        if ext in (".emf", ".wmf"):   # 矢量图 view_image 看不了，尽量先转成 PNG
            try:
                from PIL import Image
                with Image.open(io.BytesIO(blob)) as im:
                    buf = io.BytesIO()
                    im.convert("RGB").save(buf, "PNG")
                out, ext, data = out.with_suffix(".png"), ".png", buf.getvalue()
            except Exception:  # noqa: BLE001 - 转不了就保留原文件并如实标注
                pass
        try:
            out.write_bytes(data)
        except OSError:
            return None
        self._count += 1
        dims = _image_dims(data)
        meta = "，".join(x for x in (dims, ext.lstrip(".").upper()) if x)
        shown = self._display(out)
        viewable = out.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
        label = f"图片 {self._count}（{meta}）" if viewable else \
                f"图片 {self._count}（{meta}，此格式无法直接查看，需另存/转码）"
        result = (shown, label)
        self._exported[partname] = result
        return result

    def _image_marker(self, part, alt: str, where: str = "") -> list[str]:
        got = self._export_image(part)
        prefix = f"｛{where}｝" if where else ""
        if got is None:
            return [f"[{prefix}图片：未能导出（超过 {_DOCX_IMAGE_MAX} 张上限）]"]
        rel, label = got
        tail = f"，说明「{alt[:120]}」" if alt else ""
        return [f"[{prefix}{label} → {rel}{tail}]"]

    # 段落里所有非文本内容
    def paragraph_extras(self, paragraph, where: str = "") -> list[str]:
        out: list[str] = []
        el = getattr(paragraph, "_p", None)
        if el is None:
            return out
        try:
            drawings = el.findall(".//w:drawing", namespaces=_NSS)
            picts = el.findall(".//w:pict", namespaces=_NSS)
        except Exception:  # noqa: BLE001
            return out

        for drawing in drawings:
            out.extend(self._drawing_extras(drawing, where))
        # OLE 嵌入（Excel/PDF/Visio…）在真实 Word 里是 w:object，不在 w:pict 里
        for obj in el.findall(".//w:object", namespaces=_NSS):
            out.extend(self._object_extras(obj, where))
        for pict in picts:
            out.extend(self._pict_extras(pict, where))

        # 文本框：pict 里已处理过的 txbxContent 会重复，这里按元素去重
        for txbx in el.findall(".//w:txbxContent", namespaces=_NSS):
            text = self._textbox_text(txbx)
            if text:
                self._textbox_no += 1
                prefix = f"｛{where}｝" if where else ""
                out.append(f"[{prefix}文本框 {self._textbox_no}：{text}]")
        return out

    @staticmethod
    def _textbox_text(txbx) -> str:
        paras = []
        for p in txbx.findall(".//w:p", namespaces=_NSS):
            txt = "".join(t.text or "" for t in p.findall(".//w:t", namespaces=_NSS))
            if txt.strip():
                paras.append(txt.strip())
        return "⏎".join(paras)

    def _drawing_extras(self, drawing, where: str) -> list[str]:
        out: list[str] = []
        alt = ""
        docprs = drawing.findall(".//wp:docPr", namespaces=_NSS)
        if docprs:
            alt = (docprs[0].get("descr") or docprs[0].get("title") or "").strip()
        for blip in drawing.findall(".//a:blip", namespaces=_NSS):
            rid = blip.get(f"{{{_NSS['r']}}}embed") or blip.get(f"{{{_NSS['r']}}}link")
            part = _rel_part(self.doc_part, rid) if rid else None
            if part is None:
                continue
            out.extend(self._image_marker(part, alt, where))
        for chart in drawing.findall(".//c:chart", namespaces=_NSS):
            rid = chart.get(f"{{{_NSS['r']}}}id")
            part = _rel_part(self.doc_part, rid) if rid else None
            if part is None:
                continue
            self._chart_no += 1
            summary = _chart_summary(getattr(part, "blob", b""))
            prefix = f"｛{where}｝" if where else ""
            out.append(f"[{prefix}图表 {self._chart_no}：{summary}]")
        return out

    def _object_extras(self, obj, where: str) -> list[str]:
        """w:object：OLE 嵌入对象。里面的 v:imagedata 只是预览图，不是文内插图。"""
        out: list[str] = []
        for ole in obj.findall(".//o:OLEObject", namespaces=_NSS):
            out.extend(self._ole_extras(ole, where))
        if not obj.findall(".//o:OLEObject", namespaces=_NSS):
            # 不是 OLE 而只有预览图的 object（少见）：仍按图片给出，别静默丢
            for imagedata in obj.findall(".//v:imagedata", namespaces=_NSS):
                rid = imagedata.get(f"{{{_NSS['r']}}}id")
                part = _rel_part(self.doc_part, rid) if rid else None
                if part is not None and (getattr(part, "content_type", "") or "").startswith("image/"):
                    out.extend(self._image_marker(part, "", where))
        return out

    def _pict_extras(self, pict, where: str) -> list[str]:
        out: list[str] = []
        for imagedata in pict.findall(".//v:imagedata", namespaces=_NSS):
            rid = imagedata.get(f"{{{_NSS['r']}}}id")
            part = _rel_part(self.doc_part, rid) if rid else None
            if part is None:
                continue
            ctype = getattr(part, "content_type", "") or ""
            if ctype.startswith("image/"):
                out.extend(self._image_marker(part, (imagedata.get(f"{{{_NSS['o']}}}title") or "").strip(), where))
        for ole in pict.findall(".//o:OLEObject", namespaces=_NSS):
            out.extend(self._ole_extras(ole, where))
        return out

    def _ole_extras(self, ole, where: str) -> list[str]:
        rid = ole.get(f"{{{_NSS['r']}}}id")
        if not rid or rid in self._embedded_seen:
            return []
        self._embedded_seen.add(rid)
        part = _rel_part(self.doc_part, rid)
        if part is None:
            return []
        prefix = f"｛{where}｝" if where else ""
        ctype = (getattr(part, "content_type", "") or "").lower()
        name = Path(str(getattr(part, "partname", "") or "embedded")).name
        blob = getattr(part, "blob", b"")
        if ctype in _XLSX_CONTENT_TYPES:
            lines = _embedded_xlsx_lines(name, blob)
            head = f"[{prefix}内嵌表格开始]" if prefix else "[内嵌表格开始]"
            return [head] + lines + ["[内嵌表格结束]"]
        return [f"[{prefix}内嵌文件：{name}（{ctype or '未知类型'}）无法在正文里展开，"
                f"需要用户另存为独立文件后用 parse_document 解析]"]


def _embedded_xlsx_lines(name: str, blob: bytes, max_rows: int = _DOCX_EMBED_MAX_ROWS) -> list[str]:
    openpyxl = _import_or_hint("openpyxl", "openpyxl")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001
        return [f"[内嵌表格（{name}）打不开：{type(e).__name__}；可能不是 Excel 或已加密]"]
    lines: list[str] = []
    try:
        names = list(wb.sheetnames)
        for idx, sheet in enumerate(names, 1):
            ws = wb[sheet]
            total = ws.max_row if isinstance(ws.max_row, int) else None
            head = f"[内嵌表格 {idx}/{len(names)}：{sheet}" + (f"，共约 {total} 行]" if total else "]")
            lines.append(head)
            n = 0
            for row in ws.iter_rows(values_only=True):
                cells = [_cell(v) for v in row]
                while cells and cells[-1] == "":
                    cells.pop()
                if not cells:
                    continue
                if n >= max_rows:
                    lines.append(f"[本表只展开前 {max_rows} 行]")
                    break
                lines.append("\t".join(cells))
                n += 1
    finally:
        wb.close()
    return lines


def _chart_summary(blob: bytes) -> str:
    """从 chart 部件 XML 的缓存值里读标题/类型/系列数据（不依赖原始 Excel）。"""
    try:
        from lxml import etree
        root = etree.fromstring(blob)
    except Exception:  # noqa: BLE001
        return "图表数据读不出来（部件损坏或不含缓存值）"

    def _texts(el, path: str) -> list[str]:
        return [(t.text or "").strip() for t in el.findall(path, namespaces=_NSS) if (t.text or "").strip()]

    kinds = []
    for tag, label in (("barChart", "柱状图"), ("lineChart", "折线图"), ("pieChart", "饼图"),
                       ("doughnutChart", "圆环图"), ("scatterChart", "散点图"), ("areaChart", "面积图"),
                       ("radarChart", "雷达图")):
        try:
            if root.find(f".//c:{tag}", namespaces=_NSS) is not None:
                kinds.append(label)
        except Exception:  # noqa: BLE001
            continue
    title = " / ".join(_texts(root, ".//c:title//a:t"))[:80]
    series: list[str] = []
    for ser in root.findall(".//c:ser", namespaces=_NSS):
        name = " / ".join(_texts(ser, "./c:tx//c:v"))[:60]
        cats = [p.findtext("c:v", namespaces=_NSS) or "" for p in ser.findall("./c:cat//c:pt", namespaces=_NSS)]
        vals = [p.findtext("c:v", namespaces=_NSS) or "" for p in ser.findall("./c:val//c:pt", namespaces=_NSS)]
        if cats and len(cats) == len(vals):
            pairs = "、".join(f"{c}={v}" for c, v in zip(cats, vals))
        elif vals:
            pairs = "、".join(vals)
        else:
            pairs = "（没有缓存数值）"
        series.append(f"「{name or '未命名系列'}」：{pairs}")
    head = "，".join(x for x in (("、".join(kinds) if kinds else ""), (f"标题「{title}」" if title else "")) if x)
    body = "；".join(series)
    if head and body:
        return f"{head}；{body}"
    return head or body or "图表（无缓存数据）"


def _parse_docx(path: Path, args: dict, workspace=None) -> str:
    docx_mod = _import_or_hint("docx", "python-docx")
    from docx.text.paragraph import Paragraph

    try:
        doc = docx_mod.Document(str(path))
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"打开 docx 失败（文件可能损坏或加了密码）：{type(e).__name__}: {e}")

    assets = _DocxAssets(path, doc.part, workspace)
    assets.doc_part = doc.part
    parts: list[str] = []
    table_no = 0

    for block in doc.iter_inner_content():
        if isinstance(block, Paragraph):
            if block.text.strip():
                parts.append(block.text)
            parts.extend(assets.paragraph_extras(block))
        else:
            table_no += 1
            parts.append(f"[表格 {table_no}：{len(block.rows)} 行 × {len(block.columns)} 列]")
            for row in block.rows:
                parts.append("\t".join(_cell(c.text) for c in row.cells))
            # 单元格里的图片/图表也要能看到，标出它在哪一格
            for r_no, row in enumerate(block.rows, 1):
                for c_no, cell in enumerate(row.cells, 1):
                    for para in cell.paragraphs:
                        parts.extend(assets.paragraph_extras(
                            para, where=f"表格 {table_no} 第 {r_no} 行第 {c_no} 列"))

    # 页眉页脚：正文里看不到但常放公司模板/字段说明
    for hf_name, hf in _iter_headers_footers(doc):
        texts, extras = [], []
        for para in hf.paragraphs:
            if para.text.strip():
                texts.append(para.text)
            extras.extend(assets.paragraph_extras(para, where=hf_name))
        for t in hf.tables:
            for row in t.rows:
                line = "\t".join(_cell(c.text) for c in row.cells).rstrip("\t")
                if line.strip():
                    texts.append(line)
        if texts or extras:
            parts.append(f"[{hf_name}]")
            parts.extend(texts)
            parts.extend(extras)

    # 批注：需求文档里常是评审补充，和正文同级重要
    try:
        for i, comment in enumerate(doc.comments, 1):
            text = (getattr(comment, "text", "") or "").strip()
            if not text:
                continue
            author = (getattr(comment, "author", "") or "").strip() or "匿名"
            parts.append(f"[批注 {i}（{author}）：{_cell(text)}]")
    except Exception:  # noqa: BLE001 - 老版本 python-docx 无 comments，忽略即可
        pass

    if assets._skipped:
        parts.append(f"[还有 {assets._skipped} 张图片超出单次导出上限（{_DOCX_IMAGE_MAX} 张），未导出]")

    body = "\n".join(parts) or "（文档为空）"
    hint = ""
    if assets._count:
        hint = f"图片已导出到 {assets._display(assets._target_dir())}/，用 view_image 逐张查看"
    return _page_text(body, args, hint)


def _iter_headers_footers(doc):
    """按节遍历页眉页脚，同名只出一次（多节共享同一份）。"""
    seen: set[tuple[str, int]] = set()
    for sec_no, section in enumerate(getattr(doc, "sections", []) or [], 1):
        for label, obj in (("页眉", section.header), ("页脚", section.footer)):
            try:
                if _is_linked(obj):
                    continue
                key = (label, id(obj.part))
            except Exception:  # noqa: BLE001
                continue
            if key in seen:
                continue
            seen.add(key)
            yield f"{label}" + (f"（第 {sec_no} 节）" if sec_no > 1 else ""), obj


def _is_linked(header_or_footer) -> bool:
    try:
        return bool(header_or_footer.is_linked_to_previous)
    except Exception:  # noqa: BLE001
        return False


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
