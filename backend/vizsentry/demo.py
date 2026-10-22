"""Generated demonstration data; no remote table or AI requests."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


def create_demo(directory: Path) -> list[dict]:
    directory.mkdir(parents=True, exist_ok=True)
    image = Image.new('RGB', (1000, 620), '#f1f5f7')
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1000, 65), fill='#183644')
    draw.text((30, 22), 'SERVICE OVERVIEW / SYNTHETIC SAMPLE', fill='white', font_size=22)
    for index, (title, value) in enumerate([('Requests', '24,381'), ('Latency', '182 ms'), ('Success', '99.8%')]):
        left = 30 + index * 325
        draw.rounded_rectangle((left, 90, left + 290, 210), 12, fill='white')
        draw.text((left + 20, 112), title, fill='#6b7e86', font_size=20)
        draw.text((left + 20, 148), value, fill='#143b43', font_size=32)
    draw.rounded_rectangle((30, 235, 970, 585), 12, fill='white')
    for y in range(285, 560, 55):
        draw.line((65, y, 935, y), fill='#e9eff2', width=1)
    points = [(65 + i * 43, 490 - (i * 19 % 100) - i * 5) for i in range(21)]
    draw.line(points, fill='#17a99b', width=4)
    image.save(directory / 'dashboard.png')
    image.filter(ImageFilter.GaussianBlur(10)).save(directory / 'blur.png')
    Image.new('RGB', (1000, 620), 'white').save(directory / 'blank.png')
    (directory / 'broken.png').write_bytes(b'not-an-image')
    rows = []
    cases = [('demo-001', '运行概览', ['dashboard.png'], 'INFO request completed\nINFO status=200 elapsed_ms=182\n'),
             ('demo-002', '白屏示例', ['blank.png'], 'ERROR renderer initialization failed\nTraceback: CanvasContext unavailable\n'),
             ('demo-003', '模糊与原图', ['blur.png', 'dashboard.png'], 'WARN capture resized by upstream service\nINFO result uploaded\n'),
             ('demo-004', '缺少图片', [], 'INFO image field is empty\n'),
             ('demo-005', '损坏文件', ['broken.png'], 'ERROR asset checksum mismatch\n'),
             ('demo-006', '缺少日志', ['dashboard.png'], '')]
    for record_id, label, names, log in cases:
        path = directory / f'{record_id}.log'
        path.write_text(log, 'utf-8')
        rows.append({'record_id': record_id, 'label': label, 'images': [{'name': name, 'demo_path': str(directory / name)} for name in names],
                     'log_urls': [], 'demo_log': str(path) if log else None})
    return rows
