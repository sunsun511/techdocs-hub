# 来源与公开范围

TechDocs 为个人工程项目，使用 AI 编程工具辅助实现与验证。本公开版来自本地已验收的应用代码，不导入私有仓库历史。

## 外部模型（不随仓库分发）

- [multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small)：intfloat / Microsoft。量化 ONNX 转换来自 [Xenova/multilingual-e5-small](https://huggingface.co/Xenova/multilingual-e5-small)。
- [OPUS-MT zh-en](https://huggingface.co/Helsinki-NLP/opus-mt-zh-en)：Helsinki-NLP。量化 ONNX 转换来自 [Xenova/opus-mt-zh-en](https://huggingface.co/Xenova/opus-mt-zh-en)。

下载地址、固定 revision、大小和 SHA256 见 fixtures/model-manifest.json。下载和使用须遵守各模型页面对应版本的许可；本仓库不重新授权或分发权重。Python 和 JavaScript 依赖的版权与许可由各自项目保留。

## 文档与评测

公开版只附 examples/ 中的自写合成资料。没有包含 Redis、Docker 或 PostgreSQL 官方文档快照、清洗后正文、冻结题集、原文命中记录或真实数据库。历史 47/50 与 42/50 来自原本地语料，不适用于这份新示例。移除旧语料是公开发布范围选择，不修改作者本地冻结题集。

Redis 文档历史许可和非商业限制不能由公开本项目来绕过，因此原语料与逐题证据全部留在本地。页面和测试中出现产品名称或少量合成演示句不表示分发其官方文档。

截图来自随机账号和自写合成数据，不含登录密码或真实用户资料。公开摘要保留验证方法、指标和边界，不包含机器路径、会话、原始日志或个人联系信息。

目前未为项目自有代码另行授予通用开源许可证；公开可查看与已授权任意再分发是不同事项。不要把外部依赖的许可误作整个仓库的许可。
