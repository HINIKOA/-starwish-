"""宇宙之眼：看电脑屏幕或摄像头画面，和主人实时互动（通用帧模式 + 千问 Omni 视频接口预留）。

采集与变化检测在浏览器里做（/eye 页面）；服务端负责：组装请求（核心记忆 + 相关星星 + 文字历史 + 这一次的几帧）、
流式转发、预算、主动吐槽限速、文字版画面日记、结束时沉淀成星星。参考了 mcai（CC0）与 dsh-see-now（Apache-2.0）的思路，代码为独立实现。
"""
from . import engine, session  # noqa: F401
from .engine import ENGINES, EyeConfigError, preflight, resolve  # noqa: F401
from .session import BudgetError, SessionNotFound  # noqa: F401
