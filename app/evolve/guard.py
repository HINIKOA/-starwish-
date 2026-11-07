"""技能安全扫描（参考 Hermes Agent tools/skills_guard.py 的分类与信任矩阵，独立精简实现）。

技能会被注入给模型当「操作手册」，所以 AI 自己写的技能必须过一道静态扫描：
  数据外泄 / 提示注入 / 破坏性操作 / 持久化 / 反弹 shell / 混淆 / 代码执行 / 路径穿越 / 供应链 / 凭证暴露 / 不可见字符
结论三级：safe / caution / dangerous；最终是否放行看来源的信任等级（INSTALL_POLICY）。
"""
from __future__ import annotations

import re

# (类别, 严重级别, 正则, 说明)
PATTERNS: list[tuple[str, str, str, str]] = [
    # 数据外泄
    ("exfiltration", "critical", r"(curl|wget|httpx?|requests)\b[^\n]{0,120}\$\{?[A-Z_]*(KEY|TOKEN|SECRET|PASSWORD)", "把密钥拼进网络请求"),
    ("exfiltration", "high", r"(~|\$HOME|%USERPROFILE%)?[/\\]\.(ssh|aws|gnupg)\b", "读取 .ssh / .aws 等凭证目录"),
    ("exfiltration", "high", r"(cat|type|more|less)\s+[^\n]*\.env\b", "读取 .env"),
    ("exfiltration", "high", r"\b(printenv|env\s*\|)", "导出全部环境变量"),
    ("exfiltration", "high", r"!\[[^\]]*\]\(https?://[^)]*\?(?:[^)]*=)\s*\{?\{?", "Markdown 图片外泄"),
    ("exfiltration", "high", r"(发送|上传|post|send)[^\n]{0,30}(api[ _-]?key|密钥|token|密码)[^\n]{0,30}(到|to)\s*https?://", "要求把密钥发到外部地址"),
    # 提示注入
    ("injection", "critical", r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|rules)", "ignore previous instructions"),
    ("injection", "critical", r"(忽略|无视|不要理会)[^\n]{0,8}(之前|以上|前面|所有|先前|上述)[^\n]{0,6}(指令|规则|设定|系统提示|要求)", "忽略之前的指令"),
    ("injection", "high", r"(system\s*prompt\s*(override|:)|disregard\s+(the\s+)?rules|you\s+are\s+now\s+(DAN|jailbroken))", "系统提示覆盖 / 越狱"),
    ("injection", "high", r"(修改|覆盖|删除|清空)[^\n]{0,12}(核心记忆|core\.json)", "试图绕过核心记忆保护"),
    ("injection", "high", r"<!--[^>]{0,400}(instruction|指令|ignore|忽略)[^>]{0,400}-->", "HTML 注释里藏指令"),
    ("injection", "high", r"<div[^>]*(display\s*:\s*none|hidden)[^>]*>", "隐藏 div"),
    # 破坏性操作
    ("destructive", "critical", r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|~|\*|\$HOME)(\s|$)", "rm -rf /"),
    ("destructive", "critical", r"\b(mkfs(\.\w+)?|format\s+[a-z]:|dd\s+if=.*of=/dev/)", "格式化磁盘"),
    ("destructive", "high", r"\b(DROP\s+(TABLE|DATABASE)|TRUNCATE\s+TABLE)\b", "删库删表"),
    ("destructive", "high", r"\bchmod\s+(-R\s+)?777\b", "chmod 777"),
    ("destructive", "high", r"\b(del|rd|rmdir)\s+/[sq]\b", "Windows 递归删除"),
    # 持久化
    ("persistence", "high", r"(>>?|tee\s+-a)\s*~?/?\.(bashrc|zshrc|profile|bash_profile)", "写入 shell 启动文件"),
    ("persistence", "medium", r"\bcrontab\s+(-e|-l\s*\|)|/etc/cron", "crontab 注入"),
    ("persistence", "medium", r"systemctl\s+(enable|daemon-reload)|/etc/systemd/system/", "创建系统服务"),
    ("persistence", "medium", r"(schtasks\s+/create|HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run)", "Windows 开机自启"),
    # 网络
    ("network", "critical", r"\b(nc|ncat|netcat)\b[^\n]{0,40}\s-e\s", "nc -e 反弹 shell"),
    ("network", "critical", r"bash\s+-i\s*>&\s*/dev/tcp/", "bash 反弹 shell"),
    ("network", "high", r"\bssh\s+[^\n]*-[RLD]\s*\d+", "SSH 隧道 / 端口转发"),
    # 混淆
    ("obfuscation", "high", r"(base64\s+(-d|--decode)|b64decode\()[^\n]{0,80}(\|\s*(sh|bash|python)|exec|eval)", "base64 解码后执行"),
    ("obfuscation", "medium", r"(\\x[0-9a-fA-F]{2}){8,}", "大段十六进制转义"),
    # 代码执行
    ("execution", "high", r"\b(eval|exec)\s*\(\s*(input|request|sys\.argv|os\.environ)", "执行外部输入"),
    ("execution", "medium", r"\b(os\.system|subprocess\.(call|run|Popen)\([^\n]*shell\s*=\s*True)", "shell 执行"),
    # 路径穿越
    ("traversal", "high", r"(\.\./){3,}|\.\.\\\.\.\\\.\.\\", "多级 ../ 路径穿越"),
    # 供应链
    ("supply_chain", "high", r"(curl|wget)\s+[^\n|]*\|\s*(sudo\s+)?(sh|bash|python3?)\b", "curl | bash"),
    ("supply_chain", "medium", r"pip\s+install\s+[^\n]*(--index-url|-i)\s+https?://(?!pypi\.org|mirrors\.|pypi\.tuna)", "从非官方源 pip install"),
    # 凭证暴露
    ("credential_exposure", "critical", r"\b(sk-[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9-]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|xox[bp]-[A-Za-z0-9-]{20,})", "硬编码 API Key"),
    ("credential_exposure", "high", r"(password|passwd|密码)\s*[:=：]\s*['\"]?[^\s'\"]{6,}", "明文密码"),
]
_COMPILED = [(c, s, re.compile(p, re.I), d) for c, s, p, d in PATTERNS]
INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\u00ad]")

SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}

# 信任等级 × 扫描结论 → allow / ask（交给主人批准）/ block
INSTALL_POLICY = {
    #                  safe      caution    dangerous
    "owner":         ("allow", "allow", "allow"),   # 主人亲手写的：全部放行（只提示）
    "trusted":       ("allow", "allow", "block"),
    "imported":      ("allow", "ask", "block"),     # 外部导入的技能包
    "agent-created": ("allow", "allow", "ask"),     # AI 自己写的：危险 → 主人批准
}


def scan_text(text: str, path: str = "SKILL.md") -> list[dict]:
    findings = []
    for cat, sev, rx, desc in _COMPILED:
        m = rx.search(text or "")
        if m:
            line = (text[:m.start()].count("\n") + 1)
            findings.append({"category": cat, "severity": sev, "desc": desc, "file": path, "line": line,
                             "match": m.group(0)[:80]})
    inv = INVISIBLE.findall(text or "")
    if inv:
        findings.append({"category": "obfuscation", "severity": "high", "desc": f"含 {len(inv)} 个不可见 Unicode 字符",
                         "file": path, "line": 0, "match": ""})
    return findings


def verdict(findings: list[dict]) -> str:
    worst = max((SEVERITY_RANK[f["severity"]] for f in findings), default=-1)
    if worst >= 2:
        return "dangerous"
    if worst >= 0:
        return "caution"
    return "safe"


def scan_files(files: dict[str, str]) -> dict:
    findings = []
    for path, text in files.items():
        findings += scan_text(text, path)
    return {"verdict": verdict(findings), "findings": findings}


def decide(scan: dict, trust: str) -> str:
    pol = INSTALL_POLICY.get(trust, INSTALL_POLICY["agent-created"])
    return pol[("safe", "caution", "dangerous").index(scan["verdict"])]


def report(scan: dict) -> str:
    if not scan["findings"]:
        return "安全扫描：safe"
    lines = [f"安全扫描：{scan['verdict']}"]
    for f in scan["findings"]:
        lines.append(f"- [{f['severity']}] {f['category']}：{f['desc']}（{f['file']}:{f['line']}）")
    return "\n".join(lines)
