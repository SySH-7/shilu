# 拾录 Shilu

离线检索 QQ、微信聊天记录和个人文档。资料保存在本机，无需账号或 AI API。

## 下载使用

从 [Releases](https://github.com/SySH-7/shilu/releases/latest) 下载对应压缩包，完整解压到可写目录：

| 系统 | 下载包 | 启动文件 |
| --- | --- | --- |
| Windows x64 | `Shilu-windows-x64.zip` | `Shilu.exe` |
| macOS Apple 芯片 | `Shilu-macos-arm64.zip` | `启动拾录.command` |
| macOS Intel | `Shilu-macos-x64.zip` | `启动拾录.command` |

下载包自带 Python 环境。双击启动后会打开浏览器，关闭程序请点击网页右上角“退出”。程序未签名，系统可能要求安全确认。

将资料放入同目录的 `materials`，点击“更新资料”。

```text
materials/
  qq message/         # 放入完整 QQ 分块导出目录，保留 manifest.json 和 chunks/
  wechat message/     # 放入微信 JSON 导出文件
  笔记与文章/          # TXT、Markdown、DOCX、PDF
```

## 功能

- 中文单字、短词、连续文本和准确短语检索，多个关键词默认同时匹配。
- 按日期、资料类型、来源、会话和说话人筛选，可只查明确标记的本人发言。
- 查看原文及前后文，定位来源文件、消息序号、行号、段落或页码。
- 增量更新、完整来源去重、导入失败明细、索引备份和重建。

## 支持格式与资料来源

| 资料 | 格式 |
| --- | --- |
| QQ | `manifest.json` + `chunks/*.jsonl` 分块导出 |
| 微信 | 包含 `session`、`messages` 的 WeFlow 标准 JSON |
| 笔记、文章 | TXT、Markdown、DOCX |
| PDF | 可提取文字的 PDF，不做 OCR |

聊天记录可通过 [WeFlow](https://github.com/jacklilyhello/WeFlow) 和 [qq-chat-exporter](https://github.com/shuakami/qq-chat-exporter) 导出。微信请选择标准 JSON，QQ 请选择分块 JSONL，并保留原目录结构。拾录读取已有导出文件，不连接 QQ 或微信客户端。其他导出结构可能无法识别。

图片、音视频和扫描件不转写；聊天关键词在单条消息内匹配。未知时间、未知身份会明确标注，文章与创作不会自动归为本人聊天。

## 数据与备份

原始文件保留在 `materials`，程序生成的索引、标准化副本和导入报告分别位于 `data`、`reports`。服务只监听 `127.0.0.1`，不上传资料。

退出程序后复制整个解压目录即可备份或迁移。也可双击“备份索引”脚本，备份保存到 `backups`，不包含原始文件；“重建索引”脚本会从原件重新导入。

## 从源码运行

需要 Python 3.10 或更新版本。

```sh
python -m pip install .
python -m shilu
```

MIT License
