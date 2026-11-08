"""星愿 StarWish · 星图与星辰融合测试。

用法：python tests/fusion_test.py
验证：旧灵魂包自动升级、一开始只有主核星辰、按时间区分大小亮度的数据、相似记忆找组、融合（朴素 / LLM）、
融合星的时间与代数、成员不删除但不再参与召回、重复内容指向融合星、拆开还原、自动融合的触发条件、接口与前端。
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="starwish-fuse-"))
os.environ["STARWISH_SOUL_DIR"] = str(TMP / "soul")
(TMP / "soul").mkdir(parents=True)

from smoke_test import check, RESULTS  # noqa: E402

MEMS = ["主人喜欢喝美式咖啡，不加糖", "主人每天早上都要喝一杯美式咖啡", "主人最近改喝燕麦拿铁了，美式咖啡喝得少了",
        "主人养了一只叫团子的橘猫", "团子是主人的橘猫，很胖，爱睡觉", "主人在学吉他，每周练三次",
        "主人喜欢看科幻电影，最爱星际穿越", "主人在杭州工作，是一名产品经理", "主人周末常去西湖边跑步",
        "主人对花生过敏", "主人喜欢喝茶", "主人喜欢听周杰伦的歌"]


def main():
    print("== 旧灵魂包自动升级")
    db = TMP / "soul" / "soul.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE stars (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',"
                " tags TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, updated_at REAL, last_recalled_at REAL,"
                " recall_count INTEGER NOT NULL DEFAULT 0, importance INTEGER NOT NULL DEFAULT 3, boost REAL NOT NULL DEFAULT 1.0,"
                " source TEXT NOT NULL DEFAULT '', session TEXT NOT NULL DEFAULT '', hash TEXT NOT NULL DEFAULT '', deleted_at REAL,"
                " tok TEXT NOT NULL DEFAULT '')")
    con.commit()
    con.close()
    from app import stars, fusion, config
    from app.db import get_conn
    with get_conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(stars)").fetchall()}
    check("旧表自动加上 fused_into / generation / fused_from", {"fused_into", "generation", "fused_from"} <= cols)

    print("== 一开始只有主核星辰")
    check("新灵魂包没有任何记忆星", stars.star_map()["stars"] == [] and stars.stats()["total"] == 0)
    js = (ROOT / "web" / "app.js").read_text("utf-8")
    html = (ROOT / "web" / "index.html").read_text("utf-8")
    check("前端：不再铺 2500 颗背景星尘、旧的三星 + 金色星尘已去掉", "const N = 2500" not in js and "3 + 160" not in js)
    check("前端：主核星辰是一颗金色星（加一圈柔光）", "主核星辰" in js and "new Float32Array([16, 46])" in js)
    check("前端：空星图提示文案", 'id="emptyHint"' in html and "只有一颗主核星辰" in html)
    check("前端：大小 / 亮度按时间先后、融合飞入动画、诞生动画", all(k in js for k in ("recency", "buildGhosts", "stepAnims", '"birth"', '"fuse"')))

    print("== 使用中长出新星（时间先后）")
    t0 = time.time() - 86400 * 40
    for i, m in enumerate(MEMS):
        stars.add_star(m, created_at=t0 + i * 86400 * 3)
    mp = stars.star_map()["stars"]
    check("星图数据按时间排序，带代数与融合成员字段", len(mp) == 12 and all(len(x) == 8 for x in mp) and mp[0][1] < mp[-1][1])
    check("新星比老星亮（时间衰减）", mp[-1][2] > mp[0][2], (mp[0][2], mp[-1][2]))

    print("== 找相似：类似的记忆才会被归到一组")
    w = fusion.words("主人养了一只叫团子的橘猫")
    check("分词去掉「主人」这类到处都有的词，被切开的单字连成词", "主人" not in w and "橘猫" in w and "团子" in w, w)
    groups = fusion.find_groups()
    sets = [set(g["summaries"]) for g in groups]
    check("咖啡三颗归为一组", any({MEMS[0], MEMS[1], MEMS[2]} <= s for s in sets), str(groups))
    check("团子两颗归为一组", any({MEMS[3], MEMS[4]} <= s for s in sets))
    check("不相关的（喝茶 / 吉他 / 过敏）不会被硬凑", not any(MEMS[10] in s or MEMS[5] in s or MEMS[9] in s for s in sets))

    print("== 融合：朴素归纳")
    cat = next(g for g in groups if MEMS[3] in g["summaries"])
    f1 = fusion.fuse(cat["ids"], use_llm=False)
    check("融合出一颗新星，第 1 代，记下成员", f1["generation"] == 1 and sorted(f1["fused_from"]) == sorted(cat["ids"]) and "融合" in f1["tags"])
    check("朴素归纳保留每条原文", MEMS[3] in f1["content"] and MEMS[4] in f1["content"])
    newest_member = max(stars.get_star(i)["created_at"] for i in cat["ids"])
    check("新星的时间取最新的成员（所以和它一样大、一样亮）", abs(f1["created_at"] - newest_member) < 1)
    check("成员没有删除，只是被吸收", all(stars.get_star(i)["fused_into"] == f1["id"] and not stars.get_star(i)["deleted_at"] for i in cat["ids"]))
    mp2 = stars.star_map()["stars"]
    ids2 = [x[0] for x in mp2]
    check("星图上成员消失、融合星出现", f1["id"] in ids2 and not any(i in ids2 for i in cat["ids"]) and len(mp2) == 11)
    row = next(x for x in mp2 if x[0] == f1["id"])
    check("星图数据带代数与成员", row[6] == 1 and sorted(row[7]) == sorted(cat["ids"]))
    st = stars.stats()
    check("统计：总数按可见星算，另报融合星与被吸收数", st["total"] == 11 and st["fused"] == 1 and st["absorbed"] == 2, st)
    rec = stars.recall("团子 橘猫", bump=False)
    rids = [x["id"] for x in rec["items"]]
    check("召回命中融合星，不再重复返回成员（省 token）", f1["id"] in rids and not any(i in rids for i in cat["ids"]), rids)
    dup = stars.add_star(MEMS[4])
    check("再存一遍被吸收的原话 → 指向融合星，不重复长星", dup.get("duplicate") and dup["id"] == f1["id"])

    print("== 融合：LLM 归纳 + 进化")
    coffee = next(g for g in fusion.find_groups() if MEMS[0] in g["summaries"])
    seen = {}

    def fake_chat(msgs):
        seen["prompt"] = msgs[-1]["content"]
        return '{"summary": "主人的咖啡偏好", "content": "主人以前爱喝不加糖的美式，每天早上一杯；最近改喝燕麦拿铁（以此为准）。"}'
    f2 = fusion.fuse(coffee["ids"], use_llm=True, chat=fake_chat)
    check("LLM 归纳：提示里带全部成员与「以哪个为准」", all(m in seen["prompt"] for m in MEMS[:3]) and "以哪个为准" in seen["prompt"])
    check("LLM 归纳结果写进新星", f2["by_llm"] and f2["summary"] == "主人的咖啡偏好" and "燕麦拿铁" in f2["content"])
    stars.add_star("主人说燕麦拿铁要少冰", created_at=time.time())
    f3 = fusion.fuse([f2["id"], stars.star_map()["stars"][-1][0]], use_llm=False)
    check("融合星再融合 → 第 2 代（进化）", f3["generation"] == 2)
    check("已经被融合的星不能再单独融合", _raises(lambda: fusion.fuse(coffee["ids"][:2], use_llm=False)))
    check("只有一颗星不能融合", _raises(lambda: fusion.fuse([f3["id"]], use_llm=False)))

    print("== 拆开还原")
    un = fusion.unfuse(f3["id"])
    check("拆开后成员回到星图，融合星隐藏", f2["id"] in un["restored"] and stars.get_star(f3["id"])["deleted_at"]
          and f2["id"] in [x[0] for x in stars.star_map()["stars"]])
    plain = next(x[0] for x in stars.star_map()["stars"] if x[6] == 0)
    check("普通星不能拆", _raises(lambda: fusion.unfuse(plain)))
    check("成员详情可查", len(fusion.members(f2["id"])) == 3)

    print("== 自动融合")
    cfg = config.load_config()
    config.update_state(last_fuse_t=time.time())
    check("新星不够 8 颗时不触发", fusion.auto_fuse(cfg) == [])
    for k in range(8):
        stars.add_star(f"主人收藏了第{k + 1}张黑胶唱片，是爵士乐专辑《夜色{k}》", created_at=time.time() + k)
    done = fusion.auto_fuse(cfg)
    check("攒够 8 颗新星自动融合相似的那组（黑胶唱片）", done and any("黑胶" in d["star"]["content"] for d in done), str([d["members"] for d in done]))
    check("一组最多 6 颗", all(len(d["members"]) <= int(cfg["memory"]["fuse_max_group"]) for d in done))
    config.update_config({"memory": {"fuse_auto": False}})
    check("关掉自动融合后不触发（手动 force 仍可）", fusion.auto_fuse(config.load_config()) == [])
    config.update_config({"memory": {"fuse_auto": True}})

    print("== 接口")
    from fastapi.testclient import TestClient
    from app.server import app
    with TestClient(app) as c:
        pv = c.get("/api/stars/fuse/preview").json()
        check("预览接口不改动数据", "groups" in pv and stars.stats()["total"] == c.get("/api/stats").json()["total"])
        a = c.post("/api/stars", json={"content": "主人喜欢在雨天听黑胶唱片里的爵士乐"}).json()
        b = c.post("/api/stars", json={"content": "主人觉得下雨天配爵士乐黑胶唱片最舒服"}).json()
        r = c.post("/api/stars/fuse", json={"ids": [a["id"], b["id"]], "use_llm": False}).json()
        nid = r["fused"][0]["star"]["id"]
        d = c.get(f"/api/stars/{nid}").json()
        check("指定星融合 + 详情里列出融合前的记忆", len(d["members"]) == 2 and d["generation"] >= 1)
        check("拆开接口", c.post(f"/api/stars/{nid}/unfuse").status_code == 200 and c.post(f"/api/stars/{nid}/unfuse").status_code == 400)
        check("融合不存在的星报 400", c.post("/api/stars/fuse", json={"ids": [999999, 999998]}).status_code == 400)
        ev = c.get("/api/events?limit=500").json()
        check("事件日志记录 star_fuse / star_unfuse（含成员原文摘要）", any(e.get("type") == "star_fuse" and e.get("member_summaries") for e in ev)
              and any(e.get("type") == "star_unfuse" for e in ev))
        check("星图接口带新字段", all(len(x) == 8 for x in c.get("/api/stars/map").json()["stars"]))

    shutil.rmtree(TMP, ignore_errors=True)
    ok = sum(1 for _, o, _ in RESULTS if o)
    for n, o, dt in RESULTS:
        if not o:
            print("  ✘", n, dt)
    print(f"\n星图与融合结果：{ok}/{len(RESULTS)} 通过")
    return 0 if ok == len(RESULTS) else 1


def _raises(fn):
    try:
        fn()
    except Exception:
        return True
    return False


if __name__ == "__main__":
    sys.exit(main())
