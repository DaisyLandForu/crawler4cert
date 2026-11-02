# crawler4cert

基于 [Shirmer/AutoPaperCrawl](https://github.com/Shirmer/AutoPaperCrawl) 改造的本地论文检索与 PDF 下载工具，面向网络安全研究。保留 Python 包名 `autopapercrawl`，提供中文网页和固定命令行入口，无需为每次检索编写脚本。

## 功能

- 从 arXiv、OpenAlex、Crossref 检索，支持年份、研究方向、会议/期刊、标题和关键词条件。
- 默认检索当前年份及前两年，优先展示最新论文；例如 2026 年运行时为 2024—2026 年，截止运行当天。
- 预设卫星互联网安全、漏洞检测、加密流量分析检测、人工智能安全四个方向；按标题、摘要规则分类，支持一篇论文属于多个方向。
- 本地论文库、分页、多选、跨页全选、选择保存目录、复用已下载 PDF，以及失败重试。
- 优先寻找公开全文，记录下载成功、未找到公开全文、请求失败、文件无效等结果；导出中文 CSV、HTML 和 JSON 报告。
- 可选使用本地 paperCrawler 的会议官网爬虫补充采集；也可单独运行网络安全论文精选。

## 安装与启动

需要 Conda；建议 Python 3.11。在项目根目录执行：

```powershell
git clone https://github.com/DaisyLandForu/crawler4cert.git
cd crawler4cert
conda env create -f environment.yml
conda activate autopapercrawl
python -m autopapercrawl
```

浏览器自动打开 `http://127.0.0.1:8765`。服务仅绑定本机地址，保持终端运行，按 `Ctrl+C` 退出。Windows 也可在创建环境后双击 `start_workbench.cmd`。

如已有同名环境，只需在该环境内执行 `python -m pip install -r requirements.txt`。主流程只需要 `requests`；不需要安装浏览器引擎或配置大模型密钥。

指定其他端口或关闭浏览器自动打开：

```powershell
python -m autopapercrawl serve --port 8766 --no-browser
```

页面操作：设置检索条件 → 查询 → 勾选论文或全选所有符合条件的结果 → 选择保存目录 → 下载。已有论文可在“本地论文库”筛选和重试下载。

## 命令行

```powershell
# 查询四个预设方向，仅入库，不下载
python -m autopapercrawl search --direction all --limit 100

# 查询一个方向并下载，年份按需调整
python -m autopapercrawl search --start-year 2024 --end-year 2026 --direction encrypted_traffic --download --output-dir "E:/Papers"

# 不指定方向时，显式关键词、标题或会议/期刊查询不受四方向规则限制
python -m autopapercrawl search --venue CCS --query "website fingerprinting" --limit 50
python -m autopapercrawl search --title "encrypted traffic" --download

# 筛选本地论文并导出目录
python -m autopapercrawl library --direction satellite_security --export satellite.csv

# 下载全部符合本地筛选条件的论文；也可多次传入 --id 选择论文
python -m autopapercrawl download --direction encrypted_traffic --all --output-dir "E:/Papers"
python -m autopapercrawl download --id PAPER_ID --output-dir "E:/Papers"
```

`--direction`、`--venue`、`--id` 支持重复传入。`search --download` 下载本次查询结果；`download --all` 下载符合本地筛选条件的全部记录。每个子命令均可通过 `--help` 查看参数。

方向 ID：

| ID | 方向 |
| --- | --- |
| `satellite_security` | 卫星互联网安全 |
| `vulnerability_detection` | 漏洞检测 |
| `encrypted_traffic` | 加密流量分析检测 |
| `ai_security` | 人工智能安全 |

## 默认参数和配置

- `config/security_topics.json`：四方向检索词、标题/摘要分类规则。当前共有 29 组检索词。
- `config/workbench.json`：页面与命令行默认检索数量、请求参数、保存目录。
- 工作台默认每方向使用前 2 组词、每个来源每组词取 30 个候选，最终最多保留 100 条；默认分页 50 条，超时 30 秒、重试 1 次、请求间隔 3 秒。可在页面高级选项或配置文件中调整。`query_limit=0` 使用全部检索词。
- 可复制 `.env.example` 为 `.env` 并填写 `PAPER_EMAIL`，用于联系信息和可选的 Unpaywall 公开全文查询。大模型配置只用于可选精选流程。

## 文件保存位置

```text
downloads/security_research/        默认下载根目录，可在页面或 CLI 修改
  pdfs/                            去重后的 PDF 总库
  satellite_security/pdfs/          卫星互联网安全归档
  vulnerability_detection/pdfs/     漏洞检测归档
  encrypted_traffic/pdfs/           加密流量分析检测归档
  ai_security/pdfs/                 人工智能安全归档
  reports/<任务编号>/              工作台每批下载报告
data/workbench/library.sqlite3      本地论文库与任务状态
outputs/security_research/          可选批量采集入口生成的元数据和报告
```

同一论文可出现在多个方向目录中，总库按论文标识去重。网页导出包含论文信息、下载状态和失败原因；每条记录保留原始请求尝试信息，便于追查。切换保存目录时可复用、复制已有 PDF。

## 可选批量采集与会议补充

`scripts/` 仅保留可重复使用的运行入口及其依赖，不包含临时验证、出版社探测和历史采集脚本。

```powershell
# 一次执行全部预设方向的批量采集，默认下载 PDF
python scripts/security_pipeline.py

# 只生成检索计划，不联网
python scripts/security_pipeline.py --dry-plan
```

批量入口默认使用全部检索词，每来源每词最多 40 个候选，每方向最多保留 200 篇。可用 `--max-per-direction 0` 取消最终方向数量上限；这不取消各来源的候选数量限制。

如需启用已有 [paperCrawler](https://github.com/sucv/paperCrawler) 本地 checkout 中的会议爬虫：

```powershell
python -m pip install -r requirements-security.txt
python scripts/security_pipeline.py --with-papercrawler --papercrawler-path "E:/paperCrawler" --conference-venues "cvpr,iclr,acl,emnlp"
```

外部 checkout 需包含 `crawl_conf/scrapy.cfg`，不会随本仓库上传。爬虫可用会议、年份和字段取决于该 checkout；桥接器记录不支持的年份、超时和部分采集结果。会议官网补充仅在批量入口显式启用，工作台中的会议筛选仍通过通用元数据源查询。

可选网络安全精选使用 `scripts/curate_project.py --project-dir outputs/security_research`；加 `--use-llm` 才调用 `.env` 中配置的模型服务。`scripts/llm_utils.py` 提供环境配置和调用辅助，`citation-management/scripts/doi_to_bibtex.py` 仅供精选结果导出 BibTeX 使用。

## 下载状态与已知局限

- “已下载”表示文件通过 PDF 头尾基础完整性检查并已保存，不代表逐页内容、论文身份和学术质量均经人工核验。
- “未找到公开全文”表示本次没有找到可用公开 PDF 地址，不表示论文没有全文。请求失败、访问校验、链接失效会单独记录。
- arXiv 是预印本平台；OpenAlex 是学术索引；Crossref 提供 DOI 和出版元数据。检索来源不等同于会议/期刊，也不代表质量认证。
- 当前为标题/摘要关键词自动初筛，可能混入偏题论文，缺少摘要也可能漏检；尚未实现逐篇人工评审、顶会白名单精选或全量覆盖保证。
- 会议/期刊筛选依赖数据源元数据、名称别名与后置校验；arXiv 常缺少正式发表来源。旧采集数据缺少会议/期刊字段时，本地来源筛选可能漏掉记录。
- 检索有候选数量上限，API 可能限流；公开全文解析受站点策略和链接更新影响。工具不会绕过付费墙或自动破解访问校验。
- 页面文件夹选择器使用 Tk；无桌面或缺少 Tk 时可以直接输入绝对保存路径。

## 验证与代码审核

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

保留检索分页与时间过滤、分类去重、公开 PDF 下载及复用、会议桥接、工作台/API 等回归测试。测试主要使用本地服务和模拟响应，不代表外部站点的实时覆盖率。

审核入口：`search.py`（数据源查询）、`topics.py` 和方向配置（分类）、`open_access.py`（全文解析）、`workbench.py`（论文库与任务）、`web.py` 和 `web_assets/`（页面/API）。优先审核分类误报、论文身份匹配、来源字段缺失、限流重试与任务恢复。

本仓库仅包含必要实现、配置、回归测试和本 README。PDF、论文清单、数据库、下载报告、Cookie、密钥、截图和临时输出均不提交。源码基于 AutoPaperCrawl 本地版本 `0ba3ab0` 及后续网络安全改造整理，保留上游来源说明。
