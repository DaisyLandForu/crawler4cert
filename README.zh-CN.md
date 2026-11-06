# crawler4cert 通用学术文献采集工作台

基于 [Shirmer/AutoPaperCrawl](https://github.com/Shirmer/AutoPaperCrawl) 改造。网络安全是重点预设，工具支持其他领域；保留已有中文前端和 Python 包名 `autopapercrawl`。所有文件保存在个人电脑，不依赖云端平台、MCP、向量数据库或内网同步。

[English](README.md) · [审查和实施记录](docs/implementation.md) · [验收与交付报告](docs/delivery.zh-CN.md) · [Venue/Adapter 能力表](docs/capabilities.md)

## 1. 安装与 Windows 启动

**无需安装 Conda。** 推荐使用 Python 3.11（已验证版本），首次在项目目录创建本地虚拟环境并安装依赖：

```powershell
cd E:\crawler4cert
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

之后直接双击 **`start_workbench.cmd`**，无需手动激活环境。启动器依次检查以下候选，使用首个满足 Python 3.11+ 且能够导入 Requests、pypdf 的解释器：

1. 项目内的 `.venv`，其次 `venv`。
2. 当前已激活的普通虚拟环境、Conda 环境。
3. Windows Python 启动器 `py -3.11`，其次 PATH 中的 `python`。
4. 如果安装了 Conda，再兼容尝试原有的 `autopapercrawl` 环境。

不满足条件的候选会跳过；全部不可用时显示配置命令，不自动安装依赖或修改环境。启动窗口会显示实际 Python 和项目目录，默认打开 `http://127.0.0.1:8767`。保留窗口运行，按 Ctrl+C 退出。也可传入其他启动参数，例如 `start_workbench.cmd --port 8770 --no-browser`。

也可以直接用项目虚拟环境启动：

```powershell
.\.venv\Scripts\python.exe -m autopapercrawl serve --port 8767
```

已有 Conda 环境仍可继续使用，无需迁移：

```powershell
# 仅首次创建时执行；已有同名环境请跳过
conda env create -f environment.yml
conda activate autopapercrawl
python -m pip install -r requirements.txt
python -m autopapercrawl serve --port 8767
```

下文命令中的 `python` 指安装好依赖的解释器；使用本地虚拟环境时可替换为 `.\.venv\Scripts\python.exe`，或先激活该环境。不指定端口的命令行默认使用 8765，双击启动器使用 8767。服务仅监听本机 127.0.0.1；前端无需 Node 构建。系统文件夹选择器需要 Tk，缺少桌面/Tk 时可手工输入绝对路径。

## 2. 页面操作

页面按“设置条件 → 联网查询 → 查看与下载”组织。左侧保留方向、会议/期刊、标题、关键词、年份和展开的检索选项；右侧查看论文、展开摘要，并批量下载 PDF 或导出包含摘要的元数据。会议选择器点击后展开，每篇论文也可直接下载 PDF。

配置管理、采集能力、历史任务、检索计划与已有结果导入位于右上角“工具与设置”；结果区的“更多筛选”和“•••”收纳辅助筛选、重新分类、删除及失败重试。PDF 保存位置按需展开，任务诊断默认收起。“查看任务信息”和“任务详情”展示该任务的方向、会议、年份、标题、关键词、实际检索词、来源和下载设置；新任务保存创建时的条件快照，旧任务从原有 options/plan 读取，不使用当前查询表单补填。

**研究方向与本次关键词**：方向保存多组检索词和分类规则；本次关键词用于一次具体查询。关键词留空时，页面使用所选方向的全部有效检索词生成查询；填写后优先检索该关键词，方向仍用于分类，不会自动把两者拼为 AND 条件。

点击“＋ 自定义研究方向”，填写名称与检索词（ID 由系统自动分配，无需填写），勾选“保存到研究方向快捷展示列表”即可长期展示；不勾选时仍保存在“更多方向”中。之后可在“管理领域与会议”中更改该选项，刷新后仍保留。

1. 快捷列表默认显示卫星互联网安全、漏洞检测、加密流量分析检测、人工智能安全四个方向。“更多方向”可展开其他预设及未加入快捷列表的自定义方向；会议/期刊支持搜索、按领域筛选及多选。
2. 选择年份、关键词、标题和来源；页面固定使用全面模式，点击“预览实际检索计划”查看将执行的关键词与调用数量。
3. “检索选项”默认展开，可直接设置数量、数据来源，并勾选“查询后下载本次 PDF”。页面不展示任务名称及增量设置，后端接口与命令行仍保留相关能力。
4. 查看任务开始时间、来源统计、召回/去重/入库数量和 PDF 成功/失败数。历史任务支持重试。
5. 在本地库按标题、作者、摘要、领域、出处、年份、引用数、代码仓库、发表状态、分类置信度和下载状态筛选。
6. 支持当前页选择、跨页全选、批量下载、重新分类、删除记录以及 CSV/JSON/BibTeX/HTML 导出。有选择时仅导出所选的当前筛选结果。
7. 摘要可展开全文，详情保留作者、DOI、代码/原文/PDF 地址、版本快照、关联候选、来源原始数据和下载尝试。

删除文献记录前自动备份数据库，同时保留 PDF 和回收记录，不主动删除用户文件。页面来源查询失败、PDF 未找到和文件无效具有不同状态。正式 Venue 检索保留 arXiv；不能确认出处的预印本在本次任务结果中标记“出处待确认”，不会被假装成已确认会议论文。

## 3. 领域和会议配置

保留卫星互联网安全、漏洞检测、加密流量分析检测、人工智能安全四方向；新增人工智能与机器学习、计算机视觉、自然语言处理、软件工程、计算机网络、数据库与数据挖掘、分布式系统、理论计算机科学、机器人与自动化及其他自定义领域模板。

内置 15 个领域节点，其中 14 个可选模板、1 个禁用的网络安全父节点；56 个初始 Venue 配置。初始覆盖包含 S&P/CCS/USENIX Security/NDSS/ACSAC/RAID/ESORICS/EuroS&P/AsiaCCS/PETS/WiSec，NeurIPS/ICML/ICLR/AAAI/IJCAI/KDD/WWW/JMLR/TPAMI/TNNLS，CVPR/ICCV/ECCV/ACM MM/IJCV，ACL/EMNLP/NAACL/COLING/TACL，ICSE/FSE/ASE/ISSTA/TSE/TOSEM，以及 SIGCOMM/NSDI/SOSP/OSDI/ATC/IMC/MobiCom/INFOCOM/SIGMOD/VLDB/ICDE/PODS/TODS 等。

**56 是配置数，不是逐个实测成功数。** 实际状态见页面“采集能力”和验收报告，不写入虚构的评级或已验证标志。

“管理领域与会议”可新建、编辑、删除本地注册项。常用字段有表单，完整 JSON 可编辑父领域、排除词、ISSN、DBLP key、默认来源和分组规则等。修改内置方向的分类语义时可在 JSON 中显式设置 `required_groups: []`，改用普通加权关键词；否则继承原安全分组。领域与会议只作多对多建议关系，选择方向不会强制绑定会议。

- `config/domains.json`：领域字段 `domain_id/domain_name/description/parent_domain/keywords/excluded_keywords/venues/default_sources/enabled`。
- `config/venues.json`：名称、别名、类型、领域、DBLP key/URL、OpenAlex ID、ISSN/ISBN、官网、年份、adapters、评级、能力状态。
- `config/security_topics.json`：旧四方向分组规则，兼容原批量流程。
- `data/workbench/registries.json`：本地覆盖和验证记录；前端不会改写内置模板。

评级标签可使用 CCF/CORE/JCR，但每条必须提供 `system/source/version/year/rank`。未填写的标识、官网、年份范围保持未知，空 supported_years 不表示逐年验证。

## 4. 检索模式与 CLI

默认当前年份及前两年、最新优先。快速每方向 1 组词，标准最多 3 组，全面全部有效词组。明确输入关键词/标题时以输入作为检索表达式，页面计划会显示这种情况。全面模式不会被旧 query_limit 截断，但每来源每词候选上限（默认 30）和最终结果上限（默认 100）依然有效，不保证全量收录。

```powershell
python -m autopapercrawl search --direction ai_ml --mode comprehensive --plan-only
python -m autopapercrawl search --direction encrypted_traffic --mode standard --download --output-dir "E:/Papers"
python -m autopapercrawl search --venue aaai --query "learning" --limit 30
python -m autopapercrawl library --title "traffic" --export papers.json
python -m autopapercrawl library --export references.bib
python -m autopapercrawl download --direction ai_security --all --output-dir "E:/Papers"
python -m autopapercrawl retry JOB_ID
python -m autopapercrawl backup
```

`--direction/--venue/--id` 在适用命令中可重复。无方向的显式标题/关键词/Venue 检索不受安全领域限制。当前 API 年份范围延续旧版的 1991 年至今年。

普通任务配置可以保存为 JSON，避免每次编写脚本：

```powershell
python -m autopapercrawl run --config config/task.example.json --plan-only
python -m autopapercrawl run --config config/task.example.json
```

任务返回 0 表示无来源警告完成；来源部分失败返回 2；执行异常为非零。通过 config JSON 可设置 `enrich_metadata` 和 `enrichment_limit` 控制可选 DOI 摘要补全预算。运行报告分别记录检索来源统计和补全请求统计。

## 5. 来源能力和 PDF 边界

| 来源/路径 | 实际实现 | 注意 |
| --- | --- | --- |
| OpenAlex | 元数据、来源 ID 查询、OA PDF 地址 | 可选 `OPENALEX_API_KEY`；本次多个真实请求 429 |
| Crossref | DOI/出版元数据、期刊 ISSN 和会议名称召回 | 无摘要或没有 PDF 地址不代表请求失败；会议名称后置校验 |
| arXiv | 预印本元数据、分页、日期、DOI/journal_ref/分类 | 不等于同行评审；正式发表信息需要关联确认 |
| DBLP | JSON 元数据、目录 stream、年份、分页、DOI/作者 | 本次受访问校验限制，尚未真实验证成功，不绕过校验 |
| Publisher Adapter | 配置公开目录、robots.txt、citation meta 解析 | 有界目录发现，不是所有出版社的通用站内搜索；未逐站验证 |
| public PDF | 公开 URL、arXiv、OpenAlex、Unpaywall、已支持公开落地页 | 可用元数据链接与允许的公开路径，不绕过付费墙 |
| GSW/SEG 原生 provider | 保留原实现和合法授权入口 | 本轮未真实验证，浏览器分支可选依赖 Playwright |
| ScienceDirect/CNKI/OnePetro 原生 provider | 占位，未实现 | 与通用公开 PDF 链接下载区分 |

`.env` 可配置 `PAPER_EMAIL` 启用 Unpaywall，以及可选 `OPENALEX_API_KEY`。OpenAlex 密钥只放在该域名的 Authorization 头中，不写入检索 URL、配置导出或报告。参见 [OpenAlex 官方认证说明](https://help.openalex.org/api/authentication/)。大模型 API Key 仅用于可选精选，不是基本采集依赖。

DBLP 接受 `conf/sp`、`journals/jmlr`、`https://dblp.org/db/conf/sp/index.html` 和 `https://dblp.org/streams/conf/sp.xml` 等目录地址。数据来自官方结构化接口，未收录/未配置会明确报告不支持；DBLP 不提供摘要和 PDF，按 DOI 可选从 OpenAlex/Crossref 补全。没有 DOI 时不假造摘要。源码独立实现，未复制没有明确源码许可证的 paperCrawler Spider；DBLP 元数据为 [CC0](https://dblp.org/faq/1474677.html)。

## 6. 数据质量与下载

去重优先 DOI → arXiv ID → DBLP ID → 标题/年份/作者指纹。冲突 DOI 保留独立实体；预印本和发表记录关联时保存各自版本快照、DOI、出处及 PDF 地址。近似标题只生成待复核关联，不直接合并。仅有名称/别名的出处为 probable；ISSN、DBLP stream、Source ID 或有证据的 DOI 发表关联支持 confirmed。

分类保存 High/Medium/Low、分数、命中字段、命中词、规则版本和排除词。High 自动归类；Medium 提示复核；Low 保留资料，不作为高相关推荐。题名摘要规则仍可能误报，不能当作论文质量评审。

PDF 下载采用流式大小限制（默认 100 MiB，可通过工作台配置 max_pdf_mb 修改）、文件头/结束标记、pypdf 页面解析、SHA-256、内容重复识别、已有有效文件跳过。未实现 HTTP Range 断点续传；中断后的临时文件不认作成功，下次重新下载。解析通过不等于逐页内容或论文身份经过人工核验。下载失败不删除文献元数据。

## 7. 保存目录、SQLite 迁移与恢复

```text
config/                                  内置模板和默认参数
  task.example.json                      可重复运行的任务条件
  domains.json / venues.json             领域与会议注册表
  workbench.json / security_topics.json   默认参数与旧安全规则
data/workbench/
  library.sqlite3                        论文、任务、索引、增量检查点
  registries.json                        本地覆盖与验证状态
  backups/                               迁移或删除前的数据库副本
outputs/tasks/<job_id>/
  raw_records.jsonl / candidates.json     原始返回和候选（含低相关/超限记录）
  task.json                              完整计划、参数、来源统计和状态
  papers.json / papers.csv / papers.bib   文献导出
  report.html                            可读报告
<用户选择的下载目录>/
  pdfs/                                  PDF 总库
  <domain_id>/pdfs/                       方向归档，可交叉出现
  reports/<job_id>/                      下载尝试和可读报告
```

成功下载的论文按实际确认的分类归档，不会把每篇论文放入所有勾选方向。一篇论文匹配多个方向时，各方向目录均有可用 PDF；未确认分类的论文仅存于 `pdfs/` 总目录。优先通过硬链接复用文件，无法建立硬链接时复制。四个初始方向目录分别为 `satellite_security`、`vulnerability_detection`、`encrypted_traffic`、`ai_security`，各目录内的 PDF 位于 `pdfs/` 子目录；自定义方向使用系统生成的稳定标识归档，方向名称可在下载报告中查看。

SQLite 使用增量 schema version 3：保留旧 document JSON，加索引列、FTS5、标识映射、来源状态、检查点和删除回收表。迁移前使用 SQLite backup API 自动备份，不移动原 PDF，不覆盖用户本地配置。列表筛选、排序和分页下推 SQL，跨页全选只返回轻量 ID 列表。FTS5 用于索引准备，目前基本筛选仍使用可预测的子串匹配。

手动备份：`python -m autopapercrawl backup`。恢复前关闭所有工作台/定时采集进程，先另行保存当前库，再将选定备份复制为 `data/workbench/library.sqlite3`；不在应用运行时覆盖数据库。旧库导入路径延续 `outputs/security_research/raw/main/papers.json` 和对应下载报告。原先 `E:/AutoPaperCrawl` 的论文与数据库不会自动搬走。

## 8. 增量与 Windows 定时

增量检查点按完整条件隔离；自动从同一计划上次成功起点向前重叠默认 7 天。不同主题/Venue 不共用游标。失败、取消、候选数达到上限或最终超限的任务不推进自动检查点，下次重复检索并跳过已有 PDF。仍保存每个来源/Venue 最近成功时间。

增量基于发表日期，DBLP 只能缩小到年份；不保证发现无限延迟收录的旧论文，建议定期跑所选年份的完整检索。电脑关机或断网时不能采集，没有云端调度。

任务计划程序使用已安装依赖的 Python 绝对路径，可来自项目 venv 或 Conda，无需先激活环境。先复制并编辑任务 JSON、预览检索计划，再预览本地计划任务：

```powershell
powershell -NoProfile -File scripts/windows_schedule.ps1 -PythonPath "E:/crawler4cert/.venv/Scripts/python.exe" -ConfigPath "config/task.example.json" -At "09:00"
# 确认配置后才注册（会创建本机计划任务，不会覆盖同名任务）：
powershell -NoProfile -File scripts/windows_schedule.ps1 -PythonPath "E:/crawler4cert/.venv/Scripts/python.exe" -ConfigPath "config/task.example.json" -At "09:00" -Register
```

也可手工在任务计划程序添加动作：程序为上述 Python 路径，参数为 `-m autopapercrawl run --config "E:/crawler4cert/config/task.example.json"`，起始位置为 `E:/crawler4cert`；配置“已有实例运行时不启动新实例”。本次仅验证预览，没有擅自创建计划任务。

## 9. 扩展和兼容入口

新增来源继承 `adapters/base.py`，实现 search/normalize/health_check/get_capabilities，并通过 `register_adapter(id, class)` 注册；返回 Paper 且保留 raw_records。新增下载器可通过 `downloaders/router.py` 的 `register_downloader(id, handler)` 显式注册，成功结果仍经 PDF 校验。自定义实现是本地 Python 代码，不从前端执行任意代码。

原 `scripts/security_pipeline.py` 保留四安全方向批量采集，默认全部 29 组词，每来源每词 40 候选，每方向最多 200；候选和未推荐条目保留在独立文件。通用领域/UI/定时任务优先使用 workbench CLI，避免混淆两种入口的默认数量。

`python scripts/security_pipeline.py --with-papercrawler --papercrawler-path "E:/paperCrawler"` 可使用原会议桥接，需 `python -m pip install -r requirements-security.txt`。外部 checkout 必须含 `crawl_conf/scrapy.cfg`；年份支持以该 checkout 为准。其代码不随本仓库复制。`scripts/curate_project.py` 保留偏向网络安全的本地/可选 LLM 精选。

## 10. 测试与真实验证

```powershell
python -m unittest discover -s tests -p "test_*.py"
python -m pip install playwright
python tests/browser_smoke.py
python scripts/verify_sources.py --limit 3 --pdf-sample 1
```

最后一条会实际联网并下载少量样本；其他回归测试使用 Mock/本地服务或临时数据。浏览器测试需已安装 Microsoft Edge。Scrapy 桥接集成测试只在安装可选依赖时运行，不以此跳过核心回归。实际计数、失败原因和迁移验证见[交付报告](docs/delivery.zh-CN.md)。

本轮仅修改本地实现与文档，没有提交或推送远端。来源于 AutoPaperCrawl 的归属说明保留；PDF、数据库、Cookie、密钥、下载报告、截图和临时输出受 .gitignore 排除。
