"""CSV 导出：列表同口径的流式下载。

设计约束：
  - 与列表共用查询构造器（导出≠第二套筛选口径）；
  - UTF-8 BOM 开头，Excel 双击不乱码；中文文件名走 RFC 5987 filename*；
  - 单次最多 20000 行（防误点导出拖垮浏览器/内存），真实总数放 X-Total-Rows；
  - 公式注入防护：以 = + - @ 开头的单元格加前缀单引号（数字除外）。
"""
from __future__ import annotations

import csv
import io
from typing import Iterable, Sequence
from urllib.parse import quote

from fastapi.responses import StreamingResponse

MAX_EXPORT_ROWS = 20000


def _safe_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    # Excel 公式注入防护（告警标题/资产名来自外部采集，不能当公式执行）
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        s = "'" + s
    return s


def _csv_line(values: Sequence) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([_safe_cell(v) for v in values])
    return buf.getvalue()


def csv_response(headers: Sequence[tuple[str, str]], rows: Iterable[dict], *,
                 filename: str, ascii_filename: str, total_rows: int,
                 max_rows: int = MAX_EXPORT_ROWS) -> StreamingResponse:
    """headers=[(key, 中文列名)]；rows 为 dict 列表（已按 max_rows 截断也行）。

    total_rows 是筛选后的真实总数（可大于 len(rows)），写进 X-Total-Rows。
    """
    keys = [k for k, _ in headers]
    titles = [t for _, t in headers]

    def _iter():
        yield "\ufeff" + _csv_line(titles)
        exported = 0
        for row in rows:
            if exported >= max_rows:
                break
            yield _csv_line([row.get(k) for k in keys])
            exported += 1
        if total_rows > exported:
            yield f"# truncated at {max_rows} rows\n"

    disp = (f"attachment; filename=\"{ascii_filename}\"; "
            f"filename*=UTF-8''{quote(filename)}")
    return StreamingResponse(
        _iter(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": disp,
            "X-Total-Rows": str(int(total_rows or 0)),
            "X-Exported-Rows": str(min(int(total_rows or 0), max_rows)),
            "Cache-Control": "no-store",
        },
    )
