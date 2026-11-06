# 本地改造交付与验收

审查及开发从 2026-11-02 开始，所有修改位于 `E:/crawler4cert`。原 AutoPaperCrawl、paperCrawler checkout 及 PDF 不搬移、不删除。开发过程中未注册系统定时任务；代码发布状态以仓库 Git 历史为准。

## 1. 审查问题与架构调整

原基线 8fe7485，47项测试。存在四方向/别名硬编码、来源逻辑混合、Venue查询排除arXiv、分类失败不入库、版本信息合并不足、CSV字段过少、PDF仅头尾检测、SQLite无迁移和全量Python列表过滤等问题。此前描述的BM25出版社检索不在此发布版工作区，未虚称保留。上游DBLP Scrapy机制和许可证审查见 implementation.md。

## 2. 已完成的改造

- 数据驱动的15个领域节点（14个启用模板）、56个初始Venue；本地覆盖、前端CRUD、评级来源校验。
- 三个原有来源独立Adapter，新增DBLP结构化目录Adapter和公开出版社目录Adapter；兼容原search函数和批量入口。
- 实际执行计划：快速/标准/全面，原始记录归档、来源统计、可选DOI摘要补全、出处证据分级、arXiv发表关联保留。
- DOI/arXiv/DBLP/标题年份作者指纹，独立版本快照与待复核相似关联；跨独立采集的精确标题版本关系持久化。
- 分类High/Medium/Low、多标签、命中字段/词/分数/规则版本；低相关及超限候选在原始文件保留。
- PDF大小限制、流式写入、pypdf解析、SHA-256、同内容复用、已有文件跳过、失败与元数据分离。
- SQL筛选、排序、分页，FTS5索引准备、schema version 3、迁移备份、删除回收与PDF保留。
- 保留原前端，加入注册表编辑、可搜索分类Venue多选、计划预览、任务历史、重试、扩展筛选、摘要展开、批量重分类/删除及完整导出。
- 保存完整任务条件、按计划隔离增量检查点、7天默认重叠、截断/失败不推进、Windows任务计划程序预览辅助。

## 3. 会议期刊与 4. 数据源

完整56项名称、DBLP key和配置来源见 capabilities.md。来源增加DBLP与publisher，不新增云端服务。配置数量与真正采集成功数量分开；官网、年份或来源ID缺失保持未知。

## 5. DBLP实际范围 / 6. 元数据与PDF分离

实现官方JSON搜索、conf/journals stream、年份范围、offset分页、标题/作者/DOI/稳定ID与原始记录；支持用户自定义官方目录URL；无摘要/PDF时可选按DOI补全。单元测试包含分页、空年份、未知目录、重复页面、字段解析与自定义期刊。

当前网络请求返回HTTP 202访问校验HTML，未绕过，尚无DBLP真实成功样本。因此能力为实现完成但本环境暂不可用。OpenAlex后续多为429；Crossref和arXiv有真实返回。Publisher通用目录仅本地页面验证，不承诺所有出版社。实际PDF由公开全文解析/下载链路完成；原生占位provider不能标成支持下载。

## 7. 数据模型与 8. 前端

Paper新增完整作者/机构/出处/标识/类别/概念/代码/本地PDF/来源/分类/版本状态。JSON保留raw_records与版本；CSV含完整扩展元数据；BibTeX逐条导出。模糊关联不自动合并，姓名/年份冲突不会仅凭题名合并。

浏览器验收使用Microsoft Edge和临时120条本地记录，JavaScript错误为0，验证分页、跨页全选、29词全面计划、自定义方向、能力状态、任务查询、详情、重新分类、JSON导出、删除留PDF、自选目录复用PDF、引用排序、摘要展开和移动端布局。截图：outputs/browser_acceptance/desktop.png、mobile.png。

## 9. 增量与定时

完整条件哈希隔离检查点，只有任务全部正常、没有候选/最终数量截断且未取消才更新。窗口基于发表日期、DBLP基于年份；不能保证检出任意迟延索引的旧论文，应周期性全范围复查。Windows辅助脚本仅运行了预览，没有擅自注册。命令见README.zh-CN.md第8节。

## 10. 自动化与数据库兼容验证

截至 2026-11-06，92项回归/单元/API/本地HTTP及 Windows 启动器测试通过，包含原有47项对应功能；原先要求排除arXiv和跨年份直接合并预印本的旧断言，按新需求替换为保留及证据验证的更强断言，未删除测试。原简易假PDF夹具换成真实可解析PDF。可选Scrapy集成测试在当前环境实际运行。

额外做旧库副本验证：原E:/AutoPaperCrawl/data/workbench/library.sqlite3当前为763条记录（含后续工作台入库记录）；副本迁移后763条不变、553条已有下载状态可定位、PRAGMA integrity_check=ok，自动备份创建成功。原库文件SHA-256在测试前后一致，没有修改原库。

## 11. 真实采集测试数据

下表为最终9个用例加3个补充用例；均为真实网络请求，没有Mock。每来源每词最多3个候选、2024—2026年，关闭摘要补全以区分来源原始完整度，最多选1个已有PDF地址做下载样本。0/0表示未找到带PDF地址的测试候选，没有尝试，不能称为下载失败率100%。

| 领域 | 类型/Venue | 召回 | 重复 | 保留/出处匹配 | 有摘要/保留 | PDF成功/尝试 |
| --- | --- | ---: | ---: | --- | --- | --- |
| 网络安全 | conference / sp | 3 | 0 | 2 / 2 | 0 / 2 | 0 / 0 |
| 网络安全 | journal / cose | 3 | 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 网络安全 | preprint / arXiv | 3 | 0 | 3 / 0 | 3 / 3 | 1 / 1 |
| AI/ML | conference / icml | 3 | 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| AI/ML | journal / jmlr | 0 | 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| AI/ML | preprint / arXiv | 3 | 0 | 3 / 0 | 3 / 3 | 1 / 1 |
| 计算机视觉 | conference / cvpr | 3 | 0 | 3 / 3 | 0 / 3 | 0 / 0 |
| 计算机视觉 | journal / ijcv | 3 | 0 | 3 / 3 | 0 / 3 | 0 / 1 |
| 计算机视觉 | preprint / arXiv | 3 | 0 | 3 / 0 | 3 / 3 | 1 / 1 |
| 网络安全 | journal / cose | 3 | 0 | 3 / 3 | 0 / 3 | 0 / 0 |
| AI/ML | conference / aaai | 3 | 0 | 3 / 3 | 3 / 3 | 1 / 1 |
| AI/ML | journal / tnnls | 3 | 0 | 3 / 3 | 0 / 3 | 0 / 1 |

这些是有限样本的返回/匹配数，不是会议全量覆盖率；保留数包含候选而非人工精选。最终加补充测试的PDF样本共6次尝试、4次成功，其他失败的元数据仍保存；不同运行会因限流改变结果。初轮OpenAlex尚能返回记录，S&P样本为4条、3条有摘要、1个PDF成功；随后复验429。为避免混淆，初轮原始文件保留但未混入上表。

机器学习ICML：Crossref小候选池没有可确认归属，DBLP访问校验，OpenAlex429；JMLR：Crossref无返回，其他两源同样受限，均不能称为已验证。补充AAAI实测3条且都有摘要、样本PDF成功；补充TNNLS3条有出处，缺少摘要且样本PDF请求失败。安全C&S补入正确ISSN后得到3条明确期刊记录。所有失败原因见各summary.json和任务诊断。

结果文件（受Git忽略）：

- outputs/live_acceptance_final_20261102/summary.json
- outputs/live_acceptance_supplement_20261102/summary.json
- outputs/live_acceptance_20261102/summary.json（早期版本实测记录）
- 各目录outputs/tasks/<job_id>/raw_records.jsonl、task.json和论文导出；下载样本位于各验收目录downloads。

## 12. 当前限制

- DBLP访问校验、OpenAlex429和出版社权限仍是外部限制；不能保证56个配置逐项可采。
- Publisher是显式公开目录的有界发现，尚无通用站内搜索/BM25搜索引擎，不能替代全部官网爬虫。
- 小候选池、缺摘要、别名歧义会影响召回和分类；没有顶会质量精选保证。
- 版本关系为保守规则，扩展版大幅改题或改作者顺序时可能未关联；近似候选必须人工复核。
- PDF解析通过不证明论文身份/内容质量；没有HTTP Range续传，以已有完整文件跳过代替。
- FTS5已建索引但默认子串筛选不使用FTS排名；跨页全选仍返回全部匹配ID，不返回全部文档。
- 定时采集仅在电脑运行及联网时工作；本地应用不是多人平台。

## 13. 最终启动

启动器自动查找项目 venv、当前环境及系统 Python，Conda 为可选兼容方式。前端快捷区默认显示四个安全方向，支持自动分配 ID、自定义快捷列表和任务检索条件展示；页面固定使用全面模式，辅助功能收纳于“工具与设置”和结果区次级菜单。

```powershell
cd E:\crawler4cert
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\start_workbench.cmd
```

浏览器打开 http://127.0.0.1:8767 。原项目/旧工作台可能继续占用8765，两个目录的数据不会自动互相覆盖。使用保存的任务JSON即可长期重复执行，具体CLI、备份、恢复和定时说明见README.zh-CN.md。
