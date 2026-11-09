"""文档解析、内置技能播种及附件校验测试。"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from game_agent.agents.tools import ToolContext, ToolError
from game_agent.agents.tools.documents import MAX_PARSE_CHARS, parse_document
from game_agent.agents.tools.native import _resolve
from game_agent.agents.tools.registry import TOOL_IMPLS, TOOL_SCHEMAS, execute_tool
from game_agent.api.routers.sessions import FileIn, _validate_files
from game_agent.core import skills
from game_agent.core.skills import (
    _BUILTIN_SKILLS, delete_global_skill, list_skills, seed_builtin_skills,
)


def _write_pdf(path: Path, texts: list[str], password: str | None = None) -> None:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    }))
    for text in texts:
        page = writer.add_blank_page(width=595, height=842)
        if text:
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
            })
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 20 800 Td ({escaped}) Tj ET".encode("ascii"))
            page[NameObject("/Contents")] = writer._add_object(stream)
    if password is not None:
        writer.encrypt(password, owner_password="owner-secret")
    with path.open("wb") as stream:
        writer.write(stream)


@pytest.fixture()
def docs_ws(tmp_path: Path) -> Path:
    import docx
    import openpyxl

    ws = tmp_path / "ws"
    ws.mkdir()
    wb = openpyxl.Workbook()
    sh = wb.active
    sh.title = "成绩表"
    sh.append(["姓名", "分数"])
    sh.append(["张三", 92])
    sh.append(["李四", 78])
    wb.create_sheet("第二表")
    wb.save(ws / "t.xlsx")
    wb.close()

    doc = docx.Document()
    doc.add_paragraph("第一段：需求说明。")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "键"
    table.cell(0, 1).text = "值"
    table.cell(1, 0).text = "a"
    table.cell(1, 1).text = "b"
    doc.add_paragraph("第二段：表格之后。")
    doc.add_table(rows=1, cols=1).cell(0, 0).text = "第二张表"
    doc.add_paragraph("第三段：文档结束。")
    doc.save(ws / "t.docx")

    _write_pdf(ws / "t.pdf", [""])
    return ws


def _ctx(ws: Path) -> ToolContext:
    return ToolContext(run_ctx=None, workspace=ws, role="main", task_id="t")


def _parse(ws: Path, path: str, **args) -> str:
    return asyncio.run(execute_tool("parse_document", {"path": path, **args}, _ctx(ws)))


def _read_all(ws: Path, path: str, **args) -> tuple[str, list[str]]:
    async def read() -> tuple[str, list[str]]:
        offset = 1
        chunks, results = [], []
        expected_total = None
        for _ in range(1000):
            result = await execute_tool("parse_document", {"path": path, **args, "offset": offset}, _ctx(ws))
            assert isinstance(result, str)
            assert len(result) < 8000
            assert "截断" not in result
            header, _, rest = result.partition("\n")
            body, _, footer = rest.rpartition("\n")
            match = re.fullmatch(r"\[字符 (\d+)-(\d+) / 共 (\d+) 字符\]", header)
            assert match, result
            start, end, total = map(int, match.groups())
            assert start == offset
            assert len(body) == end - start + 1
            assert len(body) <= min(args.get("limit", MAX_PARSE_CHARS), MAX_PARSE_CHARS)
            if expected_total is None:
                expected_total = total
            assert total == expected_total
            chunks.append(body)
            results.append(result)
            if end == total:
                assert "本段文本已读完" in footer
                text = "".join(chunks)
                assert len(text) == total
                return text, results
            next_offset = re.search(r"用 offset=(\d+) 继续读", footer)
            assert next_offset, footer
            offset = int(next_offset.group(1))
            assert offset == end + 1
        pytest.fail("分页没有结束")

    return asyncio.run(read())


def test_parse_document_registered() -> None:
    assert "parse_document" in TOOL_IMPLS
    schema = TOOL_SCHEMAS["parse_document"]
    props = schema["parameters"]["properties"]
    assert {"path", "sheet", "page", "max_rows", "start_row", "offset", "limit"} == set(props)
    assert props["page"]["default"] == 1
    assert props["offset"]["default"] == 1
    assert props["limit"]["default"] == props["limit"]["maximum"] == MAX_PARSE_CHARS
    assert "Unicode" in props["offset"]["description"]
    assert "不含分页头尾" in props["offset"]["description"]


def test_xlsx_tsv_and_sheets(docs_ws: Path) -> None:
    result = _parse(docs_ws, "t.xlsx")
    assert "1/2" in result and "成绩表" in result and "第二表" in result
    assert "[第 1 行]\t姓名\t分数" in result
    assert "[第 2 行]\t张三\t92" in result
    second = _parse(docs_ws, "t.xlsx", sheet=2)
    assert "2/2" in second and "没有数据行" in second


def test_xlsx_row_paging(docs_ws: Path) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    for i in range(1, 260):
        wb.active.append([i, f"r{i}"])
    wb.save(docs_ws / "big.xlsx")
    wb.close()
    first = _parse(docs_ws, "big.xlsx", max_rows=100)
    assert "259 行" in first and "start_row=101、offset=1" in first
    assert "[第 1 行]" in first and "r100" in first and "r101" not in first
    second = _parse(docs_ws, "big.xlsx", max_rows=100, start_row=101)
    assert "[第 101 行]" in second and "r200" in second and "r201" not in second
    assert "start_row=201、offset=1" in second
    last = _parse(docs_ws, "big.xlsx", max_rows=100, start_row=201)
    assert "r259" in last and "start_row=" not in last
    assert "没有数据行" in _parse(docs_ws, "big.xlsx", start_row=260)


def test_xlsx_large_cells_all_columns_and_continuation(docs_ws: Path) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    sh = wb.active
    sh.append(["not-selected"])
    cells = ["大单元格" * 7000, "a\tb\nc\rd", *[f"column-{i}" for i in range(3, 66)]]
    sh.append(cells)
    sh.append(["next-row"])
    wb.save(docs_ws / "wide.xlsx")
    wb.close()

    text, pages = _read_all(docs_ws, "wide.xlsx", start_row=2, max_rows=1, limit=100000)
    expected_cells = [cells[0], "a b⏎c d", *cells[2:]]
    assert text == ("[工作表 1/1：Sheet，共约 3 行，全部工作表：Sheet]\n"
                    "[第 2 行]\t" + "\t".join(expected_cells))
    assert len(pages) > 4
    assert "column-65" in pages[-1]
    assert "start_row=3、offset=1" in pages[-1].splitlines()[-1]
    assert all("start_row=3" not in page.splitlines()[-1] for page in pages[:-1])
    assert "next-row" in _parse(docs_ws, "wide.xlsx", start_row=3, max_rows=1)


def test_xlsx_bad_sheet_is_model_readable_error(docs_ws: Path) -> None:
    result = _parse(docs_ws, "t.xlsx", sheet=9)
    assert "工具错误" in result and "超出范围" in result


def test_docx_paragraph_table_order(docs_ws: Path) -> None:
    text, _ = _read_all(docs_ws, "t.docx", limit=13)
    assert text == ("第一段：需求说明。\n[表格 1：2 行 × 2 列]\n键\t值\na\tb\n"
                    "第二段：表格之后。\n[表格 2：1 行 × 1 列]\n第二张表\n第三段：文档结束。")


def test_docx_huge_paragraph_and_table_are_fully_accessible(docs_ws: Path) -> None:
    import docx

    paragraph = "正文长段落" * 11000
    cell = "表格内容" * 5000
    doc = docx.Document()
    doc.add_paragraph(paragraph)
    doc.add_table(rows=1, cols=1).cell(0, 0).text = cell
    doc.add_paragraph("文档末尾")
    doc.save(docs_ws / "long.docx")
    text, pages = _read_all(docs_ws, "long.docx")
    assert text == f"{paragraph}\n[表格 1：1 行 × 1 列]\n{cell}\n文档末尾"
    assert len(pages) > 10


def test_empty_docx(docs_ws: Path) -> None:
    import docx

    docx.Document().save(docs_ws / "empty.docx")
    text, _ = _read_all(docs_ws, "empty.docx", limit=1)
    assert text == "（文档为空）"


def test_pdf_no_text_layer_hint(docs_ws: Path) -> None:
    result = _parse(docs_ws, "t.pdf")
    assert "共 1 页" in result
    assert "没有可提取的文本" in result and "OCR" in result
    assert "可能" in result and "空白页" in result
    assert "整份 PDF" not in result


def test_pdf_only_extracts_requested_page(docs_ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pypdf import PageObject

    _write_pdf(docs_ws / "text.pdf", ["first-page", "second-page", "last-page"])
    extract = PageObject.extract_text
    calls = []

    def tracked_extract(page, *args, **kwargs):
        text = extract(page, *args, **kwargs)
        calls.append(text)
        return text

    monkeypatch.setattr(PageObject, "extract_text", tracked_extract)
    first = _parse(docs_ws, "text.pdf")
    assert "first-page" in first and "second-page" not in first and "last-page" not in first
    assert "第 1 页 / 共 3 页" in first and "page=2、offset=1" in first
    assert calls == ["first-page"]
    last = _parse(docs_ws, "text.pdf", page=3)
    assert "last-page" in last and "page=4" not in last
    assert calls == ["first-page", "last-page"]
    assert "超出范围" in _parse(docs_ws, "text.pdf", page=4)
    assert len(calls) == 2


def test_pdf_huge_single_page_continuation(docs_ws: Path) -> None:
    content = "HUGE_PAGE_TEXT_" * 3500 + "END-OF-PAGE"
    _write_pdf(docs_ws / "huge.pdf", [content, "another-page"])
    text, pages = _read_all(docs_ws, "huge.pdf", page=1)
    assert text == f"[第 1 页 / 共 2 页]\n--- 第 1 页 ---\n{content}"
    assert len(pages) > 8
    assert "page=2、offset=1" in pages[-1].splitlines()[-1]
    assert all("page=2" not in page.splitlines()[-1] for page in pages[:-1])
    assert "another-page" in _parse(docs_ws, "huge.pdf", page=2)


def test_pdf_extraction_failure_is_not_reported_as_scanned(docs_ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pypdf import PageObject

    _write_pdf(docs_ws / "broken-text.pdf", ["real-text"])

    def fail_extract(*args, **kwargs):
        raise ValueError("broken content stream")

    monkeypatch.setattr(PageObject, "extract_text", fail_extract)
    result = _parse(docs_ws, "broken-text.pdf")
    assert "工具错误" in result and "第 1 页文本提取失败" in result
    assert "ValueError: broken content stream" in result
    assert "扫描" not in result and "OCR" not in result and "没有可提取" not in result


@pytest.mark.parametrize("password", ["", "secret"])
def test_encrypted_pdf_checks_empty_password_result(docs_ws: Path, password: str) -> None:
    _write_pdf(docs_ws / "encrypted.pdf", ["encrypted-text"], password=password)
    result = _parse(docs_ws, "encrypted.pdf")
    if password:
        assert result == "工具错误：PDF 有密码，无法解析"
    else:
        assert "encrypted-text" in result and "工具错误" not in result


@pytest.mark.parametrize("path", ["t.xlsx", "t.docx", "t.pdf"])
@pytest.mark.parametrize("args", [{"offset": 0}, {"offset": "bad"}, {"offset": 10**8},
                                  {"limit": -1}, {"limit": "bad"}, {"limit": 1.5}])
def test_invalid_text_paging_is_model_readable(docs_ws: Path, path: str, args: dict) -> None:
    result = _parse(docs_ws, path, **args)
    assert result.startswith("工具错误：")
    assert next(iter(args)) in result


def test_read_file_hints(docs_ws: Path) -> None:
    result = asyncio.run(execute_tool("read_file", {"path": "t.xlsx"}, _ctx(docs_ws)))
    assert "parse_document" in result and "乱码" in result
    (docs_ws / "old.doc").write_bytes(b"x")
    old = asyncio.run(execute_tool("read_file", {"path": "old.doc"}, _ctx(docs_ws)))
    assert "另存为" in old
    (docs_ws / "slides.pptx").write_bytes(b"x")
    slides = asyncio.run(execute_tool("read_file", {"path": "slides.pptx"}, _ctx(docs_ws)))
    assert "旧版" not in slides and "PDF" in slides and "不支持" in slides


@pytest.mark.parametrize("absolute", [False, True])
@pytest.mark.parametrize("filename", ["ws.xlsx", "ws-sibling/outside.xlsx"])
def test_filename_and_sibling_path_escape_rejected(tmp_path: Path, filename: str, absolute: bool) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / filename
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"outside")
    path = str(outside) if absolute else f"../{filename}"
    with pytest.raises(ToolError, match="路径越界"):
        _resolve(ws, path)
    with pytest.raises(ToolError, match="路径越界"):
        asyncio.run(parse_document({"path": path}, _ctx(ws)))


@pytest.mark.parametrize("directory", [False, True])
def test_symlink_escape_rejected(tmp_path: Path, directory: bool) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "ws-outside"
    outside.mkdir()
    (outside / "outside.xlsx").write_bytes(b"outside")
    link = ws / ("linked" if directory else "linked.xlsx")
    try:
        link.symlink_to(outside if directory else outside / "outside.xlsx", target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"当前环境无法创建符号链接：{exc}")
    path = "linked/outside.xlsx" if directory else "linked.xlsx"
    with pytest.raises(ToolError, match="路径越界"):
        asyncio.run(parse_document({"path": path}, _ctx(ws)))


def test_resolve_allows_workspace_descendants(docs_ws: Path) -> None:
    assert _resolve(docs_ws, "./t.xlsx") == (docs_ws / "t.xlsx").resolve()
    assert _resolve(docs_ws, str(docs_ws / "t.xlsx")) == (docs_ws / "t.xlsx").resolve()


def test_resolved_target_is_checked_without_symlink_privileges(docs_ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    resolve = Path.resolve
    linked = docs_ws / "linked.xlsx"
    outside = docs_ws.parent / "ws-outside" / "outside.xlsx"

    def resolve_link(path, *args, **kwargs):
        if path == linked:
            return outside
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_link)
    with pytest.raises(ToolError, match="路径越界"):
        asyncio.run(parse_document({"path": "linked.xlsx"}, _ctx(docs_ws)))


def test_parse_document_rejects_sandbox_escape(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="路径越界"):
        asyncio.run(parse_document({"path": "../../etc/passwd.xlsx"}, _ctx(tmp_path)))


@pytest.mark.parametrize("suffix", [".doc", ".xls"])
def test_old_formats_rejected(docs_ws: Path, suffix: str) -> None:
    (docs_ws / f"old{suffix}").write_bytes(b"x")
    result = _parse(docs_ws, f"old{suffix}")
    assert "工具错误" in result and "另存为" in result


@pytest.mark.parametrize("suffix", [".xlsx", ".docx", ".pdf"])
def test_corrupt_documents_are_model_readable_errors(docs_ws: Path, suffix: str) -> None:
    (docs_ws / f"corrupt{suffix}").write_bytes(b"not a document")
    assert _parse(docs_ws, f"corrupt{suffix}").startswith("工具错误：")


@pytest.fixture()
def skills_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "skills"
    monkeypatch.setattr(skills, "GLOBAL_SKILLS_DIR", root)

    async def no_extra_roots():
        return []

    monkeypatch.setattr(skills, "get_extra_roots", no_extra_roots)
    return root


def test_seed_builtin_skills_idempotent_and_respects_deletion(skills_dir: Path) -> None:
    assert set(seed_builtin_skills()) == set(_BUILTIN_SKILLS)
    for name in _BUILTIN_SKILLS:
        assert (skills_dir / name / "SKILL.md").is_file()
    assert seed_builtin_skills() == []
    delete_global_skill("word")
    assert seed_builtin_skills() == []
    assert not (skills_dir / "word").exists()
    (skills_dir / ".builtin-seeded").write_text("0", encoding="utf-8")
    assert seed_builtin_skills() == ["word"]
    assert (skills_dir / "word" / "SKILL.md").is_file()


@pytest.mark.parametrize("flat", [False, True])
def test_seed_preserves_custom_skills(skills_dir: Path, flat: bool) -> None:
    path = skills_dir / "excel.md" if flat else skills_dir / "excel" / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    custom = "---\nname: excel\ndescription: 我的\n---\n用户内容"
    path.write_text(custom, encoding="utf-8")
    # 自定义 excel 已存在 → 只补其余内置技能（imagegen 等新增项自动包含进来）
    assert set(seed_builtin_skills()) == set(_BUILTIN_SKILLS) - {"excel"}
    assert path.read_text(encoding="utf-8") == custom
    assert (skills_dir / ".builtin-seeded").read_text(encoding="utf-8") == skills._BUILTIN_SKILLS_VERSION
    assert seed_builtin_skills() == []


@pytest.mark.parametrize("partial", [False, True])
def test_failed_skill_seed_retries_without_marker(skills_dir: Path, monkeypatch: pytest.MonkeyPatch, partial: bool) -> None:
    write = Path.write_text
    failed = False

    def fail_once(path, *args, **kwargs):
        nonlocal failed
        if path.parent.name == "word" and not failed:
            failed = True
            if partial:
                write(path, "partial", encoding="utf-8")
            raise OSError("write failed")
        return write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_once)
    # word 写入失败 → 其余内置技能都要补齐（含 imagegen 等新增项）
    assert set(seed_builtin_skills()) == set(_BUILTIN_SKILLS) - {"word"}
    assert not (skills_dir / ".builtin-seeded").exists()
    assert not (skills_dir / "word" / "SKILL.md").exists()
    assert seed_builtin_skills() == ["word"]
    assert (skills_dir / ".builtin-seeded").read_text(encoding="utf-8") == skills._BUILTIN_SKILLS_VERSION
    assert "offset/limit" in (skills_dir / "word" / "SKILL.md").read_text(encoding="utf-8")
    assert seed_builtin_skills() == []


def test_failed_skill_install_does_not_preserve_partial_file(skills_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    replace = Path.replace
    unlink = Path.unlink
    pending = skills_dir / "word" / "SKILL.tmp"

    def fail_replace(path, target):
        if path == pending:
            raise OSError("install failed")
        return replace(path, target)

    def fail_cleanup(path, *args, **kwargs):
        if path == pending:
            raise OSError("cleanup failed")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail_replace)
        patch.setattr(Path, "unlink", fail_cleanup)
        assert set(seed_builtin_skills()) == set(_BUILTIN_SKILLS) - {"word"}
        assert pending.exists()
        assert not pending.with_suffix(".md").exists()
        assert not (skills_dir / ".builtin-seeded").exists()
    assert seed_builtin_skills() == ["word"]
    assert not pending.exists()
    assert pending.with_suffix(".md").is_file()
    assert (skills_dir / ".builtin-seeded").is_file()


def test_failed_marker_write_can_retry(skills_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write = Path.write_text
    failed = False

    def fail_once(path, *args, **kwargs):
        nonlocal failed
        if path.name == ".builtin-seeded" and not failed:
            failed = True
            raise OSError("marker write failed")
        return write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_once)
    assert set(seed_builtin_skills()) == set(_BUILTIN_SKILLS)
    assert not (skills_dir / ".builtin-seeded").exists()
    assert seed_builtin_skills() == []
    assert (skills_dir / ".builtin-seeded").read_text(encoding="utf-8") == skills._BUILTIN_SKILLS_VERSION


def test_seeded_skills_are_listable_and_describe_paging(skills_dir: Path) -> None:
    seed_builtin_skills()
    names = {s["name"] for s in asyncio.run(list_skills(None))["skills"]}
    assert set(_BUILTIN_SKILLS) <= names
    # 只有「文档解析」类技能讲 offset/limit 分页；imagegen 等不是分页文档，别一起断言
    for name in ("excel", "word", "pdf"):
        body = (skills_dir / name / "SKILL.md").read_text(encoding="utf-8")
        assert "offset" in body and "limit" in body and "6000" in body
        assert "Unicode" in body and "不含分页头尾" in body
        assert "16000" not in body and "最多 50 列" not in body
    assert "默认只提取第 1 页" in (skills_dir / "pdf" / "SKILL.md").read_text(encoding="utf-8")


def test_validate_files_ok_and_sanitize() -> None:
    out = _validate_files([FileIn(path=".trace/uploads/x-成绩表.xlsx", name="成绩表.xlsx", size=123)])
    assert out == [{"path": ".trace/uploads/x-成绩表.xlsx", "name": "成绩表.xlsx", "size": 123}]
    out = _validate_files([FileIn(path=".trace/uploads\\a.pdf")])
    assert out[0]["path"] == ".trace/uploads/a.pdf" and out[0]["name"] == "a.pdf"
    out = _validate_files([FileIn(path=".trace/uploads/a.pdf"), FileIn(path=".trace/uploads/a.pdf", name="a.pdf")])
    assert len(out) == 1


@pytest.mark.parametrize("bad", ["../etc/passwd", "uploads/a.pdf", ".trace/uploads/../x.xlsx",
                                 ".trace/uploads/", "C:/Windows/a.xlsx"])
def test_validate_files_rejects_bad_paths(bad: str) -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        _validate_files([FileIn(path=bad)])
