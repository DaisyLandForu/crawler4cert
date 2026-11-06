# Venue 与 Adapter 能力清单

清单来自 `config/venues.json`，不是逐项实测成功承诺。本地用户覆盖位于 `data/workbench/registries.json`；页面运行时叠加真实验证状态。

## Adapter 能力

| Adapter | 元数据 | Venue目录/约束 | 预印本 | 自身下载PDF | 验证边界 |
| --- | --- | --- | --- | --- | --- |
| openalex | 是 | Source ID | 关联来源 | 否，提供OA地址 | 初轮有成功；复验429 |
| crossref | 是 | ISSN/名称召回后校验 | 关联DOI | 否，可能有PDF链接 | S&P、AAAI、CVPR、C&S、IJCV、TNNLS样本有成功 |
| arxiv | 是 | journal_ref/DOI待校验 | 是 | 否，提供PDF地址 | 三领域元数据与PDF路由样本通过 |
| dblp | 是 | stream目录及年份分页 | 非预印本专用 | 否 | 单元测试通过，真实请求访问校验，未标已验证 |
| publisher | citation meta | 显式配置公开catalog，核对robots | 视站点 | 否，可能有PDF链接 | 有界通用实现；本轮仅本地模拟页面验证 |

PDF 是独立能力：`open_access.py` 和公开 URL 下载器负责实际下载。仅注册 provider 不等于实现原生下载。ScienceDirect、CNKI、OnePetro 原生路径仍为占位；GSW/SEG 原实现保留，本轮未逐站验证。

## 内置 Venue 注册表

空 ISSN、OpenAlex ID、官网或 supported_years 表示待补充；不会推断全部年份已验证。所有内置 capability_status 均为 configured。评级标签默认空，需录入来源、版本和年份后才能使用。

| ID | 名称 | 类型 | DBLP key | 配置的来源 |
| --- | --- | --- | --- | --- |
| sp | IEEE Symposium on Security and Privacy | conference | conf/sp | dblp, openalex, crossref |
| ccs | ACM SIGSAC Conference on Computer and Communications Security | conference | conf/ccs | dblp, openalex, crossref |
| usenix | USENIX Security Symposium | conference | conf/uss | dblp, openalex, crossref |
| ndss | Network and Distributed System Security Symposium | conference | conf/ndss | dblp, openalex, crossref |
| acsac | Annual Computer Security Applications Conference | conference | conf/acsac | dblp, openalex, crossref |
| raid | Research in Attacks Intrusions and Defenses | conference | conf/raid | dblp, openalex, crossref |
| esorics | European Symposium on Research in Computer Security | conference | conf/esorics | dblp, openalex, crossref |
| eurosp | IEEE European Symposium on Security and Privacy | conference | conf/eurosp | dblp, openalex, crossref |
| asiaccs | ACM Asia Conference on Computer and Communications Security | conference | conf/asiaccs | dblp, openalex, crossref |
| pets | Proceedings on Privacy Enhancing Technologies | journal | journals/popets | dblp, openalex, crossref |
| wisec | ACM Conference on Security and Privacy in Wireless and Mobile Networks | conference | conf/wisec | dblp, openalex, crossref |
| nips | Advances in Neural Information Processing Systems | conference | conf/nips | dblp, openalex, crossref |
| icml | International Conference on Machine Learning | conference | conf/icml | dblp, openalex, crossref |
| iclr | International Conference on Learning Representations | conference | conf/iclr | dblp, openalex, crossref |
| aaai | AAAI Conference on Artificial Intelligence | conference | conf/aaai | dblp, openalex, crossref |
| ijcai | International Joint Conference on Artificial Intelligence | conference | conf/ijcai | dblp, openalex, crossref |
| kdd | ACM SIGKDD Conference on Knowledge Discovery and Data Mining | conference | conf/kdd | dblp, openalex, crossref |
| www | The Web Conference | conference | conf/www | dblp, openalex, crossref |
| jmlr | Journal of Machine Learning Research | journal | journals/jmlr | dblp, openalex, crossref |
| tpami | IEEE Transactions on Pattern Analysis and Machine Intelligence | journal | journals/pami | dblp, openalex, crossref |
| tnnls | IEEE Transactions on Neural Networks and Learning Systems | journal | journals/tnn | dblp, openalex, crossref |
| cvpr | IEEE/CVF Conference on Computer Vision and Pattern Recognition | conference | conf/cvpr | dblp, openalex, crossref |
| iccv | IEEE/CVF International Conference on Computer Vision | conference | conf/iccv | dblp, openalex, crossref |
| eccv | European Conference on Computer Vision | conference | conf/eccv | dblp, openalex, crossref |
| mm | ACM International Conference on Multimedia | conference | conf/mm | dblp, openalex, crossref |
| ijcv | International Journal of Computer Vision | journal | journals/ijcv | dblp, openalex, crossref |
| acl | Annual Meeting of the Association for Computational Linguistics | conference | conf/acl | dblp, openalex, crossref |
| emnlp | Conference on Empirical Methods in Natural Language Processing | conference | conf/emnlp | dblp, openalex, crossref |
| naacl | North American Chapter of the Association for Computational Linguistics | conference | conf/naacl | dblp, openalex, crossref |
| coling | International Conference on Computational Linguistics | conference | conf/coling | dblp, openalex, crossref |
| tacl | Transactions of the Association for Computational Linguistics | journal | journals/tacl | dblp, openalex, crossref |
| icse | International Conference on Software Engineering | conference | conf/icse | dblp, openalex, crossref |
| fse | ACM International Conference on the Foundations of Software Engineering | conference | conf/sigsoft | dblp, openalex, crossref |
| ase | IEEE/ACM International Conference on Automated Software Engineering | conference | conf/kbse | dblp, openalex, crossref |
| issta | International Symposium on Software Testing and Analysis | conference | conf/issta | dblp, openalex, crossref |
| tse | IEEE Transactions on Software Engineering | journal | journals/tse | dblp, openalex, crossref |
| tosem | ACM Transactions on Software Engineering and Methodology | journal | journals/tosem | dblp, openalex, crossref |
| sigcomm | ACM SIGCOMM Conference | conference | conf/sigcomm | dblp, openalex, crossref |
| nsdi | USENIX Symposium on Networked Systems Design and Implementation | conference | conf/nsdi | dblp, openalex, crossref |
| sosp | ACM Symposium on Operating Systems Principles | conference | conf/sosp | dblp, openalex, crossref |
| osdi | USENIX Symposium on Operating Systems Design and Implementation | conference | conf/osdi | dblp, openalex, crossref |
| atc | USENIX Annual Technical Conference | conference | conf/usenix | dblp, openalex, crossref |
| imc | ACM Internet Measurement Conference | conference | conf/imc | dblp, openalex, crossref |
| mobicom | International Conference on Mobile Computing and Networking | conference | conf/mobicom | dblp, openalex, crossref |
| infocom | IEEE International Conference on Computer Communications | conference | conf/infocom | dblp, openalex, crossref |
| sigmod | ACM SIGMOD International Conference on Management of Data | conference | conf/sigmod | dblp, openalex, crossref |
| vldb | International Conference on Very Large Data Bases | conference | conf/vldb | dblp, openalex, crossref |
| icde | IEEE International Conference on Data Engineering | conference | conf/icde | dblp, openalex, crossref |
| pods | ACM Symposium on Principles of Database Systems | conference | conf/pods | dblp, openalex, crossref |
| tods | ACM Transactions on Database Systems | journal | journals/tods | dblp, openalex, crossref |
| tdsc | IEEE Transactions on Dependable and Secure Computing | journal | journals/tdsc | dblp, openalex, crossref |
| tifs | IEEE Transactions on Information Forensics and Security | journal | journals/tifs | dblp, openalex, crossref |
| comnet | Computer Networks | journal | journals/cn | dblp, openalex, crossref |
| cose | Computers & Security | journal | journals/cose | dblp, openalex, crossref |
| jisa | Journal of Information Security and Applications | journal | journals/ias | dblp, openalex, crossref |
| arxiv | arXiv | preprint | 未配置 | arxiv |

## 自定义配置示例

在页面“管理领域与会议”新建期刊时可填写以下 JSON；标识仅为格式示例，不代表期刊真实存在：

```json
{
  "venue_id": "example_journal",
  "name": "Example Journal",
  "abbreviation": "EJ",
  "aliases": [
    "Example Journal"
  ],
  "venue_type": "journal",
  "research_domains": [
    "robotics"
  ],
  "dblp_key": null,
  "dblp_url": null,
  "openalex_source_ids": [],
  "issn": [],
  "isbn": [],
  "official_url": null,
  "supported_years": [],
  "adapters": [
    "openalex",
    "crossref"
  ],
  "ranking_tags": [],
  "capability_status": "configured",
  "enabled": true
}
```

Publisher 配置需要额外的 `publisher: {"catalog_url": "https://期刊官网/公开目录", "article_path": "/article/"}`，并将 `publisher` 加入 adapters。必须用真实公开路径并遵循 robots.txt；未配置时显式报不支持。该例不会自动启动爬取。

DBLP key 支持 conf/sp、journals/jmlr 及官方目录/stream URL；未知目录有显式 unsupported 诊断。来源接口细节见 [DBLP 官方API文档](https://dblp.org/faq/How%2Bto%2Buse%2Bthe%2Bdblp%2Bsearch%2BAPI.html)。
