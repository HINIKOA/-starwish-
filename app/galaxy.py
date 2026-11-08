"""星图的银河系布局（v0.11.0）：
  银河系星系核 = 核心记忆（原点的金色主核星辰）
  恒星系       = 大类（cat_major，如 体育、代码）——绕星系核排开，星越多的恒星系越靠内
  行星系       = 小类（cat_minor，如 代码 > 环境调用）——聚在所属恒星系周围
  星球         = 一条记忆——绕所属行星系转，越新越靠近中心
位置完全由分类决定、可复现（同样的输入永远得到同样的坐标），大小 / 亮度仍由前端按新旧决定。
还没分类的星归入「未分类」恒星系。
"""
from __future__ import annotations

import math
import zlib

GOLDEN = math.pi * (3 - math.sqrt(5))   # 黄金角：均匀散开、不重叠
UNSORTED = "未分类"


def _h(s: str) -> float:
    return (zlib.crc32(s.encode("utf-8")) % 10000) / 10000


def layout(items: list[tuple]) -> dict:
    """items: [(星编号, 大类, 小类, 创建时间)]。
    返回 {"pos": {"编号": [x,y,z]}, "systems": [{"name", "center", "count", "radius", "minors": [{"name", "center", "count"}]}]}"""
    groups: dict[str, dict[str, list]] = {}
    for sid, major, minor, created in items:
        groups.setdefault((major or "").strip() or UNSORTED, {}).setdefault((minor or "").strip(), []).append((created, sid))
    order = sorted(groups, key=lambda m: (m == UNSORTED, -sum(len(v) for v in groups[m].values()), m))
    pos: dict[str, list] = {}
    cat: dict[str, list] = {}
    systems = []
    ring = 16.0          # 星系核周围留一圈空
    for i, major in enumerate(order):
        minors = groups[major]
        total = sum(len(v) for v in minors.values())
        # 恒星系的半径随星数增长；按序号一圈圈往外排，相邻的不重叠
        sys_r = 5 + 2.2 * math.sqrt(total)
        ring += sys_r
        ang = i * GOLDEN * 2.3 + _h(major) * 0.4
        cx, cz = math.cos(ang) * ring, math.sin(ang) * ring
        cy = (_h(major + "y") - 0.5) * 6
        ring += sys_r * 0.55 + 3       # 螺旋往外：相邻恒星系方向不同，半径也错开，不会重叠
        mlist = []
        morder = sorted(minors, key=lambda n: (n == "", -len(minors[n]), n))
        for j, minor in enumerate(morder):
            planets = sorted(minors[minor], reverse=True)          # 新的在前，离中心近
            if minor == "":
                mx, my, mz = cx, cy, cz
            else:
                mr = 3.5 + 2.4 * math.sqrt(j + 1)
                a2 = j * GOLDEN + _h(major + "/" + minor) * 0.5
                mx, my, mz = cx + math.cos(a2) * mr, cy + (_h(minor) - 0.5) * 2.5, cz + math.sin(a2) * mr
            for k, (_, sid) in enumerate(planets):
                pr = (0.9 if minor else 2.2) + 0.75 * math.sqrt(k)
                a3 = k * GOLDEN + _h(str(sid)) * 0.6
                cat[str(sid)] = [major, minor]
                pos[str(sid)] = [round(mx + math.cos(a3) * pr, 3), round(my + (_h(str(sid) + "y") - 0.5) * 1.2, 3),
                                 round(mz + math.sin(a3) * pr, 3)]
            if minor:
                mlist.append({"name": minor, "center": [round(mx, 3), round(my, 3), round(mz, 3)], "count": len(planets)})
        systems.append({"name": major, "center": [round(cx, 3), round(cy, 3), round(cz, 3)], "count": total,
                        "radius": round(sys_r, 2), "minors": mlist})
    return {"pos": pos, "cat": cat, "systems": systems}


def dist(a, b) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))
