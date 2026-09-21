# -*- coding: utf-8 -*-
"""make_icon.py —— 生成「DSH 启动器」的专属图标（纯标准库，无第三方依赖）。

产物：
    assets/launcher.ico        多尺寸 Windows 图标（16/24/32/48/64/128/256）
    assets/launcher-256.png    256×256 预览图（给人看的）
    assets/launcher-32-preview.png  32×32 预览图（只看「小尺寸下还清不清楚」）

用法：
    python tools/make_icon.py

画法（为什么这么写）：
- 先在 1024×1024 的「母图」上按**硬边**画（圆角矩形 + 竖向渐变 + 白色圆角三角），
  再逐级**盒式平均降采样**到各个目标尺寸。降采样本身带来抗锯齿，
  比在 Python 里逐像素算覆盖率快得多，也不需要任何图形库。
- 母图只算一次（约 100 万像素），所有尺寸都从它缩出来，保证各尺寸完全一致。
- 图案刻意简单：圆角底板 + 一个白色「启动」三角。16px 下三角形依然清晰可辨。
- 三角做了圆角（收边半平面 ∩ 顶点圆盘），避免尖角在小尺寸下糊成锯齿。
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

SIZES = (16, 24, 32, 48, 64, 128, 256)
MASTER = 1024

# 配色：上浅下深的蓝，与界面强调色同一族
TOP = (0x4C, 0x8D, 0xF6)
BOTTOM = (0x1B, 0x4F, 0xD8)
WHITE = (0xFF, 0xFF, 0xFF)

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"


# --------------------------------------------------------------------------- #
# 形状判断（母图上按硬边判断，抗锯齿交给降采样）
# --------------------------------------------------------------------------- #
def _inside_round_rect(x: float, y: float, size: float, radius: float) -> bool:
    """圆角矩形：把四角用圆盘裁掉。"""
    left, top = radius, radius
    right, bottom = size - radius, size - radius
    if left <= x <= right or top <= y <= bottom:
        return 0.0 <= x <= size and 0.0 <= y <= size
    cx = left if x < left else right
    cy = top if y < top else bottom
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


def _inside_rounded_triangle(x: float, y: float, pts, radius: float,
                             centers) -> bool:
    """圆角三角形 =（向内收 radius 的三角形）∪（内缩顶点处的圆盘）。"""
    for px, py in centers:                               # 切线圆角
        if (x - px) ** 2 + (y - py) ** 2 <= radius ** 2:
            return True
    cx = sum(p[0] for p in pts) / 3.0
    cy = sum(p[1] for p in pts) / 3.0
    for i in range(3):                                   # 三条边各自向内收
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % 3]
        nx, ny = -(by - ay), (bx - ax)                   # 边法线
        length = math.hypot(nx, ny) or 1.0
        nx, ny = nx / length, ny / length
        if (cx - ax) * nx + (cy - ay) * ny < 0:          # 让法线朝多边形内部
            nx, ny = -nx, -ny
        if (x - ax) * nx + (y - ay) * ny < radius:
            # 到这条边的「内向距离」不足 radius —— 落在被削掉的外圈里。
            # （符号写反过一次：变成只留下三个顶点圆盘、三角内部全空。）
            return False
    return True


def _triangle_geometry(pts, radius: float):
    """算出三个角各自的「切线圆角」几何量。

    正确画法不是「把三角形整体向内缩 radius 再补圆盘」——那样**直边也会一起缩进去**，
    看起来就是三个分离的鼓包（我连错两次都出在这个理解上）。
    正确做法是：**保留原来的三角形**，只在每个角上把「弦以外、圆弧以内」的那一小块削掉：
      - ``center``：圆心，位于角平分线上、距顶点 radius/sin(θ/2)
      - ``chord`` ：弦的位置（沿角平分线距顶点 radius·cos²(θ/2)/sin(θ/2)）
      点若在弦的「顶点侧」且到圆心距离大于 radius，就落在被削掉的那块里。
    """
    out = []
    for i, v in enumerate(pts):
        a = pts[(i - 1) % 3]
        b = pts[(i + 1) % 3]
        u1 = (a[0] - v[0], a[1] - v[1])
        u2 = (b[0] - v[0], b[1] - v[1])
        n1 = math.hypot(*u1) or 1.0
        n2 = math.hypot(*u2) or 1.0
        u1 = (u1[0] / n1, u1[1] / n1)
        u2 = (u2[0] / n2, u2[1] / n2)
        # u1 + u2 就是指向三角形内部的角平分线方向（凸多边形性质），不要再取反
        direction = (u1[0] + u2[0], u1[1] + u2[1])
        length = math.hypot(*direction) or 1.0
        direction = (direction[0] / length, direction[1] / length)
        cos_full = max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1]))
        cos_half = math.sqrt(max(0.0, (1.0 + cos_full) / 2.0))
        sin_half = math.sqrt(max(1e-9, (1.0 - cos_full) / 2.0))
        inset = radius / sin_half
        out.append({
            "v": v,
            "dir": direction,
            "center": (v[0] + direction[0] * inset, v[1] + direction[1] * inset),
            "chord": inset * cos_half ** 2,
        })
    return out


def _inside_polygon(x: float, y: float, pts, inner) -> bool:
    """凸多边形内部判断（三个半平面）。"""
    ix, iy = inner
    for i in range(3):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % 3]
        nx, ny = -(by - ay), (bx - ax)
        length = math.hypot(nx, ny) or 1.0
        nx, ny = nx / length, ny / length
        if (ix - ax) * nx + (iy - ay) * ny < 0:
            nx, ny = -nx, -ny
        if (x - ax) * nx + (y - ay) * ny < 0:
            return False
    return True


def _inside_rounded_triangle(x: float, y: float, pts, radius: float, geo,
                             inner) -> bool:
    """三角形内部，且不在任何一个被削掉的圆角块里。

    一个角要削掉某点，必须**同时**满足：① 点在弦的「顶点侧」；② 点在该角的圆弧之外。
    两个条件必须写在一起 —— 拆成两个分支写过一次，结果三角形中段被判成"被削掉"，
    整个图案只剩中间一小块。
    """
    if not _inside_polygon(x, y, pts, inner):
        return False
    for item in geo:
        vx, vy = item["v"]
        dx, dy = x - vx, y - vy
        if dx * item["dir"][0] + dy * item["dir"][1] < item["chord"]:
            cx, cy = item["center"]
            if (x - cx) ** 2 + (y - cy) ** 2 > radius ** 2:
                return False
    return True


def _master_rgba() -> bytearray:
    """画出 1024×1024 的母图（RGBA，逐行从上到下）。"""
    size = MASTER
    radius = size * 0.22                                  # 底板圆角
    plate = bytearray(size * size * 4)

    half = size * 0.20                                    # 三角半高
    cx = size / 2 + size * 0.015                          # 三角光学重心偏左，整体右移一点
    cy = size / 2
    left = cx - size * 0.17
    pts = ((left, cy - half), (cx + size * 0.17, cy), (left, cy + half))
    tri_radius = size * 0.04
    geo = _triangle_geometry(pts, tri_radius)
    inner = (sum(p[0] for p in pts) / 3.0, sum(p[1] for p in pts) / 3.0)

    for y in range(size):
        t = y / (size - 1)
        base = tuple(int(round(TOP[i] + (BOTTOM[i] - TOP[i]) * t)) for i in range(3))
        row = y * size * 4
        for x in range(size):
            idx = row + x * 4
            fx, fy = x + 0.5, y + 0.5
            if not _inside_round_rect(fx, fy, size, radius):
                continue                                  # 板外保持全透明
            if _inside_rounded_triangle(fx, fy, pts, tri_radius, geo, inner):
                plate[idx], plate[idx + 1], plate[idx + 2] = WHITE
            else:
                plate[idx], plate[idx + 1], plate[idx + 2] = base
            plate[idx + 3] = 255
    return plate


def _downsample(master: bytearray, src: int, dst: int) -> bytes:
    """盒式平均降采样；按 alpha 预乘，避免透明边缘出现黑边。"""
    out = bytearray(dst * dst * 4)
    scale = src / dst
    for dy in range(dst):
        y0 = int(dy * scale)
        y1 = max(y0 + 1, int((dy + 1) * scale))
        for dx in range(dst):
            x0 = int(dx * scale)
            x1 = max(x0 + 1, int((dx + 1) * scale))
            r = g = b = a = 0
            count = 0
            for sy in range(y0, y1):
                row = sy * src * 4
                for sx in range(x0, x1):
                    idx = row + sx * 4
                    alpha = master[idx + 3]
                    r += master[idx] * alpha
                    g += master[idx + 1] * alpha
                    b += master[idx + 2] * alpha
                    a += alpha
                    count += 1
            o = (dy * dst + dx) * 4
            if a:
                out[o] = min(255, int(round(r / a)))
                out[o + 1] = min(255, int(round(g / a)))
                out[o + 2] = min(255, int(round(b / a)))
            out[o + 3] = int(round(a / count))
    return bytes(out)


def _png(width: int, height: int, rgba: bytes) -> bytes:
    """按 PNG 规范打包（RGBA / 8 位 / filter 0）。"""
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)
        raw += rgba[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload +
                struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def _ico(images: list) -> bytes:
    """把若干 PNG 拼成 .ico（Vista 以后支持 PNG 载荷）。"""
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + count * 16
    entries, blobs = b"", b""
    for size, data in images:
        entries += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,          # 256 在 ICO 里要写成 0
            0 if size >= 256 else size,
            0, 0, 1, 32, len(data), offset,
        )
        blobs += data
        offset += len(data)
    return header + entries + blobs


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)
    print("渲染母图 %d×%d …" % (MASTER, MASTER))
    master = _master_rgba()

    images = []
    for size in SIZES:
        png = _png(size, size, _downsample(master, MASTER, size))
        images.append((size, png))
        if size == 256:
            (ASSETS / "launcher-256.png").write_bytes(png)
        if size == 32:
            (ASSETS / "launcher-32-preview.png").write_bytes(png)
        print(f"  {size:>3}×{size:<3} {len(png):>7,} 字节")

    ico_path = ASSETS / "launcher.ico"
    ico_path.write_bytes(_ico(images))
    print("已写出：")
    for path in (ico_path, ASSETS / "launcher-256.png", ASSETS / "launcher-32-preview.png"):
        print(f"  {path}  ({path.stat().st_size:,} 字节)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
