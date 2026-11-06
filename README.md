# crawler4cert

面向网络安全研究的论文检索与下载工具，基于 [AutoPaperCrawl](https://github.com/Shirmer/AutoPaperCrawl) 改造。通过本地网页查询论文、查看摘要、导出元数据并下载公开 PDF。

## 主要功能

- 按研究方向、会议/期刊、标题、关键词和年份联网检索。
- 默认提供卫星互联网安全、漏洞检测、加密流量分析检测、人工智能安全四个方向，支持自定义方向和检索词。
- 接入 arXiv、OpenAlex、Crossref、DBLP 等来源，支持去重、多方向分类与任务记录。
- 查看摘要，导出 CSV、JSON、BibTeX 和阅读报告；单篇或批量下载 PDF，按方向分目录归档。

## 快速启动

推荐使用 **Python 3.11**。首次安装：

```powershell
git clone https://github.com/DaisyLandForu/crawler4cert.git
cd crawler4cert
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

之后双击 **`start_workbench.cmd`**，浏览器会打开 [论文工作台](http://127.0.0.1:8767)。启动器自动查找可用的 Python 环境；使用期间保持启动窗口运行。

也可手动启动：

```powershell
.\.venv\Scripts\python.exe -m autopapercrawl serve --port 8767
```

## 使用流程

1. 选择研究方向、年份，可补充会议/期刊、标题或关键词，点击“联网查论文”。
2. 点击论文标题或“展开摘要”查看内容，勾选结果后导出元数据或下载 PDF。
3. 在“PDF 保存位置”选择目录，也可勾选“查询后下载本次 PDF”。

关键词留空时，使用方向预设的全部检索词；填写后优先查询该关键词。自定义方向、历史任务等位于“工具与设置”。

PDF 保存在所选目录的 `pdfs/` 总库及对应方向子目录；匹配多个方向的论文会分别归档。数据来源、公开全文权限会影响查询和下载结果，自动分类结果建议复核。

## 更多说明

[详细使用文档](README.zh-CN.md) · [数据源与会议覆盖](docs/capabilities.md) · [实现说明](docs/implementation.md) · [验收记录](docs/delivery.zh-CN.md)
