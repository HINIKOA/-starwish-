"""星愿 StarWish · 向量模型（语义检索）测试。

用法：python tests/embed_test.py
需要：onnxruntime + tokenizers + numpy，以及内置模型 models/embed/small/（完整包自带）。
验证：三档选择与自动退回、真实 bge-small 把「保暖外套」连到「羽绒服」、字面命中仍排第一、
多模型向量并存、改星重算、接口档（Qwen3-Embedding，模拟）、接口出错时退回、BGE-M3 一键部署（本地模拟下载源 + 镜像兜底）、
旧向量表自动升级、设置页的「向量模型」与「视频交互模型」两块。
"""
import hashlib
import http.server
import os
import shutil
import socketserver
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="starwish-embed-"))
os.environ["STARWISH_SOUL_DIR"] = str(TMP / "soul")
os.environ["STARWISH_MODELS_DIR"] = str(TMP / "models")
(TMP / "soul").mkdir(parents=True)
SRC_SMALL = Path(os.environ.get("STARWISH_SMALL_SRC") or ROOT / "models" / "embed" / "small")
(TMP / "models" / "embed").mkdir(parents=True)
shutil.copytree(SRC_SMALL, TMP / "models" / "embed" / "small")

from smoke_test import check, RESULTS  # noqa: E402

DOCS = ["主人冬天总穿那件黑色羽绒服", "主人喜欢喝美式咖啡，不加糖", "主人养了一只叫团子的橘猫",
        "主人对花生过敏，点外卖要备注", "主人周末常去西湖边跑步", "主人在学吉他，每周练三次"]


def fake_api(base, key, body):
    """模拟 Qwen3-Embedding：按字的哈希做 64 维袋子向量（同字越多越像）。"""
    FAKE["calls"] += 1
    if FAKE.get("fail"):
        raise RuntimeError("接口超时（模拟）")
    import numpy as np
    out = []
    for t in body["input"]:
        v = np.zeros(int(body.get("dimensions") or 64), dtype="float32")
        for ch in t:
            v[int(hashlib.md5(ch.encode()).hexdigest(), 16) % len(v)] += 1
        out.append(v.tolist())
    FAKE["last"] = body
    return out


FAKE = {"calls": 0}


def serve(dirpath: Path):
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(dirpath), **k)  # noqa: E731
    srv = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def main():
    print("== 旧向量表自动升级")
    con = sqlite3.connect(TMP / "soul" / "soul.db")
    con.execute("CREATE TABLE embeddings (star_id INTEGER PRIMARY KEY, model TEXT NOT NULL, vec BLOB NOT NULL)")
    con.execute("INSERT INTO embeddings VALUES (999, 'BAAI/bge-small-zh-v1.5', x'00000000')")
    con.commit(); con.close()
    from app import config, semantic, stars, fusion
    from app.embed import models as em
    from app.db import get_conn
    with get_conn() as c:
        pk = [r[1] for r in c.execute("PRAGMA table_info(embeddings)").fetchall() if r[5]]
        kept = c.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    check("主键改成 (star_id, model)，旧数据保留", pk == ["star_id", "model"] and kept == 1, pk)

    print("== 默认：内置 bge-small 常驻")
    cfg = config.load_config()
    check("默认档位 small、语义检索默认开", cfg["embed"]["provider"] == "small" and cfg["memory"]["semantic"])
    check("运行库齐全", em.deps_ok(), em.dep_status())
    check("内置模型就位", em.installed("small"))
    act = semantic.active(cfg)
    check("实际在用 small", act and act[0] == "small" and act[1] == "small:bge-small-zh-v1.5", act)
    t = time.time()
    ids = [stars.add_star(d)["id"] for d in DOCS]
    check("写入 6 颗星时顺手算好向量（每条很快）", (time.time() - t) < 20 and semantic.coverage()["done"] == 6, semantic.coverage())

    print("== 语义检索：字面没对上也能找到")
    res = stars.search("保暖外套")
    check("「保暖外套」→ 排第一的是「羽绒服」", res and res[0]["id"] == ids[0], [(r["summary"], r["relevance"]) for r in res[:3]])
    check("「宠物」→ 橘猫", stars.search("宠物")[0]["id"] == ids[2])
    check("「饮食禁忌」→ 花生过敏", stars.search("饮食禁忌")[0]["id"] == ids[3])
    check("「运动习惯」→ 西湖跑步", stars.search("运动习惯")[0]["id"] == ids[4])
    rc = stars.recall("冬天穿什么", bump=False)
    check("召回（给 AI 的那一路）同样生效", any(x["id"] == ids[0] for x in rc["items"]))
    config.update_config({"memory": {"semantic": False}})
    check("关掉语义检索后，「保暖外套」字面找不到（证明是向量的功劳）", not any(r["id"] == ids[0] for r in stars.search("保暖外套")))
    config.update_config({"memory": {"semantic": True}})
    ex = stars.search("美式咖啡")
    check("字面完全命中仍然排第一、相关度最高", ex[0]["id"] == ids[1] and ex[0]["relevance"] >= max(r["relevance"] for r in ex))
    with get_conn() as c:
        junk = semantic.search(c, "量子色动力学的渐近自由", config.load_config())
        pet = semantic.search(c, "宠物", config.load_config())
    check("毫不相关的查询：语义这一路不硬凑结果", len(junk) == 0, junk)
    check("语义只留真正接近的（「宠物」只命中橘猫）", list(pet) == [ids[2]], pet)

    print("== 改星重算向量")
    with get_conn() as c:
        v0 = c.execute("SELECT vec FROM embeddings WHERE star_id=? AND model=?", (ids[5], act[1])).fetchone()[0]
    stars.update_star(ids[5], content="主人在学钢琴，每天练半小时")
    with get_conn() as c:
        v1 = c.execute("SELECT vec FROM embeddings WHERE star_id=? AND model=?", (ids[5], act[1])).fetchone()[0]
    check("内容改了 → 向量重算", v0 != v1)
    check("「乐器」→ 钢琴", stars.search("乐器")[0]["id"] == ids[5])

    print("== 融合星也有向量，融合判断可参考语义")
    f = fusion.fuse([ids[1], ids[3]], use_llm=False)
    with get_conn() as c:
        has = c.execute("SELECT 1 FROM embeddings WHERE star_id=? AND model=?", (f["id"], act[1])).fetchone()
        ps = semantic.pair_similarity(c, ids[0], ids[2])
    check("融合星有向量", bool(has))
    check("两颗星的语义相似度可算", ps is not None and -1 <= ps <= 1)
    fusion.unfuse(f["id"])

    print("== 档位 ②：BGE-M3 未部署 → 自动退回内置")
    config.update_config({"embed": {"provider": "m3"}})
    semantic.reset()
    a = semantic.active(config.load_config())
    check("退回 small 并说明原因", a[0] == "small" and "BGE-M3" in a[2], a)
    st = semantic.status()
    check("状态里显示 M3 未安装、需下载约 590MB", not st["local"]["m3"]["installed"] and st["local"]["m3"]["need_mb"] > 500)

    print("== 一键部署 BGE-M3（本地模拟下载源；主站失败自动换镜像）")
    src = TMP / "hf"
    d = src / "Xenova" / "bge-m3" / "resolve" / "main"
    (d / "onnx").mkdir(parents=True)
    shutil.copy(SRC_SMALL / "model.onnx", d / "onnx" / "model_quantized.onnx")   # 用小模型冒充，只测下载流程
    shutil.copy(SRC_SMALL / "tokenizer.json", d / "tokenizer.json")
    srv, base = serve(src)
    e = dict(config.load_config()["embed"], hf_base="http://127.0.0.1:9", hf_mirror=base)
    em.install("m3", e, deps=False, wait=True)
    j = em.jobs().get("model:m3", {})
    check("主站连不上 → 自动走镜像下载成功", em.installed("m3") and j.get("state") == "done", j)
    check("下载到程序目录 models/embed/m3（不进灵魂包）", (TMP / "models" / "embed" / "m3" / "model.onnx").exists()
          and not any((TMP / "soul").rglob("*.onnx")))
    semantic.reset()
    a = semantic.active(config.load_config())
    check("部署完立刻切到 M3", a and a[0] == "m3", a)
    semantic.start_backfill(wait=True)
    cov = semantic.coverage()
    check("后台把所有星补上 M3 向量", cov["done"] == cov["total"] and cov["total"] > 0, cov)
    with get_conn() as c:
        models = {r[0] for r in c.execute("SELECT DISTINCT model FROM embeddings")}
    check("small 与 M3 向量并存（切回去不用重算）", {"small:bge-small-zh-v1.5", "m3:bge-m3"} <= models, models)
    srv.shutdown()
    check("内置模型不能删", _raises(lambda: em.uninstall("small")))
    n = em.uninstall("m3")
    semantic.reset()
    check("删除 M3 → 又退回内置", n > 0 and not em.installed("m3") and semantic.active(config.load_config())[0] == "small")

    print("== 档位 ③：Qwen3-Embedding 接口（模拟）")
    semantic._api_hook = fake_api
    config.update_config({"embed": {"provider": "api", "api_key": "sk-test-123456789", "api_dimensions": 64}})
    semantic.reset()
    cfg = config.load_config()
    a = semantic.active(cfg)
    check("填了 Key → 用接口，模型键带维度", a and a[0] == "api" and a[1] == "api:text-embedding-v4:64", a)
    r = semantic.test_api(cfg)
    check("测试接口：返回维度 / 耗时 / 相似度", r["ok"] and r["dim"] == 64)
    calls = FAKE["calls"]
    sid = stars.add_star("主人新买了一双登山鞋")["id"]
    semantic.start_backfill(wait=True)
    with get_conn() as c:
        got = c.execute("SELECT COUNT(*) FROM embeddings WHERE model=?", (a[1],)).fetchone()[0]
    check("后台补齐接口向量，按 10 条一批", got == semantic.coverage()["total"] and len(FAKE["last"]["input"]) <= 10, got)
    check("请求带上 model 与 dimensions", FAKE["last"]["model"] == "text-embedding-v4" and FAKE["last"]["dimensions"] == 64)
    check("接口档也能搜到", any(x["id"] == sid for x in stars.search("登山鞋")))
    FAKE["fail"] = True
    stars.add_star("主人想去爬黄山")
    semantic.start_backfill(wait=True)
    a2 = semantic.active(config.load_config())
    check("接口出错 → 自动退回内置，不影响使用", a2 and a2[0] == "small" and "出错" in a2[2], a2)
    check("出错时搜索照常（字面 + 内置）", stars.search("羽绒服")[0]["id"] == ids[0])
    FAKE["fail"] = False
    semantic.reset()
    check("修好后 reset 即恢复接口档", semantic.active(config.load_config())[0] == "api")
    m = config.masked_config()
    check("向量接口 Key 按机密保存（界面只显示掩码）", m["embed"]["api_key"].startswith("****") and m["embed"]["api_key_set"])
    config.update_config({"embed": {"provider": "small"}})
    semantic.reset()

    print("== 接口与设置页")
    os.environ["STARWISH_NO_EMBED_BOOT"] = "1"
    from fastapi.testclient import TestClient
    from app.server import app
    with TestClient(app) as c:
        s = c.get("/api/embed/status").json()
        check("/api/embed/status：在用档位、三档状态、覆盖率", s["active"] == "small" and set(s["local"]) == {"small", "m3"} and "coverage" in s)
        check("装不存在的档位报 400", c.post("/api/embed/install", json={"kind": "xx"}).status_code == 400)
        check("删内置模型报 400", c.delete("/api/embed/models/small").status_code == 400)
        c.put("/api/settings", json={"embed": {"provider": "api"}})
        ev = c.get("/api/events?limit=50").json()
        check("切换档位记事件 embed_switch", any(x.get("type") == "embed_switch" for x in ev))
        check("测试接口", c.post("/api/embed/test").json().get("dim") == 64)
        c.put("/api/settings", json={"embed": {"provider": "small"}})
        check("统计里报当前向量档位", c.get("/api/stats").json().get("semantic_model") == "small")
    js = (ROOT / "web" / "app.js").read_text("utf-8") + (ROOT / "web" / "settings.js").read_text("utf-8")
    check("设置页有「向量模型」一块：三档 + 一键部署 + 测试接口", all(k in js for k in ("向量模型（语义检索）", "① 内置 bge-small-zh", "② BGE-M3", "③ 接口", "一键部署 BGE-M3", "/api/embed/test")))
    check("设置页有「视频交互模型」一块：引擎 / 帧模式 / 千问", all(k in js for k in ("视频交互模型（宇宙之眼）", "eye.engine", "frame_model", "eye.qwen_api_key")))
    req = (ROOT / "requirements.txt").read_text("utf-8")
    check("requirements 里加了 onnxruntime / tokenizers / numpy", all(k in req for k in ("onnxruntime", "tokenizers", "numpy")))

    shutil.rmtree(TMP, ignore_errors=True)
    ok = sum(1 for _, o, _ in RESULTS if o)
    for nme, o, dt in RESULTS:
        if not o:
            print("  ✘", nme, dt)
    print(f"\n向量模型结果：{ok}/{len(RESULTS)} 通过")
    return 0 if ok == len(RESULTS) else 1


def _raises(fn):
    try:
        fn()
    except Exception:
        return True
    return False


if __name__ == "__main__":
    sys.exit(main())
