"""星愿记忆 v0.11.0 测试：对话监控 / 钩子 / 任务抽屉 / 对话抽屉 / 星系整理 / 上下文预算与自动压缩 / 数据同步。

不联网：整理模型、主模型都用替身；云服务器用本地模拟。用法：python tests/watch_test.py
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="starwish-watch-"))
HOME = TMP / "home"
os.environ["STARWISH_SOUL_DIR"] = str(TMP / "soul")
os.environ["STARWISH_HOME"] = str(HOME)
os.environ["STARWISH_NO_EMBED_BOOT"] = "1"
os.environ["STARWISH_NO_WATCH"] = "1"
os.environ["STARWISH_NO_DAILY"] = "1"
for k in ("CLAUDE_CONFIG_DIR", "QODER_CONFIG_DIR", "WORKBUDDY_CONFIG_DIR", "CODEBUDDY_CONFIG_DIR", "CODEX_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "STARWISH_EXE"):
    os.environ.pop(k, None)

from smoke_test import check, RESULTS, free_port  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "watch"
CALLS = {"extract": [], "merge": 0, "main": []}


def fake_llm(messages, cfg):
    p = messages[-1]["content"]
    if "记录碎片" in p:
        CALLS["merge"] += 1
        return json.dumps({"title": "build 支持 --release", "summary": "目标：build.py 与打包脚本支持 --release；步骤：改 build.py、改 pack.py、跑测试。",
                           "reusable": True, "skill_name": "release-build", "skill_description": "需要给项目加 release 构建参数时用",
                           "skill_body": "## 步骤\n1. 改 build.py 解析 --release\n2. 改 pack.py\n3. 跑测试"}, ensure_ascii=False)
    CALLS["extract"].append(p)
    user = p.split("【主人】", 1)[1].split("【AI】", 1)[0]
    items = []
    if "以后回答我都用中文" in user:
        items += [{"kind": "preference", "text": "回答和代码注释都用中文", "category": "编程", "importance": 5},
                  {"kind": "task", "text": "build.py 已加 --release，打包脚本还没改", "category": "编程", "task_key": "build-release", "task_status": "open"}]
    elif "打包脚本" in user:
        assert "build-release" in p, "未闭环任务摘要应传给监控 AI"
        items += [{"kind": "task", "text": "打包脚本改好，测试通过", "category": "编程", "task_key": "build-release", "task_status": "closed"}]
    elif "pnpm" in user:
        items += [{"kind": "preference", "text": "主人习惯用 pnpm，不用 npm", "category": "工具", "importance": 3}]
    elif "周会" in user:
        items += [{"kind": "fact", "text": "主人团队每周五下午三点开周会", "category": "工作", "importance": 3}]
    elif "/health" in user:
        items += [{"kind": "task", "text": "给 /health 加单元测试", "category": "编程", "task_key": "health-test", "task_status": "failed"}]
    elif "日志级别" in user:
        items += [{"kind": "fact", "text": "dsh 项目日志级别默认 info", "category": "编程", "importance": 2}]
    elif "OpenCode 里" in user:
        items += [{"kind": "fact", "text": "主人在 OpenCode 里用 Go 写服务", "category": "编程", "importance": 3}]
    else:
        items += [{"kind": "bogus", "text": "x"}, {"kind": "fact", "text": ""}]
    return json.dumps({"items": items}, ensure_ascii=False)


def main():
    from app import config, context, datasync, galaxy, jobs, organize, stars
    from app.watch import adapters, extract, hooks, notify, store, watcher as W
    from app.server import app
    from fastapi.testclient import TestClient

    extract.llm = fake_llm
    config.update_config({"llm": {"base_url": "http://127.0.0.1:9/v1", "api_key": "sk-test", "model": "m"},
                          "watch": {"organize_after": False, "idle_sec": 30}})
    c = TestClient(app)

    # ---------- 1. 解析各客户端 ----------
    print("\n[解析]")
    t, safe = adapters.parse("claude", (FIX / "claude_session.jsonl").read_bytes(), 0, {}, FIX / "claude_session.jsonl")
    check("Claude：两轮，工具结果 / isMeta / 子代理行不算新一轮", len(t) == 2 and t[0].turn_complete and not t[1].turn_complete)
    check("Claude：工具摘要含文件路径", "Edit(/home/u/proj/build.py)" in t[0].tool_summary)
    check("Claude：未完成的最后一轮 → 安全偏移停在它开头", safe == t[1].start)
    q, _ = adapters.parse("qoder", (FIX / "qoder_session.jsonl").read_bytes(), 0, {}, FIX / "qoder_session.jsonl")
    check("Qoder：Claude 同款格式", q and "pnpm" in q[0].user_text and q[0].assistant_text)
    wb, _ = adapters.parse("workbuddy", (FIX / "workbuddy_session.jsonl").read_bytes(), 0, {}, FIX / "workbuddy_session.jsonl")
    check("WorkBuddy：Responses 风格记录 + function_call", wb and wb[0].session_id == "wb-sess-0001" and "read_file" in wb[0].tool_summary)
    cx_path = next(FIX.glob("rollout-*.jsonl"))
    meta = {}
    cx, _ = adapters.parse("codex", cx_path.read_bytes(), 0, meta, cx_path)
    check("Codex：event_msg 用户消息、task_complete 结束、环境上下文不算用户话",
          len(cx) == 1 and cx[0].turn_complete and cx[0].user_text == "给 /health 接口加单元测试" and cx[0].cwd == "/home/u/api")
    check("Codex：session_meta 的 id", meta.get("session_id") == "019a0000-aaaa-7bbb-8ccc-codex0000001")
    ds, _ = adapters.parse("dsh", (FIX / "dsh_session.jsonl").read_bytes(), 0, {}, FIX / "dsh_session.jsonl")
    check("dsh（实验性）：text-chunks 拼回、turn-end 结束", ds and ds[0].assistant_text == "已改成 info。" and ds[0].turn_complete)
    half = (FIX / "claude_session.jsonl").read_bytes()[:-5]
    t2, safe2 = adapters.parse("claude", half, 0, {}, FIX / "claude_session.jsonl")
    check("写了一半的行不读", safe2 <= len(half) and all(x.end <= len(half) for x in t2))
    from app.watch.turns import Turn, is_trivial, clip
    check("琐碎一轮跳过、干了活的不跳过", is_trivial(Turn("claude", user_text="好的", assistant_text="嗯")) and
          not is_trivial(Turn("claude", user_text="好", tool_summary="Bash(ls)")))
    check("长文本保留头尾", len(clip("a" * 5000 + "结论", 200)) <= 200 and clip("a" * 5000 + "结论", 200).endswith("结论"))

    # ---------- 2. 监控：偏移量、未完成轮次、闭环 ----------
    print("\n[监控]")
    proj = HOME / ".claude" / "projects" / "-home-u-proj"
    proj.mkdir(parents=True)
    old = proj / "old.jsonl"
    old.write_bytes((FIX / "qoder_session.jsonl").read_bytes())
    w = W.get()
    w.poll_once()
    check("第一次启用：已有对话从末尾开始（不回灌历史）", w.state.files[str(old)]["o"] == old.stat().st_size)
    check("没有新内容就不调模型", w.drain() == 0 and not CALLS["extract"])
    live = proj / "0d7f6a2e-1111-4c3b-9a55-claude000001.jsonl"
    lines = (FIX / "claude_session.jsonl").read_bytes().splitlines(keepends=True)
    live.write_bytes(b"".join(lines[:7]))       # 第一轮 + 一行元信息
    w.last_rescan.clear()
    w.poll_once()
    w.drain()
    fs = w.state.files[str(live)]
    check("新会话文件从头读；第一轮还没确认结束 → 不处理", not CALLS["extract"] and fs["o"] == 0 and fs["p"])
    live.write_bytes(b"".join(lines))           # 第二轮的用户消息出现 → 第一轮结束
    w.poll_once()
    w.drain()
    check("出现下一条用户消息 → 第一轮被归纳", len(CALLS["extract"]) == 1)
    props = c.get("/api/proposals").json()
    check("重要偏好 → 核心提案（不直接改核心）", any("中文" in p["content"] for p in props) and "中文" not in c.get("/api/core").json()["text"])
    tk = store.list_tasks()
    check("任务碎片：未闭环 + 类别", tk and tk[0]["status"] == "open" and tk[0]["category"] == "编程")
    st_open = stars.get_star(tk[0]["star_id"])
    check("未闭环任务对应一颗带「未闭环」标签的星", "未闭环" in st_open["tags"])
    off_before = fs["o"]
    w.poll_once(now=time.time() + 5)
    check("最后一轮静止不够久 → 不处理", w.drain() == 0)
    w.poll_once(now=time.time() + 120)
    w.drain()
    tk = store.get_task(tk[0]["id"])
    check("静止超过 idle_sec → 最后一轮也处理；未闭环摘要传给监控 AI", len(CALLS["extract"]) == 2 and fs["o"] > off_before)
    check("任务闭环：碎片合并、状态 closed", tk["status"] == "closed" and len(tk["fragments"]) == 2 and CALLS["merge"] == 1)
    from app.evolve import skills
    check("可复用的任务 → 存成技能", tk["skill_name"] == "release-build" and skills.find("release-build"))
    st_done = stars.get_star(tk["star_id"])
    check("任务星改为「任务」标签", "任务" in st_done["tags"] and "未闭环" not in st_done["tags"])
    w.poll_once(now=time.time() + 300)
    check("处理过的不再重复读", w.drain() == 0 and len(CALLS["extract"]) == 2)
    offs = json.loads((TMP / "soul" / "watch" / "offsets.json").read_text("utf-8"))
    check("偏移量落盘", offs["files"][str(live)]["o"] == live.stat().st_size)

    # 重启后补读（关着时聊的不丢）
    W.reset()
    with open(live, "ab") as f:
        for r in [{"type": "user", "sessionId": "s", "cwd": "/home/u/proj", "uuid": "x1", "timestamp": "2026-11-08T05:00:00Z",
                   "message": {"role": "user", "content": "我们团队周五下午三点开周会，帮我记一下"}},
                  {"type": "assistant", "sessionId": "s", "uuid": "x2", "timestamp": "2026-11-08T05:00:02Z",
                   "message": {"role": "assistant", "content": [{"type": "text", "text": "好的，已记下。"}]}}]:
            f.write((json.dumps(r, ensure_ascii=False) + "\n").encode())
    os.utime(live, (time.time() - 3600,) * 2)       # 关着软件期间写的，早就静止了
    w = W.get()
    w.poll_once()
    w.drain()
    check("重启后从保存的偏移量补读", len(CALLS["extract"]) == 3 and any("周会" in s["content"] for s in stars.list_stars(50)))

    # 其他客户端 + notify
    cdir = HOME / ".codex" / "sessions" / datetime.now().strftime("%Y/%m/%d")
    cdir.mkdir(parents=True)
    w.poll_once()                                # 初始化 Codex（目录还空）
    shutil.copy(cx_path, cdir / cx_path.name)
    w.last_rescan.clear()
    w.poll_once()
    w.drain()
    check("Codex：task_complete 立即处理", any("/health" in p for p in CALLS["extract"]))
    hk = [x for x in store.list_tasks() if x["task_key"] == "health-test"]
    check("失败的任务 → failed（红）", hk and hk[0]["status"] == "failed")

    wbd = HOME / ".workbuddy" / "projects" / "-home-u-docs"
    wbd.mkdir(parents=True)
    w.poll_once()
    shutil.copy(FIX / "workbuddy_session.jsonl", wbd / "wb-sess-0001.jsonl")
    n0 = len(CALLS["extract"])
    r = c.post("/api/watch/notify", json={"client": "workbuddy", "payload": {"session_id": "wb-sess-0001",
               "transcript_path": str(wbd / "wb-sess-0001.jsonl"), "last_assistant_message": "周报已写好"}}).json()
    check("notify：在客户端目录下 → 排队（延迟 1.5 秒等文件写完）", r.get("ok") and r.get("queued"))
    w.drain()
    check("notify：本轮立即视为结束", len(CALLS["extract"]) == n0 + 1)
    bad = c.post("/api/watch/notify", json={"client": "claude", "transcript_path": "/etc/passwd"}).json()
    check("notify：不在客户端目录下的路径不读", not bad.get("ok"))
    check("notify：没打开监控的客户端忽略", not c.post("/api/watch/notify", json={"client": "codebuddy"}).json().get("ok"))

    # OpenCode SQLite（1.x）
    oc = HOME / ".local" / "share" / "opencode"
    oc.mkdir(parents=True)
    db = sqlite3.connect(oc / "opencode.db")
    db.executescript("""CREATE TABLE session(id TEXT PRIMARY KEY, directory TEXT, time_created INTEGER);
      CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
      CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);""")
    db.commit()
    w.poll_once()
    now_ms = int(time.time() * 1000)
    db.execute("INSERT INTO session VALUES ('ses_1','/home/u/go',?)", (now_ms,))
    db.execute("INSERT INTO message VALUES ('msg_1','ses_1',?,?,?)", (now_ms + 10, now_ms + 10, json.dumps({"role": "user"})))
    db.execute("INSERT INTO part VALUES ('prt_1','msg_1','ses_1',?,?,?)", (now_ms + 10, now_ms + 10, json.dumps({"type": "text", "text": "我在 OpenCode 里用 Go 写服务"})))
    db.execute("INSERT INTO message VALUES ('msg_2','ses_1',?,?,?)", (now_ms + 20, now_ms + 20, json.dumps({"role": "assistant", "time": {"created": now_ms + 20, "completed": now_ms + 30}})))
    db.execute("INSERT INTO part VALUES ('prt_2','msg_2','ses_1',?,?,?)", (now_ms + 20, now_ms + 20, json.dumps({"type": "text", "text": "好的。"})))
    db.execute("INSERT INTO part VALUES ('prt_3','msg_2','ses_1',?,?,?)", (now_ms + 21, now_ms + 21, json.dumps({"type": "tool", "tool": "bash", "state": {"input": {"command": "go build"}}})))
    db.commit()
    db.close()
    n0 = len(CALLS["extract"])
    r = c.post("/api/watch/notify", json={"client": "opencode", "payload": {"session_id": "ses_1"}}).json()
    w.drain()
    check("OpenCode 1.x：只读打开、按会话游标读出一轮（含工具）", len(CALLS["extract"]) == n0 + 1 and "go build" in CALLS["extract"][-1])
    w.poll_once(now=time.time() + 600)
    dbg = dict(w.state.dbs)
    check("OpenCode：游标推进，不重复", w.drain() == 0 and len(CALLS["extract"]) == n0 + 1, str(dbg))
    # 没配整理模型：不推进偏移
    config.update_config({"llm": {"api_key": ""}})
    with open(live, "ab") as f:
        f.write((json.dumps({"type": "user", "sessionId": "s", "uuid": "y1", "message": {"role": "user", "content": "我习惯用 pnpm 装依赖"}}, ensure_ascii=False) + "\n").encode())
        f.write((json.dumps({"type": "user", "sessionId": "s", "uuid": "y2", "message": {"role": "user", "content": "还有，周末不要提醒我工作"}}, ensure_ascii=False) + "\n").encode())
    o1 = w.state.files[str(live)]["o"]
    w.poll_once()
    w.drain()
    check("没配整理模型 → 不推进偏移、状态里说明", w.state.files[str(live)]["o"] == o1 and "整理模型" in w.public()["waiting"])
    config.update_config({"llm": {"api_key": "sk-test"}})
    r = c.post("/api/watch/process").json()
    check("立即归纳：配好后补上", r["ok"] and r["turns"] >= 1)

    # 模型出错重试 3 次后跳过
    def boom(m, cfg):
        raise RuntimeError("上游 500")
    extract.llm = boom
    with open(live, "ab") as f:
        f.write((json.dumps({"type": "user", "sessionId": "s", "uuid": "z1", "message": {"role": "user", "content": "记住我对花生过敏，很严重"}}, ensure_ascii=False) + "\n").encode())
        f.write((json.dumps({"type": "user", "sessionId": "s", "uuid": "z2", "message": {"role": "user", "content": "另一件事"}}, ensure_ascii=False) + "\n").encode())
    o2 = w.state.files[str(live)]["o"]
    for i in range(3):
        w.process_file(str(live))
    check("模型出错：前两次不推进，第三次跳过这一轮", w.state.files[str(live)]["o"] > o2)
    extract.llm = fake_llm
    jl = jobs.list_jobs(100)
    check("归纳任务记进任务抽屉（含失败）", any(j["kind"] == "watch" and j["status"] == "done" for j in jl) and any(j["status"] == "failed" for j in jl))

    # inbox 补读
    with open(TMP / "soul" / "watch" / "inbox.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps({"client": "claude", "payload": {"session_id": "0d7f6a2e-1111-4c3b-9a55-claude000001"}}) + "\n")
    check("服务没开时的提醒（inbox）启动后补上并清空", w.replay_inbox() == 1 and not (TMP / "soul" / "watch" / "inbox.jsonl").exists())

    # ---------- 3. 钩子 ----------
    print("\n[钩子]")
    sp = HOME / ".claude" / "settings.json"
    sp.write_text(json.dumps({"model": "x", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}), "utf-8")
    r = c.post("/api/watch/client/claude", json={"hook": True}).json()
    d = json.loads(sp.read_text("utf-8"))
    cmds = [h["command"] for g in d["hooks"]["Stop"] for h in g["hooks"]]
    check("Claude 钩子：保留原有钩子、加上 --notify claude、有备份", "echo hi" in cmds and any("--notify claude" in x for x in cmds) and r["hook_result"]["backup"])
    c.post("/api/watch/client/claude", json={"hook": True})
    d = json.loads(sp.read_text("utf-8"))
    check("重复安装不重复添加（幂等）", sum("--notify" in h["command"] for g in d["hooks"]["Stop"] for h in g["hooks"]) == 1)
    st = c.get("/api/watch/status").json()
    check("状态里显示钩子已装", next(x for x in st["clients"] if x["id"] == "claude")["hook_installed"])
    c.post("/api/watch/client/claude", json={"hook": False})
    d = json.loads(sp.read_text("utf-8"))
    check("卸载干净：只删星愿的，别的设置都在", d == {"model": "x", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}})
    (HOME / ".qoder").mkdir()
    (HOME / ".qoder" / "settings.json").write_text("{坏的 json", "utf-8")
    r = c.post("/api/watch/client/qoder", json={"hook": True})
    check("配置文件不是合法 JSON → 拒绝改写", r.status_code == 400 and (HOME / ".qoder" / "settings.json").read_text("utf-8") == "{坏的 json")
    (HOME / ".codex" / "config.toml").write_text('model = "gpt-5"\n\n[mcp_servers.x]\ncommand = "y"\n', "utf-8")
    c.post("/api/watch/client/codex", json={"hook": True})
    toml = (HOME / ".codex" / "config.toml").read_text("utf-8")
    check("Codex notify：顶层（在第一个表之前）、带标记", toml.index("notify = [") < toml.index("[mcp_servers") and "# starwish-notify" in toml)
    try:
        import tomllib
        parsed = tomllib.loads(toml)
        check("Codex config.toml 仍是合法 TOML，notify 是数组", isinstance(parsed["notify"], list) and parsed["notify"][-2:] == ["--notify", "codex"])
    except ImportError:
        pass
    c.post("/api/watch/client/codex", json={"hook": False})
    check("Codex 卸载后恢复原样", (HOME / ".codex" / "config.toml").read_text("utf-8") == 'model = "gpt-5"\n\n[mcp_servers.x]\ncommand = "y"\n')
    (HOME / ".codex" / "config.toml").write_text('notify = ["my-notifier"]\n', "utf-8")
    check("Codex 已有别人的 notify → 不覆盖", c.post("/api/watch/client/codex", json={"hook": True}).status_code == 400)
    c.post("/api/watch/client/opencode", json={"hook": True})
    js = hooks.opencode_plugin_path().read_text("utf-8")
    check("OpenCode 插件：session.idle 时调用 --notify opencode", "session.idle" in js and '"--notify", "opencode"' in js)
    c.post("/api/watch/client/opencode", json={"hook": False})
    check("OpenCode 插件卸载", not hooks.opencode_plugin_path().exists())
    r = c.post("/api/watch/client/dsh", json={"hook": True}).json()
    check("dsh：给出桥接说明（实验性）", r["hook_result"].get("manual") and "dsh-hooks-claude-code" in r["hook_result"]["help"])
    check("路径覆盖保存", c.post("/api/watch/client/dsh", json={"path": str(TMP / "dsh")}).json()["clients"][-1]["path"] == str(TMP / "dsh"))

    # --notify 命令行：服务不在 → 写 inbox，0 退出，很快
    env = {**os.environ, "STARWISH_SOUL_DIR": str(TMP / "soul2")}
    t0 = time.time()
    p = subprocess.run([sys.executable, str(ROOT / "desktop.py"), "--notify", "claude"], input=json.dumps({"session_id": "abc"}).encode(),
                       env=env, capture_output=True, timeout=30)
    inbox = TMP / "soul2" / "watch" / "inbox.jsonl"
    check("--notify：服务没开 → 0 退出、写进 inbox", p.returncode == 0 and inbox.exists() and '"abc"' in inbox.read_text("utf-8"), p.stderr.decode()[-300:])
    check("--notify：几秒内结束，不阻塞客户端", time.time() - t0 < 15)
    p = subprocess.run([sys.executable, str(ROOT / "desktop.py"), "--notify", "codex", json.dumps({"type": "agent-turn-complete", "thread-id": "t9"})],
                       env=env, capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
    check("--notify codex：JSON 在最后一个参数里", p.returncode == 0 and '"t9"' in inbox.read_text("utf-8"))
    # 服务在：POST 过去
    import uvicorn
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    config.update_state(run_port=port)
    check("--notify：服务在 → 直接 POST", notify.send("claude", {"session_id": "nope"}) == "sent")
    srv.should_exit = True
    src = (ROOT / "tools" / "launcher" / "onefile.c").read_text("utf-8") + (ROOT / "tools" / "launcher" / "launcher.c").read_text("utf-8")
    check("启动器：--notify 继承标准输入输出", src.count("--notify") >= 2 and "console" in src)

    # ---------- 4. 任务抽屉 ----------
    print("\n[任务]")
    r = c.get("/api/tasks").json()
    check("/api/tasks：星愿自己的任务 + 捕获的任务", r["jobs"] and r["client_tasks"] and "closed" in r["counts"])
    hid = hk[0]["id"]
    r = c.post(f"/api/tasks/client/{hid}", json={"status": "open"}).json()
    check("主人可以重新打开任务", r["status"] == "open")
    r = c.post(f"/api/tasks/client/{hid}", json={"status": "closed"}).json()
    check("主人标记完成 → 合并闭环", r["status"] == "closed")
    check("非法状态被拒", c.post(f"/api/tasks/client/{hid}", json={"status": "x"}).status_code == 400)

    # ---------- 5. 上下文预算与自动压缩 ----------
    print("\n[上下文]")
    cfg = config.load_config()
    check("默认上下文窗口 400,000、压缩阈值 380,000", context.window(cfg) == 400000 and context.compact_threshold(cfg) == 380000)
    msgs = [{"role": "user", "content": "字" * 1000} for _ in range(10)]
    check("按预算从最新往回保留", len(context.trim_history(msgs, 3100)) == 3 and context.trim_history(msgs, 10)[-1] is msgs[-1])
    small = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "在"}] * 3
    h2, info = context.compact_if_needed(small, cfg)
    check("没到阈值不压缩", info is None and h2 is small)
    config.update_config({"llm": {"compact_tokens": 5000, "context_tokens": 20000}})
    cfg = config.load_config()
    big = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"第{i}条 " + "记" * 600} for i in range(12)]
    h3, info = context.compact_if_needed(big, cfg, summarize=lambda p: "主人要求：记住第0条到第8条的要点")
    check("累计到阈值 → 旧历史压成摘要 + 保留最近几轮", info and h3[0]["role"] == "system" and "摘要" in h3[0]["content"]
          and h3[-1] == big[-1] and len(h3) < len(big) and info["after"] < info["before"])
    check("压缩记进任务抽屉", any(j["kind"] == "compact" for j in jobs.list_jobs(50)))
    h4, info4 = context.compact_if_needed(big, cfg, summarize=lambda p: (_ for _ in ()).throw(RuntimeError("x")))
    check("模型压缩失败 → 朴素摘要兜底", info4 and not info4["by_llm"] and h4[0]["content"].startswith(context.SUMMARY_TAG))
    check("阈值不会超过窗口", context.compact_threshold({"llm": {"compact_tokens": 999999, "context_tokens": 10000}}) == 10000)

    # ---------- 6. 对话抽屉（有上下文） ----------
    print("\n[对话抽屉]")
    seen = []

    def fake_main(messages, cfg, tools=None, temperature=0.2):
        seen.append(messages)
        last = messages[-1]
        if last["role"] == "tool":
            return {"choices": [{"message": {"role": "assistant", "content": "已记下，编号见上。"}}]}
        if "记住" in str(last["content"]):
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "add_star", "arguments": json.dumps({"content": "主人喜欢喝无糖乌龙茶", "tags": ["饮食"], "major": "生活", "minor": "饮食"})}},
                {"id": "c2", "type": "function", "function": {"name": "update_core", "arguments": json.dumps({"section": "preferences", "content": "喝无糖乌龙茶", "mode": "append"})}}]}}]}
        return {"choices": [{"message": {"role": "assistant", "content": "你刚才让我记住乌龙茶。" if any("乌龙茶" in str(m.get("content")) for m in messages) else "嗯"}}]}
    context.main_complete = fake_main
    config.update_config({"llm": {"compact_tokens": 380000, "context_tokens": 400000}})
    r = c.post("/api/agent/chat", json={"text": "记住我喜欢喝无糖乌龙茶"}).json()
    check("对话抽屉：工具调用新增星（带大类 / 小类）", r["ok"] and r["actions"][0]["tool"] == "add_star" and r["actions"][0]["ok"])
    check("面板是主人通道：改核心直接生效", "乌龙茶" in c.get("/api/core").json()["text"])
    r = c.post("/api/agent/chat", json={"text": "我刚才说了什么？"}).json()
    check("有上下文：第二句能看到第一句", "乌龙茶" in r["reply"])
    hist = c.get("/api/agent/history").json()
    check("历史持久化 + 显示 token 与压缩阈值", len(hist["history"]) == 4 and hist["compact_at"] == 380000)
    config.update_config({"llm": {"compact_tokens": 2000, "context_tokens": 8000}})
    agent_hist = json.loads((TMP / "soul" / "agent" / "chat.json").read_text("utf-8"))
    agent_hist = [{"role": "user", "content": "旧话 " + "长" * 900}, {"role": "assistant", "content": "好"}] * 3 + agent_hist
    (TMP / "soul" / "agent" / "chat.json").write_text(json.dumps(agent_hist, ensure_ascii=False), "utf-8")
    r = c.post("/api/agent/chat", json={"text": "继续"}).json()
    check("对话抽屉累计到阈值 → 自动压缩", r.get("compacted") and json.loads((TMP / "soul" / "agent" / "chat.json").read_text("utf-8"))[0]["role"] == "system")
    config.update_config({"llm": {"compact_tokens": 380000, "context_tokens": 400000}})
    c.delete("/api/agent/history")
    config.update_config({"llm": {"api_key": "", "base_url": ""}})
    r = c.post("/api/agent/chat", json={"text": "/状态"}).json()
    check("没配主模型：离线指令 /状态 可用", r["ok"] and "星星" in r["reply"])
    r = c.post("/api/agent/chat", json={"text": "帮我整理"}).json()
    check("没配主模型：提示去配置", not r["ok"] and "默认模型" in r["reply"])
    config.update_config({"llm": {"api_key": "sk-test", "base_url": "http://127.0.0.1:9/v1"}})
    check("对话指令记进任务抽屉", any(j["kind"] == "agent" for j in jobs.list_jobs(100)))

    # ---------- 7. 星系整理（分类 + 布局） ----------
    print("\n[星系]")
    for txt, tags in [("主人周末踢足球", ["足球"]), ("主人看 NBA", ["篮球"]), ("项目用 conda 管环境", ["环境"]), ("venv 放在 .venv", ["环境"]),
                      ("提交前跑 pre-commit", ["git"])]:
        stars.add_star(txt, "", tags, 3, source="test")
    org_hist = []

    def fake_org(messages, cfg, tools=None, temperature=0.2):
        org_hist.append(len(messages))
        p = messages[-1]["content"]
        assign = []
        for ln in p.split("待分类的星（编号｜摘要｜标签）：", 1)[1].strip().splitlines():
            sid, summ = ln.split("｜")[0], ln.split("｜")[1]
            if "足球" in summ or "NBA" in summ:
                assign.append({"id": int(sid), "major": "体育", "minor": "球类"})
            elif "conda" in summ or "venv" in summ:
                assign.append({"id": int(sid), "major": "代码", "minor": "环境配置"})
            elif "pre-commit" in ln or "｜git" in ln:
                assign.append({"id": int(sid), "major": "编程", "minor": "git"})
            else:
                assign.append({"id": int(sid), "major": "生活", "minor": ""})
        return {"choices": [{"message": {"content": json.dumps({"assign": assign, "merge": [{"from_major": "编程", "from_minor": "", "to_major": "代码", "to_minor": ""}]}, ensure_ascii=False)}}]}
    context.main_complete = fake_org
    r = organize.organize(fuse=False)
    cats = {(x["major"], x["minor"]) for x in organize.categories()}
    check("主模型给星分大类 / 小类", r["by_llm"] and ("体育", "球类") in cats and ("代码", "环境配置") in cats)
    check("主模型合并重复类别（编程 → 代码）", ("代码", "git") in cats and not any(m == "编程" for m, _ in cats), str(cats) + str(r))
    check("主模型整理的上下文持久化（organizer 历史）", (TMP / "soul" / "agent" / "organizer.json").exists())
    config.update_config({"llm": {"compact_tokens": 2500, "context_tokens": 10000}})
    stars.add_star("主人喜欢羽毛球 " + "很" * 10, "", ["羽毛球"], 3, source="test")
    hist_o = organize.load_history()
    organize.save_history(hist_o + [{"role": "user", "content": "旧分类 " + "类" * 2500}, {"role": "assistant", "content": "{}"}])
    r = organize.organize(fuse=False)
    check("主模型整理累计到阈值 → 自动压缩", r.get("compacted") and organize.load_history()[0]["role"] == "system")
    config.update_config({"llm": {"compact_tokens": 380000, "context_tokens": 400000}})
    check("没有主模型时按标签粗分", organize.heuristic(["任务", "足球", "训练"]) == ("足球", "训练") and organize.heuristic(["未闭环"]) == ("任务", ""))
    m = c.get("/api/stars/map").json()
    check("星图接口：每颗星仍是 8 个字段 + 银河系布局", all(len(x) == 8 for x in m["stars"]) and m["galaxy"]["systems"])
    pos = m["galaxy"]["pos"]
    byc = {}
    for s in stars.list_stars(200):
        byc.setdefault((s.get("cat_major") or "", s.get("cat_minor") or ""), []).append(str(s["id"]))
    a1, a2 = byc[("代码", "环境配置")][:2]
    b1 = byc[("体育", "球类")][0]
    check("同一行星系的星挨在一起，不同恒星系离得远", galaxy.dist(pos[a1], pos[a2]) * 3 < galaxy.dist(pos[a1], pos[b1]))
    sysd = {s["name"]: s for s in m["galaxy"]["systems"]}
    check("恒星系绕星系核（不与核心重叠），行星系在恒星系附近",
          all(galaxy.dist(s["center"], [0, 0, 0]) > 10 for s in m["galaxy"]["systems"])
          and all(galaxy.dist(mm["center"], sysd["代码"]["center"]) < galaxy.dist(sysd["代码"]["center"], sysd["体育"]["center"]) for mm in sysd["代码"]["minors"]))
    lay1 = galaxy.layout([(1, "A", "x", 1), (2, "A", "y", 2), (3, "B", "", 3)])
    check("布局可复现", lay1 == galaxy.layout([(1, "A", "x", 1), (2, "A", "y", 2), (3, "B", "", 3)]))
    r = c.post(f"/api/stars/{b1}/category", json={"major": "运动", "minor": ""}).json()
    check("手动改类别", r["cat_major"] == "运动")
    check("手动整理排进任务抽屉", c.post("/api/organize").json()["ok"])
    time.sleep(1.2)
    check("整理任务可见", any(j["kind"] == "organize" for j in jobs.list_jobs(100)))

    # ---------- 8. 数据：增量 / 定时 / 云服务器 ----------
    print("\n[数据]")
    d0 = datasync.export_delta(0)
    check("增量导出：每颗星有 uid、带核心记忆", d0["stars"] and all(x["uid"] for x in d0["stars"]) and d0["core"]["sections"])
    wm = d0["until"]
    check("水位线之后没有变化 → 空", datasync.export_delta(wm)["stars"] == [])
    time.sleep(0.01)
    ns = stars.add_star("增量测试：新加的一颗", "", ["测试"], 3)
    ed = d0["stars"][0]
    sid_e = next(s["id"] for s in stars.list_stars(500) if s["content"] == ed["content"])
    time.sleep(0.01)
    stars.update_star(sid_e, content=ed["content"] + "（改过）")
    d1 = datasync.export_delta(wm)
    check("增量只含新增 / 修改的星", {x["content"] for x in d1["stars"]} == {"增量测试：新加的一颗", ed["content"] + "（改过）"})
    time.sleep(0.01)
    stars.soft_delete(ns["id"])
    d2 = datasync.export_delta(d1["until"])
    check("隐藏也算变化", len(d2["stars"]) == 1 and d2["stars"][0]["deleted_at"])

    store_dav = {}

    class FakeDav:
        def ensure_dir(self): pass
        def put(self, name, data): store_dav[name] = data
        def get(self, name): return store_dav[name]
        def list(self): return [{"name": k} for k in sorted(store_dav)]
    datasync._delta_dav = lambda cfg: FakeDav()
    config.update_config({"webdav": {"url": "http://dav.example/", "passphrase": "口令123"}})
    config.update_state(webdav_delta_since=0, webdav_core_rev=None)
    r1 = datasync.webdav_incremental()
    r2 = datasync.webdav_incremental()
    check("WebDAV 增量：第一次全部上传、加密", r1["uploaded"] and r1["encrypted"] and r1["stars"] >= 5)
    check("WebDAV 增量：没有变化就不上传", not r2["uploaded"] and len(store_dav) == 1)
    time.sleep(0.01)
    stars.add_star("又一颗新星", "", [], 3)
    r3 = datasync.webdav_incremental()
    check("WebDAV 增量：只传新变化（1 颗）", r3["uploaded"] and r3["stars"] == 1)
    dec = datasync._decode(store_dav[r3["name"]], "口令123")
    check("增量文件可用口令解开", dec["stars"][0]["content"] == "又一颗新星")

    CST = timezone(timedelta(hours=8))
    t_0259 = datetime(2026, 11, 9, 2, 59, tzinfo=CST).timestamp()
    t_0301 = datetime(2026, 11, 9, 3, 1, tzinfo=CST).timestamp()
    ran_0310_yday = datetime(2026, 11, 8, 3, 10, tzinfo=CST).timestamp()
    check("03:00 前不到期（昨天 03:10 跑过）", not datasync.daily_due(ran_0310_yday, t_0259))
    check("过了 03:00 到期", datasync.daily_due(ran_0310_yday, t_0301))
    check("关着软件错过了几天 → 下次启动到期（补跑）", datasync.daily_due(ran_0310_yday, datetime(2026, 11, 12, 15, 0, tzinfo=CST).timestamp()))
    check("从没跑过 → 到期", datasync.daily_due(None, t_0259))
    runs = []
    datasync.webdav_incremental = lambda: runs.append(1) or {"ok": True, "uploaded": True}
    config.update_state(daily_webdav_t=ran_0310_yday)
    out = datasync.run_daily(now=datetime(2026, 11, 10, 9, 0, tzinfo=CST).timestamp(), catch_up=True)
    out2 = datasync.run_daily(now=datetime(2026, 11, 10, 9, 5, tzinfo=CST).timestamp())
    check("补跑一次，同一天不再重复", len(runs) == 1 and out and out[0]["ok"] and not out2)
    check("补跑记进任务抽屉", any("补上错过的 03:00" in j["title"] for j in jobs.list_jobs(100)))
    datasync.webdav_incremental = lambda: (_ for _ in ()).throw(RuntimeError("网络断了"))
    out3 = datasync.run_daily(now=datetime(2026, 11, 11, 3, 0, 30, tzinfo=CST).timestamp())
    check("失败不推进，下次再试", out3 and not out3[0]["ok"] and datasync.daily_due(config.load_state()["daily_webdav_t"], datetime(2026, 11, 11, 3, 5, tzinfo=CST).timestamp()))

    # 云服务器（模拟远端星愿）
    from fastapi import FastAPI, Header, HTTPException
    remote = FastAPI()
    REM = {"pushed": []}

    @remote.get("/api/health")
    def rh():
        return {"app": "starwish", "version": "0.11.0"}

    @remote.post("/api/sync/delta")
    def rpush(body: dict, authorization: str = Header("")):
        if authorization != "Bearer tok-1":
            raise HTTPException(403, "令牌不对")
        REM["pushed"].append(body)
        return {"added": len(body["stars"]), "updated": 0, "skipped": 0}

    @remote.get("/api/sync/delta")
    def rpull(since: float = 0, authorization: str = Header("")):
        if authorization != "Bearer tok-1":
            raise HTTPException(403, "令牌不对")
        return {"format": "starwish-delta-1", "since": since, "until": 2e9, "core": {},
                "stars": [{"uid": "remote-uid-1", "content": "来自云服务器的记忆", "summary": "", "tags": ["云"], "created_at": 1.7e9,
                           "updated_at": 1.7e9, "importance": 3, "cat_major": "生活", "cat_minor": ""}]}
    rport = free_port()
    rs = uvicorn.Server(uvicorn.Config(remote, host="127.0.0.1", port=rport, log_level="warning"))
    threading.Thread(target=rs.run, daemon=True).start()
    for _ in range(100):
        if rs.started:
            break
        time.sleep(0.05)
    config.update_config({"data": {"cloud_url": f"http://127.0.0.1:{rport}", "cloud_token": "tok-1"}})
    check("云服务器：检测地址与令牌", c.post("/api/data/cloud/check", json={}).json()["ok"])
    r = c.post("/api/data/backup", json={"target": "cloud"}).json()
    check("云服务器：推送本机变化", r["push"]["pushed"] >= 6 and REM["pushed"][0]["format"] == "starwish-delta-1")
    check("云服务器：拉取远端变化并合并", r["pull"]["added"] == 1 and any(s["content"] == "来自云服务器的记忆" for s in stars.list_stars(500)))
    r = datasync.cloud_push()
    check("云服务器：再推只推新的", r["pushed"] <= 1)
    check("合并同一份不重复", datasync.import_delta(REM["pushed"][0])["added"] == 0)
    config.update_config({"data": {"cloud_token": "wrong"}})
    check("令牌不对给出原因", c.post("/api/data/backup", json={"target": "cloud"}).status_code == 502)
    rs.should_exit = True
    # 本机作为「云服务器」的接口
    dl = c.get("/api/sync/delta", params={"since": 0}).json()
    check("本机 /api/sync/delta 导出", dl["format"] == "starwish-delta-1" and dl["stars"])
    remote_delta = {"format": "starwish-delta-1", "since": 0, "until": 1, "stars": [], "core": {"rev": 999, "sections": {"identity": "我叫小愿"}}}
    c.post("/api/sync/delta", json=remote_delta)
    check("同步来的核心记忆只变成提案", any(p["content"] == "我叫小愿" for p in c.get("/api/proposals").json()) and "小愿" not in c.get("/api/core").json()["text"])

    # 目录 / 日志 / 缓存 / 本地备份 / 重置
    info = c.get("/api/data/info").json()
    check("数据目录信息", info["soul_dir"] == str(TMP / "soul") and info["log_days"] == 30)
    logs = TMP / "soul" / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "old.log").write_text("x")
    os.utime(logs / "old.log", (time.time() - 40 * 86400,) * 2)
    (logs / "new.log").write_text("y")
    check("日志按保留天数清理", datasync.clean_logs() == 1 and (logs / "new.log").exists())
    (TMP / "soul" / "cache").mkdir(exist_ok=True)
    (TMP / "soul" / "cache" / "a.bin").write_bytes(b"0" * 2048)
    check("清除缓存（显示大小）", c.get("/api/data/info").json()["cache_bytes"] >= 2048 and c.post("/api/data/clear_cache").json()["freed"] >= 2048)
    config.update_config({"data": {"local_dir": str(TMP / "bk")}})
    r = c.post("/api/data/backup", json={"target": "local"}).json()
    check("本地备份写到指定文件夹（有口令就加密）", (TMP / "bk" / r["name"]).exists() and r["name"].endswith(".enc"))
    check("本地备份列表", c.get("/api/data/backups", params={"target": "local"}).json()["items"][0]["name"] == r["name"])
    check("重置需要输入「重置」", c.post("/api/data/reset", json={"confirm": "yes"}).status_code == 428)
    n_before = stars.stats()["total"]
    r = c.post("/api/data/reset", json={"confirm": "重置"}).json()
    check("重置：先自动快照、再清空", r["ok"] and r["safety_snapshot"] and stars.stats()["total"] == 0 and n_before > 0)
    config.update_config({"data": {"local_dir": ""}})
    rr = c.post("/api/data/restore", json={"target": "local", "name": r["safety_snapshot"], "confirm": True}).json()
    check("从重置前的快照恢复", rr.get("ok") and stars.stats()["total"] == n_before)

    # ---------- 9. 外观 / 前端 ----------
    print("\n[界面]")
    r = c.put("/api/settings", json={"ui": {"theme": "light", "accent": "#1677FF", "scale": 110, "chat_side": "left", "task_side": "right"}}).json()
    check("外观设置保存", r["ui"]["theme"] == "light" and r["ui"]["scale"] == 110 and r["ui"]["accent"] == "#1677FF")
    web = ROOT / "web"
    idx = (web / "index.html").read_text("utf-8")
    sj = (web / "settings.js").read_text("utf-8")
    dj = (web / "drawers.js").read_text("utf-8")
    th = (web / "theme.js").read_text("utf-8")
    check("所有页面加载 theme.js", all("theme.js" in (web / f).read_text("utf-8") for f in ("index.html", "core.html", "skills.html", "eye.html")))
    check("主题：浅色 / 深色 / 系统 + 主题颜色 + 缩放", all(k in th for k in ("prefers-color-scheme", "--brand", "zoom")))
    check("主页：对话抽屉 + 任务抽屉", "drawers.js" in idx and "/api/agent/chat" in dj and "/api/tasks" in dj)
    check("任务颜色：完成绿 / 进行中橙 / 失败红", all(k in (web / "style.css").read_text("utf-8") for k in (".st-done", ".st-running", ".st-failed")))
    for k in ("对话监控", "外观", "数据", "立即归纳", "主题颜色", "数据目录", "云服务器", "WebDAV", "即将支持", "重置数据", "返回",
              "基础", "工具", "偏好", "上下文窗口", "自动压缩"):
        check(f"设置页包含「{k}」", k in sj)

    ok = sum(1 for _, g, _ in RESULTS if g)
    for nme, g, dt in RESULTS:
        if not g:
            print("  ✘", nme, dt)
    print(f"\n对话监控 / 抽屉 / 星系 / 数据结果：{ok}/{len(RESULTS)} 通过")
    shutil.rmtree(TMP, ignore_errors=True)
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
