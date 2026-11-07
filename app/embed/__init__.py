"""向量模型（语义检索）：三档可选。

- small：内置 bge-small-zh-v1.5 int8 ONNX（23.9MB，512 维），常驻默认，完整包自带，缺了会自动下载；
- m3：BGE-M3 int8 ONNX（约 590MB，1024 维，中英多语、长文本更强），默认不装，设置里一键部署；
- api：Qwen3-Embedding（阿里云百炼 text-embedding-v4，或任何 OpenAI 兼容 /embeddings 接口），单独填 Key。

向量只用来「补」字面检索：jieba + FTS5 仍然是第一路，向量负责把「保暖外套」和「羽绒服」连起来。
"""
