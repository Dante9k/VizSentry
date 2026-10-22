from __future__ import annotations

import json
from pathlib import Path

import xlsxwriter
from PIL import Image

from .database import Database


def export_workbook(database: Database, job_id: str, destination: Path) -> Path:
    job = database.job(job_id)
    if not job['import_complete']:
        raise ValueError('记录清单尚未完整，不能导出为完整结果')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix('.building.xlsx')
    book = xlsxwriter.Workbook(str(temp), {'constant_memory': True, 'strings_to_formulas': False, 'strings_to_urls': False})
    header = book.add_format({'bg_color': '#142D39', 'font_color': '#FFFFFF', 'bold': True, 'text_wrap': True, 'valign': 'vcenter'})
    cell = book.add_format({'text_wrap': True, 'valign': 'top', 'font_size': 10})
    pending = book.add_format({'text_wrap': True, 'valign': 'top', 'font_color': '#9A6400'})
    link = book.add_format({'font_color': '#007F78', 'underline': True, 'valign': 'top'})
    sheets: dict[int, object] = {}
    log_sheets: dict[int, object] = {}
    image_sheets: dict[int, object] = {}
    log_count = 0
    image_count = 0
    max_rows = 1_048_575

    def get_result_sheet(index: int):
        if index not in sheets:
            sheet = book.add_worksheet('结果' if index == 0 else f'结果_{index + 1}')
            sheet.freeze_panes(1, 3)
            sheet.set_column(0, 0, 8)
            sheet.set_column(1, 2, 24)
            sheet.set_column(3, 3, 32)
            sheet.set_column(4, 4, 12)
            sheet.set_column(5, 6, 52)
            sheet.set_column(7, 10, 26)
            sheet.set_row(0, 30)
            sheet.write_row(0, 0, ['序号', '记录 ID', '名称', '图片', '图片结论', '判断依据', '日志摘要', '完整日志', '日志来源', '处理状态', '飞书表格'], header)
            sheet.write_comment(0, 7, '完整日志存储在隐藏的“日志全文”工作表中。请先在 Excel 工作表标签处右键选择“取消隐藏”，再点击此列跳转。应用网页可直接展开全文。')
            sheets[index] = sheet
        return sheets[index]

    def get_log_sheet(index: int):
        if index not in log_sheets:
            sheet = book.add_worksheet('日志全文' if index == 0 else f'日志全文_{index + 1}')
            sheet.write_row(0, 0, ['记录 ID', '日志名称', '分块序号', '原文（按分块序号无分隔拼接）', '完整性'], header)
            sheet.set_column(0, 1, 24)
            sheet.set_column(2, 2, 12)
            sheet.set_column(3, 3, 120)
            sheet.freeze_panes(1, 0)
            sheet.hide()
            log_sheets[index] = sheet
        return log_sheets[index]

    def get_image_sheet(index: int):
        if index not in image_sheets:
            sheet = book.add_worksheet('图片详情' if index == 0 else f'图片详情_{index + 1}')
            sheet.write_row(0, 0, ['原行序号', '记录 ID', '图片序号', '图片名称', '结论', '依据', '离线指标', '离线信号', '失败原因'], header)
            sheet.freeze_panes(1, 3)
            sheet.set_column(0, 2, 18)
            sheet.set_column(3, 4, 22)
            sheet.set_column(5, 8, 55)
            image_sheets[index] = sheet
        return image_sheets[index]

    try:
        get_result_sheet(0)
        for index, row in enumerate(database.export_rows(job_id)):
            sheet = get_result_sheet(index // max_rows)
            row_index = index % max_rows + 1
            sheet.set_row(row_index, 88)
            sheet.write_number(row_index, 0, row['ordinal'], cell)
            sheet.write_string(row_index, 1, row['record_id'], cell)
            sheet.write_string(row_index, 2, str(row.get('label', '')), cell)
            pictures = row.get('images', [])
            for picture_index, picture in enumerate(pictures):
                details = get_image_sheet(image_count // max_rows)
                details.write_row(image_count % max_rows + 1, 0, [row['ordinal'], row['record_id'], picture_index + 1,
                    str(picture.get('name', '图片'))[:32767], picture.get('verdict', '待确认'),
                    str(picture.get('reason', ''))[:32767],
                    json.dumps(picture.get('metrics', {}), ensure_ascii=False)[:32767],
                    json.dumps(picture.get('signals', []), ensure_ascii=False)[:32767],
                    str(picture.get('error', ''))[:32767]], cell)
                image_count += 1
                try:
                    art = database.artifact(job_id, picture['id'] + '-thumb')
                    with Image.open(art['path']) as image:
                        width, height = image.size
                    scale = min(100 / height, 200 / max(1, len(pictures)) / width)
                    sheet.insert_image(row_index, 3, art['path'], {'x_scale': scale, 'y_scale': scale,
                        'x_offset': int(picture_index * 200 / max(1, len(pictures))), 'y_offset': 6, 'object_position': 1,
                        'description': picture.get('name', '图片')})
                except (KeyError, OSError):
                    pass
            if not pictures:
                sheet.write_string(row_index, 3, '无可用图片', pending)
            sheet.write_string(row_index, 4, row.get('image_verdict', '待确认'), cell)
            sheet.write_string(row_index, 5, str(row.get('image_reason', '') or row.get('error', '') or '')[:32767], cell)
            sheet.write_string(row_index, 6, str(row.get('log_summary', '') or '')[:32767], cell)
            first_log = None
            for log in row.get('logs', []):
                try:
                    art = database.artifact(job_id, log['id'] + '-text')
                    chunk_index = 0
                    with open(art['path'], 'r', encoding='utf-8', newline='') as handle:
                        while chunk := handle.read(30000):
                            log_sheet_index = log_count // max_rows
                            log_row = log_count % max_rows + 1
                            log_sheet = get_log_sheet(log_sheet_index)
                            if first_log is None:
                                first_log = (log_sheet.get_name(), log_row + 1)
                            chunk_index += 1
                            log_sheet.write_row(log_row, 0, [row['record_id'], log.get('name', ''), chunk_index, chunk,
                                '完整' if log.get('complete') else '读取不完整'], cell)
                            log_count += 1
                except (KeyError, OSError):
                    continue
            if first_log:
                sheet.write_url(row_index, 7, f"internal:'{first_log[0]}'!A{first_log[1]}", link, '查看全文（先取消隐藏）')
            else:
                sheet.write_string(row_index, 7, '无可用日志', pending)
            urls = row.get('log_urls', [])
            if urls and str(urls[0]).startswith(('http://', 'https://')):
                sheet.write_url(row_index, 8, str(urls[0]), link, '打开原始日志链接')
            sheet.write_string(row_index, 9, row['status'] + ((' · ' + str(row['error'])) if row.get('error') else ''), cell)
            source_url = job['source'].get('url', '')
            if source_url.startswith(('http://', 'https://')):
                sheet.write_url(row_index, 10, source_url, link, '打开飞书表格')
        for index, sheet in sheets.items():
            count = min(max_rows, max(0, job['total'] - index * max_rows))
            sheet.autofilter(0, 0, count, 10)
    finally:
        book.close()
    temp.replace(destination)
    return destination
