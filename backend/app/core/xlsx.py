"""用于资产台账导出的最小 XLSX 写入器。

为什么不用第三方库：离线发布包没有 openpyxl/xlsxwriter，新增依赖会让目标机
断网环境装不上。这里只实现导出需要的子集：多 sheet、表头加粗、冻结首行、
列宽、自动筛选、文本/数字单元格。

安全：所有文本走 inlineStr（不是公式），外部资产名以 = + - @ 开头也不会
被 Excel 当公式执行；非法 XML 控制字符剔除，单元格超长截断到 Excel 上限。
"""
from __future__ import annotations

import io
import re
import zipfile
from typing import Iterable, Sequence
from urllib.parse import quote
from xml.sax.saxutils import escape

MAX_CELL_CHARS = 32767
_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_SHEET_BAD = re.compile(r"[\[\]:*?/\\]")


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    text = _ILLEGAL_XML.sub("", str(value))
    return text[:MAX_CELL_CHARS]


def _col_letter(index: int) -> str:
    out = ""
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def _sheet_name(name: str, used: set[str]) -> str:
    base = _SHEET_BAD.sub("_", _cell_text(name) or "Sheet").strip("'") or "Sheet"
    base = base[:31]
    candidate, n = base, 2
    while candidate.casefold() in used:
        suffix = f" ({n})"
        candidate = base[:31 - len(suffix)] + suffix
        n += 1
    used.add(candidate.casefold())
    return candidate


def _numeric(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _sheet_xml(columns: Sequence[tuple], rows: Sequence[dict]) -> str:
    # columns: [(key, label, kind)]，kind="number" 时数字写数值单元格，其余一律文本
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '</sheetView></sheetViews>',
        "<cols>",
    ]
    for idx, (key, label, kind) in enumerate(columns, 1):
        width = max(10, min(48, len(str(label)) * 2 + 4))
        parts.append(f'<col min="{idx}" max="{idx}" width="{width}" customWidth="1"/>')
    parts.append("</cols><sheetData>")

    header = []
    for idx, (_key, label, _kind) in enumerate(columns, 1):
        ref = f"{_col_letter(idx)}1"
        header.append(
            f'<c r="{ref}" s="1" t="inlineStr"><is><t xml:space="preserve">'
            f"{escape(_cell_text(label))}</t></is></c>"
        )
    parts.append('<row r="1">' + "".join(header) + "</row>")

    for r_idx, row in enumerate(rows, 2):
        cells = []
        for c_idx, (key, _label, kind) in enumerate(columns, 1):
            value = row.get(key)
            if value is None or value == "":
                continue
            ref = f"{_col_letter(c_idx)}{r_idx}"
            if kind == "number":
                num = _numeric(value)
                if num is not None:
                    cells.append(f'<c r="{ref}"><v>{num}</v></c>')
                    continue
            text = escape(_cell_text(value))
            cells.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>')
        parts.append(f'<row r="{r_idx}">' + "".join(cells) + "</row>")

    parts.append("</sheetData>")
    if rows:
        last = f"{_col_letter(max(1, len(columns)))}{len(rows) + 1}"
        parts.append(f'<autoFilter ref="A1:{last}"/>')
    parts.append("</worksheet>")
    return "".join(parts)


def build_xlsx(sheets: Iterable[dict]) -> bytes:
    """sheets=[{name, columns:[(key,label,kind)], rows:[dict]}] -> XLSX bytes。"""
    sheets = list(sheets)
    used: set[str] = set()
    names = [_sheet_name(s.get("name", "Sheet"), used) for s in sheets]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   + "".join(
                       f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
                       'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                       for i in range(1, len(sheets) + 1))
                   + "</Types>")
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                   '</Relationships>')
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   + "".join(
                       f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>'
                       for i, name in enumerate(names, 1))
                   + "</sheets></workbook>")
        rels = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
        ]
        for i in range(1, len(sheets) + 1):
            rels.append(
                f'<Relationship Id="rId{i}" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                f'Target="worksheets/sheet{i}.xml"/>'
            )
        rels.append(
            f'<Relationship Id="rId{len(sheets) + 1}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
            'Target="styles.xml"/>'
        )
        rels.append("</Relationships>")
        z.writestr("xl/_rels/workbook.xml.rels", "".join(rels))
        z.writestr("xl/styles.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   '<fonts count="2">'
                   '<font><sz val="11"/><name val="Calibri"/></font>'
                   '<font><b/><sz val="11"/><name val="Calibri"/></font>'
                   '</fonts>'
                   '<fills count="2"><fill><patternFill patternType="none"/></fill>'
                   '<fill><patternFill patternType="gray125"/></fill></fills>'
                   '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
                   '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                   '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
                   '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
                   '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
                   '</styleSheet>')
        for i, sheet in enumerate(sheets, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml",
                       _sheet_xml(sheet.get("columns") or [], sheet.get("rows") or []))
    return buf.getvalue()


def xlsx_content_disposition(filename: str, ascii_filename: str) -> str:
    return (f'attachment; filename="{ascii_filename}"; '
            f"filename*=UTF-8''{quote(filename)}")


def xlsx_response(content: bytes, *, filename: str, ascii_filename: str,
                  total_rows: int, exported_rows: int) -> tuple[bytes, dict]:
    """返回 (bytes, headers)，由路由层包成 Response。"""
    return content, {
        "Content-Disposition": xlsx_content_disposition(filename, ascii_filename),
        "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "X-Total-Rows": str(int(total_rows or 0)),
        "X-Exported-Rows": str(int(exported_rows or 0)),
        "Cache-Control": "no-store",
    }
