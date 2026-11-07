"""宇宙之声：宇宙之眼的声纹插件——分辨片中每个说话人（男1 / 女1 …），并在看片过程中拿到他们的名字。

默认不装、不占空间。打开开关后，面板一键下载约 60MB：sherpa-onnx 运行库（Apache-2.0）+ 3D-Speaker CAM++ 中英声纹模型
+ pyannote-segmentation-3.0 int8 分割模型（MIT），放在程序目录 models/voice/（不进灵魂包、不进 WebDAV 备份）。

三路线索一起认人（思路参考 CineSRD 2026 / 3D-Speaker 多模态分离，代码为独立实现）：
  1. 声纹：每句话提取 192 维声纹，和本场已知的人比对，对不上就是新人；音高判断男 / 女。
  2. 台词：转写后按说话人记日记；「我叫 X」直接提名；LLM 读带说话人的台词，从称呼里推断名字。
  3. 画面：提问时把「这几秒是谁在说话」告诉视觉模型，它能看出是谁就在回答末尾留一个【认人】标记。
同一个名字被两路线索（或两次）提到就自动确认；主人可以随时改名、确认、否决、合并。
只存声纹向量，不存录音。
"""
from . import audio, registry, models, backend, naming, pipeline  # noqa: F401
from .registry import Registry, VoiceError  # noqa: F401
