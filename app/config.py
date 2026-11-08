"""星愿 StarWish —— 路径与配置。

所有路径都相对于程序所在文件夹，整个文件夹可以放 U 盘 / 网盘同步目录。
"""
from __future__ import annotations

import base64
import copy
import json
import os
import sys
import threading
from pathlib import Path

if getattr(sys, "frozen", False):  # PyInstaller 打包后
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent.parent

SOUL_DIR = Path(os.environ.get("STARWISH_SOUL_DIR") or (APP_DIR / "soul")).resolve()
WEB_DIR = APP_DIR / "web"
if not WEB_DIR.exists() and getattr(sys, "_MEIPASS", None):  # PyInstaller 把 web/ 打进包内
    WEB_DIR = Path(sys._MEIPASS) / "web"


def soul_path(*parts: str) -> Path:
    return SOUL_DIR.joinpath(*parts)


DB_PATH = soul_path("soul.db")
CORE_JSON = soul_path("core.json")
CORE_MD = soul_path("core.md")
EVENTS_PATH = soul_path("events.jsonl")
ARCHIVE_DIR = soul_path("archive")
SNAPSHOT_DIR = soul_path("snapshots")
CONFIG_PATH = soul_path("config.json")
STATE_PATH = soul_path("state.json")

DEFAULT_CONFIG = {
    "llm": {
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o-mini",
        "maintenance_model": "",
        "maint_base_url": "",       # 整理模型在另一个服务商时用（由「默认模型」自动填写）
        "maint_api_key": "",
        "protocol": "openai",       # openai / anthropic / responses（由「默认模型」所在服务商自动填写）
        "maint_protocol": "",       # 整理模型在另一个服务商时的协议
        "timeout": 120,
        "context_tokens": 400000,   # 主模型（对话模型）的上下文窗口：整理星系、对话抽屉按它分配预算
        "maint_context_tokens": 400000,  # 整理模型的上下文窗口（对话监控不带上下文，只看一轮）
        "compact_tokens": 380000,   # 有上下文的地方（对话抽屉、主模型整理）累计到这么多就自动压缩旧历史
    },
    "proxy": {
        "inject": True,
        "budget_tokens": 600,
        "log_to_archive": True,
        "proxy_key": "",
        "model_override": "",
    },
    "memory": {
        "half_life_days": 30,
        "brightness_floor": 0.08,
        "brightness_weight": 0.5,
        "recall_budget_default": 600,
        "semantic": True,           # 语义检索总开关（向量模型在 embed 段里选）
        # —— 星辰融合：相似的记忆融合、归纳、进化成一颗新星（成员不删除，可拆开还原）——
        "fuse_auto": True,          # 自动融合
        "fuse_every_new": 8,        # 每新增多少颗星检查一次
        "fuse_threshold": 0.34,     # 相似度达到多少算「类似的记忆」
        "fuse_max_group": 6,        # 一次最多几颗融成一颗
        "fuse_max_per_run": 5,      # 每次最多融合几组
        "fuse_scan": 400,           # 每次只看最近多少颗星（老星早已融合过）
        "fuse_use_llm": True,       # 配好 LLM 时让它归纳；否则朴素拼接
    },
    "embed": {                      # 向量模型：和 jieba + FTS5 混合检索，「保暖外套」也能找到「羽绒服」
        "provider": "small",        # small = 内置 bge-small-zh int8（常驻默认）；m3 = BGE-M3 int8（一键部署）；api = Qwen3-Embedding 等接口
        "auto_download": True,      # 内置模型不在时自动下载（完整包自带，一般用不上）
        "hf_base": "https://huggingface.co",
        "hf_mirror": "https://hf-mirror.com",  # 国内镜像：主站下不动时自动换它
        "prefer_mirror": False,     # 先走镜像
        "pip_index": "",
        "threads": 2,
        # —— Qwen3-Embedding（阿里云百炼 text-embedding-v4，OpenAI 兼容；也可填硅基流动等）——
        "api_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": "",
        "api_model": "text-embedding-v4",
        "api_dimensions": 1024,     # 0 = 用服务默认
        "api_batch": 10,            # text-embedding-v4 一次最多 10 条
        "api_timeout": 30,
        # —— 混合检索 ——
        "min_sim": 0,               # 相似度门槛；0 = 按模型默认（small 0.44 / M3 0.45 / 接口 0.35）
        "weight": 0.9,              # 语义命中的最高相关度（字面完全命中是 1.0）
    },
    "webdav": {
        "url": "",
        "user": "",
        "password": "",
        "remote_dir": "starwish",
        "passphrase": "",
        "keep_versions": 10,
    },
    "schedules": {
        "consolidate_hours": 0,
        "backup_hours": 0,
        "snapshot_hours": 24,
        "keep_local_snapshots": 10,
    },
    "core": {
        "owner_token": "",          # 主人令牌：远程 API 带上它才能直接改核心记忆（为空时只信任本机 127.0.0.1）
        "chat_commands": True,      # 代理里识别 /核心 指令
        "natural_commands": True,   # 代理里识别「记住，以后叫我X」等主人原话
        "mcp_owner_edit": True,     # MCP 的 update_core 带主人原话时直接生效（否则转提案）
    },
    "evolve": {
        "enabled": True,            # 自进化总开关
        "nudge_interval": 10,       # 后台巡检阈值：距上次保存技能累计多少轮对话（跨会话累积；0 = 不自动巡检）
        "approval": "auto",         # auto：安全的直接生效、危险的交主人批准；always：AI 的所有技能修改都要主人批准
        "max_ops": 5,               # 每次巡检最多改几个技能
        "review_memory": True,      # 联合巡检：顺便提取星星、提出核心提案
        "buffer_turns": 40,         # 巡检回看的最近对话轮数上限
        "inject": True,             # 代理里注入相关技能
        "inject_k": 3,
        "inject_budget_tokens": 400,
        "inject_full_threshold": 0.3,
    },
    "eye": {                        # 宇宙之眼：看屏幕 / 摄像头，陪主人实时互动
        "enabled": True,
        "engine": "frame",          # frame = 通用帧模式（任意 OpenAI / Anthropic 兼容识图模型）；qwen_omni = 千问全模态视频接口
        # —— 通用帧模式（留空则沿用「LLM 维护」里的地址 / Key / 模型）——
        "frame_protocol": "openai", # openai / anthropic
        "frame_base_url": "",
        "frame_api_key": "",
        "frame_model": "",
        "frame_compose": "grid",    # grid = 多帧拼成一张分镜图（省钱、单图模型也能用）；multi = 每帧一张图
        # —— 千问 Qwen3.8-Omni-Flash 视频接口（预留：填 Key 即用）——
        "qwen_protocol": "openai",  # openai / anthropic
        "qwen_base_url_openai": "https://maas.qianwenaiapi.com/compatible-mode/v1",
        "qwen_base_url_anthropic": "https://maas.qianwenaiapi.com/apps/anthropic",
        "qwen_api_key": "",
        "qwen_model": "qwen3.8-omni-flash",
        "qwen_video_mode": "video", # video = 帧序列按「视频」传（失败自动退回多图）；images = 每帧一张图
        "qwen_audio_out": False,    # 让模型直接出语音（预留，未实测）
        "qwen_voice": "Cherry",
        # —— 看与说 ——
        "max_frames": 6,            # 每次最多带几帧
        "frame_width": 768,         # 单帧宽度（像素）
        "pace": "normal",           # 主动吐槽节奏：off / calm / normal / chatty
        "reply_chars": 60,          # 回答尽量控制在多少字以内（陪看要短）
        "history_turns": 12,        # 带最近几轮文字对话（图片永不进历史）
        "recall_budget_tokens": 300,
        "daily_budget_tokens": 0,   # 每日 token 预算（0 = 不限）
        "daily_max_requests": 0,    # 每日请求次数上限（0 = 不限）
        "star_on_end": True,        # 结束时把画面日记沉淀成一颗星
        "save_frames": False,       # 画面默认不落盘，只存文字日记
        # —— 声音 ——
        "stt_mode": "browser",      # browser = 浏览器自带识别；api = OpenAI 兼容转写服务（可转写电影声音）
        "stt_base_url": "",
        "stt_api_key": "",
        "stt_model": "whisper-1",
        "tts": True,                # 用浏览器朗读 AI 的话
    },
    "voice": {                      # 宇宙之声：宇宙之眼的声纹插件（分辨男1 / 女1…，看片时拿到名字）
        "enabled": False,           # 观看页「🌌 宇宙之声」开关；默认关，不下载就不占空间
        "backend": "sherpa",        # sherpa = sherpa-onnx + CAM++ + pyannote 分割（纯 CPU）
        "chunk_sec": 8,             # 浏览器每隔几秒送一段声音
        "match_threshold": 0.5,     # 和已知说话人声纹相似度达到多少算同一个人
        "merge_threshold": 0.65,    # 重新聚类时，两个人相似度达到多少就合并
        "local_cluster_threshold": 0.5,  # 一段音频内部的聚类阈值（越小分得越细）
        "min_learn_sec": 1.5,       # 只用这么长以上的句子更新声纹（短句、抢话不学）
        "gender_split_hz": 165,     # 音高低于此值记为「男」，高于记为「女」
        "recluster_every": 4,       # 每处理几段做一次全场重新聚类
        "recluster_on_end": True,
        "transcribe": True,         # 有转写服务时逐句转写并标上说话人
        "auto_identify_every": 20,  # 每新增多少句带说话人的台词，让 LLM 读台词认一次名字（0 = 只手动）
        "auto_accept_votes": 2,     # 同一个名字被两路线索（或两次）提到就自动确认（0 = 全部等主人确认）
        "allow_override": False,    # 已确认的名字是否允许被自动线索改掉
        "visual_naming": True,      # 提问时让视觉模型顺便认人（回答末尾的【认人】标记，不朗读）
        "use_library": True,        # 按片名 / 系列记住认识的人，下次直接认出
        "owner_detect": True,       # 录过主人声音后，能分清哪句是主人说的
        "owner_threshold": 0.55,
        "download_mirror": "",      # 模型下载加速前缀（国内可填 https://ghfast.top/ 之类）
        "pip_index": "",            # pip 镜像（如 https://pypi.tuna.tsinghua.edu.cn/simple）
        "model_base_url": "",       # 自建模型镜像（留空 = sherpa-onnx 官方发布页）
    },
    "watch": {                      # 对话监控（v0.11.0）：直接读各客户端写在本机的对话记录，不经过代理
        "enabled": True,            # 总开关
        "poll_sec": 3,              # 每隔几秒看一眼文件大小 / 修改时间（只 stat，不读内容）
        "rescan_sec": 60,           # 每隔多久重新列一次目录找新会话
        "idle_sec": 45,             # 文件静止多久算这一轮说完了（装了钩子会更快）
        "hot_days": 3,              # 只盯最近几天动过的会话
        "min_turn_chars": 12,       # 太短的寒暄直接跳过，不花模型调用
        "max_turn_chars": 4000,     # 一轮最多给模型看多少字（长的保留头尾）
        "core_importance": 4,       # 偏好重要度达到多少转成「核心提案」（自动通道绝不直接改核心）
        "skill_on_close": True,     # 任务闭环时，可复用的流程存成技能
        "backfill_existing": False, # 第一次启用时是否回读最近几天的旧对话（默认从现在开始）
        "organize_after": True,     # 归纳出新星后，排队让主模型整理星系
        "clients": {                # 每个客户端：enabled 监控 / hook 是否装了钩子 / path 路径覆盖（多个用 ; 分隔）
            "claude": {"enabled": True, "hook": False, "path": ""},
            "qoder": {"enabled": True, "hook": False, "path": ""},
            "workbuddy": {"enabled": True, "hook": False, "path": ""},
            "codebuddy": {"enabled": False, "hook": False, "path": ""},
            "codex": {"enabled": True, "hook": False, "path": ""},
            "opencode": {"enabled": True, "hook": False, "path": ""},
            "dsh": {"enabled": False, "hook": False, "path": ""},
        },
    },
    "data": {                       # 数据（v0.11.0）
        "log_days": 30,             # 日志保留天数（0 = 永久）；启动时清理一次，之后每 12 小时
        "local_dir": "",            # 本地备份文件夹（空 = soul/snapshots）
        "webdav_daily": True,       # 每天 03:00（北京时间）向 WebDAV 增量上传；错过了下次启动补上
        "webdav_hour": 3,
        "cloud_url": "",            # 云服务器：你自己服务器上的星愿地址（如 https://my.server:7860）
        "cloud_token": "",          # 云服务器的主人令牌
        "cloud_daily": False,       # 云服务器也每天 03:00 推送 + 拉取
    },
    "ui": {                         # 外观（v0.11.0）
        "theme": "dark",            # light / dark / system
        "accent": "#00B96B",        # 主题颜色
        "scale": 100,               # 缩放百分比
        "chat_side": "right",       # 对话抽屉默认位置
        "task_side": "left",        # 任务列表位置
        "chat_open": True,
        "task_open": True,
    },
    # —— 模型服务（v0.8.0）：服务商列表 + 默认模型；保存后自动映射到 llm / eye / embed ——
    "providers": [],
    "defaults": {"chat": "", "maintain": "", "vision": "", "embed": ""},
    "server": {"host": "127.0.0.1", "port": 7860, "open_browser": True},
    "secrets_encrypted": False,
    "secrets_salt": "",
}

SECRET_FIELDS = [
    ("llm", "api_key"),
    ("webdav", "password"),
    ("webdav", "passphrase"),
    ("proxy", "proxy_key"),
    ("core", "owner_token"),
    ("eye", "frame_api_key"),
    ("eye", "qwen_api_key"),
    ("eye", "stt_api_key"),
    ("embed", "api_key"),
    ("llm", "maint_api_key"),
    ("data", "cloud_token"),
]


def _provider_keys(cfg: dict):
    for p in cfg.get("providers") or []:
        if isinstance(p, dict):
            yield p

_lock = threading.RLock()
_master_passphrase: str | None = os.environ.get("STARWISH_MASTER") or None


def ensure_dirs() -> None:
    for d in (SOUL_DIR, ARCHIVE_DIR, SNAPSHOT_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ---------- 机密字段加密（可选主密码） ----------

def _fernet_for(passphrase: str, salt_b64: str):
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=base64.b64decode(salt_b64), iterations=390_000)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8"))))


def set_master_passphrase(p: str | None) -> None:
    global _master_passphrase
    _master_passphrase = p or None


def secrets_unlocked() -> bool:
    cfg = load_raw_config()
    if not cfg.get("secrets_encrypted"):
        return True
    if not _master_passphrase:
        return False
    try:
        _decrypt_all(cfg, _master_passphrase)
        return True
    except Exception:
        return False


def _decrypt_all(cfg: dict, passphrase: str) -> dict:
    f = _fernet_for(passphrase, cfg["secrets_salt"])
    for sec, key in SECRET_FIELDS:
        v = cfg.get(sec, {}).get(key, "")
        if isinstance(v, str) and v.startswith("enc:"):
            cfg[sec][key] = f.decrypt(v[4:].encode()).decode()
    for p in _provider_keys(cfg):
        v = p.get("api_key", "")
        if isinstance(v, str) and v.startswith("enc:"):
            p["api_key"] = f.decrypt(v[4:].encode()).decode()
    return cfg


def _encrypt_all(cfg: dict, passphrase: str) -> dict:
    f = _fernet_for(passphrase, cfg["secrets_salt"])
    for sec, key in SECRET_FIELDS:
        v = cfg.get(sec, {}).get(key, "")
        if v and not str(v).startswith("enc:"):
            cfg[sec][key] = "enc:" + f.encrypt(str(v).encode()).decode()
    for p in _provider_keys(cfg):
        v = p.get("api_key", "")
        if v and not str(v).startswith("enc:"):
            p["api_key"] = "enc:" + f.encrypt(str(v).encode()).decode()
    return cfg


def load_raw_config() -> dict:
    with _lock:
        ensure_dirs()
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text("utf-8"))
            except Exception:
                data = {}
        else:
            data = {}
        return _deep_merge(DEFAULT_CONFIG, data)


def load_config() -> dict:
    """返回可直接使用的配置（机密字段已解密；若加密且未解锁则置空）。"""
    cfg = load_raw_config()
    if cfg.get("secrets_encrypted"):
        if _master_passphrase:
            try:
                return apply_providers(_decrypt_all(cfg, _master_passphrase))
            except Exception:
                pass
        for sec, key in SECRET_FIELDS:
            if str(cfg[sec].get(key, "")).startswith("enc:"):
                cfg[sec][key] = ""
        for p in _provider_keys(cfg):
            if str(p.get("api_key", "")).startswith("enc:"):
                p["api_key"] = ""
    return apply_providers(cfg)


def _migrate_legacy(cfg: dict) -> None:
    """旧版只填了 llm 的：在设置界面里显示成一个服务商（保存后才真正写入 providers）。"""
    if not cfg.get("providers") and cfg["llm"].get("api_key") and cfg["llm"].get("base_url"):
        cfg["providers"] = [{"id": "legacy", "name": "上游大模型（旧设置）", "preset": "custom", "protocol": "openai",
                             "base_url": cfg["llm"]["base_url"], "api_key": cfg["llm"]["api_key"], "enabled": True,
                             "models": [{"id": cfg["llm"]["model"], "kind": "chat"}] if cfg["llm"].get("model") else []}]
        if cfg["llm"].get("model") and not cfg["defaults"].get("chat"):
            cfg["defaults"]["chat"] = "legacy::" + cfg["llm"]["model"]


def apply_providers(cfg: dict) -> dict:
    """把「模型服务 → 默认模型」映射回各模块原来读取的字段；旧版只填了 llm 的，自动变成一个服务商。"""
    from . import providers as pv

    chat = pv.resolve(cfg, "chat")
    if chat:
        cfg["llm"].update(base_url=chat["base_url"], api_key=chat["api_key"], model=chat["model"],
                          protocol=chat["protocol"])
    mt = pv.resolve(cfg, "maintain")
    if mt:
        same = chat and mt["base_url"] == chat["base_url"] and mt["api_key"] == chat["api_key"] \
            and mt["protocol"] == chat["protocol"]
        cfg["llm"].update(maintenance_model=mt["model"], maint_base_url="" if same else mt["base_url"],
                          maint_api_key="" if same else mt["api_key"], maint_protocol="" if same else mt["protocol"])
    elif cfg["defaults"].get("maintain") == "" and chat:
        cfg["llm"].update(maintenance_model="", maint_base_url="", maint_api_key="", maint_protocol="")
    vi = pv.resolve(cfg, "vision")
    if vi:
        cfg["eye"].update(frame_base_url=vi["base_url"], frame_api_key=vi["api_key"], frame_model=vi["model"],
                          frame_protocol=vi["protocol"])
    em = pv.resolve(cfg, "embed")
    if em:
        cfg["embed"].update(api_base_url=em["base_url"], api_key=em["api_key"], api_model=em["model"])
    return cfg


def save_config(new_cfg: dict) -> dict:
    """保存配置。new_cfg 为明文；若已开启加密，写盘前自动加密机密字段。"""
    with _lock:
        cfg = _deep_merge(DEFAULT_CONFIG, new_cfg)
        if cfg.get("secrets_encrypted"):
            if not _master_passphrase:
                raise PermissionError("机密已加密，请先用主密码解锁再保存设置")
            cfg = _encrypt_all(cfg, _master_passphrase)
        _atomic_write_json(CONFIG_PATH, cfg)
        return load_config()


def update_config(patch: dict) -> dict:
    cur = load_config()
    raw = load_raw_config()
    # 未解锁时保留原密文，避免把空值写回
    patch = dict(patch or {})
    if isinstance(patch.get("providers"), list):   # 列表整体替换：Key 留空 / 掩码时沿用原值
        old = {p.get("id"): p for p in cur.get("providers") or []}
        for p in patch["providers"]:
            p.pop("api_key_set", None)
            k = p.get("api_key")
            if k is None or (isinstance(k, str) and k.startswith("****")):
                p["api_key"] = (old.get(p.get("id")) or {}).get("api_key", "") or (
                    cur["llm"].get("api_key", "") if p.get("id") == "legacy" else "")
    merged = _deep_merge(cur, patch)
    if raw.get("secrets_encrypted") and not _master_passphrase:
        raise PermissionError("机密已加密，请先用主密码解锁再保存设置")
    return save_config(merged)


def enable_secret_encryption(passphrase: str) -> None:
    with _lock:
        cfg = load_config()
        if not passphrase:
            raise ValueError("主密码不能为空")
        cfg["secrets_encrypted"] = True
        cfg["secrets_salt"] = base64.b64encode(os.urandom(16)).decode()
        set_master_passphrase(passphrase)
        save_config(cfg)


def disable_secret_encryption() -> None:
    with _lock:
        if not secrets_unlocked():
            raise PermissionError("请先解锁")
        cfg = load_config()
        cfg["secrets_encrypted"] = False
        _atomic_write_json(CONFIG_PATH, cfg)


def masked_config() -> dict:
    cfg = load_config()
    raw = load_raw_config()
    for sec, key in SECRET_FIELDS:
        v = cfg[sec].get(key, "")
        rv = raw[sec].get(key, "")
        cfg[sec][key + "_set"] = bool(v or rv)
        cfg[sec][key] = ("****" + v[-4:]) if v and len(v) > 8 else ("****" if (v or rv) else "")
    _migrate_legacy(cfg)
    raw_keys = {p.get("id"): p.get("api_key", "") for p in _provider_keys(raw)}
    for p in _provider_keys(cfg):
        v = p.get("api_key", "")
        p["api_key_set"] = bool(v or raw_keys.get(p.get("id")))
        p["api_key"] = ("****" + v[-4:]) if v and len(v) > 8 else ("****" if p["api_key_set"] else "")
    cfg.pop("secrets_salt", None)
    cfg["secrets_unlocked"] = secrets_unlocked()
    return cfg


def _atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, path)


# ---------- 运行状态（整理进度、上次备份时间等） ----------

def load_state() -> dict:
    with _lock:
        if STATE_PATH.exists():
            try:
                return json.loads(STATE_PATH.read_text("utf-8"))
            except Exception:
                return {}
        return {}


def save_state(state: dict) -> None:
    with _lock:
        _atomic_write_json(STATE_PATH, state)


def update_state(**kw) -> dict:
    with _lock:
        st = load_state()
        st.update(kw)
        save_state(st)
        return st
