#!/usr/bin/env python3
"""
碳治理文献日报 · 四级漏斗精准筛选系统 v4.0
============================================================
第1级：中文+国际双通道海选（Crossref + OpenAlex，零成本公开元数据）
第2级：多维打分初筛（严格10-15篇，划定 AI token 使用边界）
第3级：硅基流动 AI 分级精读（OA文献深度解析，非OA仅摘要）
第4级：综合得分终选（4篇，语言与OA状态不额外加减分）

核心原则：
- OA中立：OA状态仅影响精读方式，不参与任何打分排序
- 分区保底：高分区非OA文献优先级永远高于低分区OA文献
- Token边界：付费token严格限定在初筛12篇范围内使用
- 容错优先：单条异常直接跳过，绝不中断整体流程
============================================================
"""

import os, json, re, sys, time, datetime, pathlib, unicodedata, io, ipaddress, socket
import requests
from typing import List, Dict
from urllib.parse import urlparse

# ── 全局配置（对齐四级漏斗规则文档） ─────────────────────────
CONFIG = {
    "pool_size":   80,    # 第1级：海选候选池规模
    "screen_num":  12,    # 第2级：初筛入围数量（10-15区间，付费token唯一边界）
    "final_num":   4,     # 第4级：终选推荐数量
    "partition_standard": "新锐分区", # 唯一分区判定标准
    # 打分权重（OA状态完全不参与，保证客观中立）
    "weight": {
        "relevance":        0.40,  # 主题契合度（第一优先级）
        "journal_level":    0.20,  # 期刊新锐分区等级
        "implementability": 0.15,  # 方法可实现性（AI辅助可完成）
        "citation":         0.15,  # 学术影响力/引用量
        "recency":          0.10,  # 发表时效性
    }
}

# 分区等级优先级映射（终选排序用，数值越大优先级越高）
PARTITION_PRIORITY = {
    "Top": 5, "一区": 4, "二区": 3, "三区": 2, "四区": 1,
    "分区冲突待核验": 0, "未匹配分区": 0,
}

# ── 2026 新锐分区表（由用户提供的 Excel 全量导入） ───────────────
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
PARTITION_FILE = PROJECT_ROOT / "journal_partitions_2026.json"


def normalize_journal_name(value: str) -> str:
    """规范化刊名，兼容大小写、标点、全半角与 &/and 差异。"""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("&", " and ")
    return " ".join(re.findall(r"[^\W_]+", text, flags=re.UNICODE))


def normalize_issn(value: str) -> str:
    text = re.sub(r"[^0-9Xx]", "", str(value or "")).upper()
    return text if len(text) == 8 else ""


def load_partition_table() -> Dict:
    if not PARTITION_FILE.exists():
        print(f"  [分区表缺失] {PARTITION_FILE}")
        return {"by_name": {}, "by_issn": {}}
    payload = json.loads(PARTITION_FILE.read_text(encoding="utf-8"))
    by_name, by_issn = {}, {}
    for record in payload.get("journals", []):
        name_key = record.get("normalized_name") or normalize_journal_name(record.get("name", ""))
        if name_key:
            by_name.setdefault(name_key, []).append(record)
        for issn in record.get("issn", []):
            issn_key = normalize_issn(issn)
            if issn_key:
                by_issn.setdefault(issn_key, []).append(record)
    print(
        f"  📊 已加载2026新锐分区表：{payload.get('row_count', 0)}条，"
        f"刊名冲突{payload.get('name_conflict_count', 0)}组"
    )
    return {"by_name": by_name, "by_issn": by_issn}


PARTITION_INDEX = load_partition_table()


def lookup_partition(journal: str, issns: List[str]) -> Dict:
    name_key = normalize_journal_name(journal)
    name_matches = PARTITION_INDEX["by_name"].get(name_key, [])

    # 刊名唯一时以刊名为准；表内个别 ISSN 存在质量问题，不反向覆盖。
    if name_matches:
        levels = {record.get("level", "未匹配分区") for record in name_matches}
        if len(levels) == 1:
            record = name_matches[0]
            return {"level": levels.pop(), "match": "normalized_name", "record": record}

        # 同名且分区冲突时，仅用 ISSN 做二次消歧。
        issn_keys = {normalize_issn(value) for value in (issns or []) if normalize_issn(value)}
        narrowed = [
            record for record in name_matches
            if issn_keys.intersection({normalize_issn(value) for value in record.get("issn", [])})
        ]
        narrowed_levels = {record.get("level", "未匹配分区") for record in narrowed}
        if narrowed and len(narrowed_levels) == 1:
            return {"level": narrowed_levels.pop(), "match": "name_and_issn", "record": narrowed[0]}
        return {"level": "分区冲突待核验", "match": "name_conflict", "record": {}}

    # 刊名未命中时才尝试 ISSN，且只接受唯一分区。
    issn_matches = []
    for value in issns or []:
        issn_matches.extend(PARTITION_INDEX["by_issn"].get(normalize_issn(value), []))
    levels = {record.get("level", "未匹配分区") for record in issn_matches}
    if issn_matches and len(levels) == 1:
        return {"level": levels.pop(), "match": "issn", "record": issn_matches[0]}
    return {"level": "未匹配分区", "match": "none", "record": {}}

# ── 独立的国际/中文检索路由（避免长串混合查询导致语言偏颇） ───────────
INTERNATIONAL_QUERIES = [
    "Beijing Tianjin Hebei carbon emission coordinated development difference-in-differences policy",
    "embodied carbon MRIO multi-regional input-output inter-regional transfer China responsibility",
    "forest carbon sink CCER voluntary carbon market China ecological compensation incentive",
    "low-carbon governance industrial upgrading green innovation urban agglomeration China",
]
CHINESE_QUERIES = [
    "京津冀碳排放区域协调发展政策效应准实验",
    "京津冀 隐含碳 投入产出 责任分担 区域间转移",
    "林业碳汇CCER价值实现生态保护激励机制",
    "低碳治理 产业结构升级 绿色创新 城市群",
]
RESEARCH_QUERY_GROUPS = {
    "international": INTERNATIONAL_QUERIES,
    "zh": CHINESE_QUERIES,
}
COMBINED_KW = " ".join(INTERNATIONAL_QUERIES + CHINESE_QUERIES)
RETRIEVAL_STATS: List[Dict] = []

# ── 时间 ──────────────────────────────────────────────────────
TODAY  = datetime.date.today().strftime("%Y-%m-%d")
NOW_CN = datetime.datetime.now(
    datetime.timezone(datetime.timedelta(hours=8))
).strftime("%Y年%m月%d日 %H:%M")
DATA_DIR = pathlib.Path("data")
DATA_DIR.mkdir(exist_ok=True)

# ── 硅基流动 API（第3级精读用） ───────────────────────────────
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
GEMINI_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "finding_zh": {"type": "string", "description": "2-3句中文核心发现或创新"},
        "reference_value_zh": {"type": "string", "description": "对博士论文具体环节的中文可操作参考"},
        "method_zh": {"type": "string", "description": "中文说明研究方法与数据，未知则如实说明"},
        "limitations_zh": {"type": "string", "description": "中文说明阅读依据及待核验事项"},
    },
    "required": ["finding_zh", "reference_value_zh", "method_zh", "limitations_zh"],
}
SF_KEY   = os.environ.get("SILICONFLOW_API_KEY", "")
SF_URL   = "https://api.siliconflow.cn/v1/chat/completions"
SF_MODEL = "Qwen/Qwen2.5-72B-Instruct"
AI_PROVIDER = "Gemini" if GEMINI_KEY else ("SiliconFlow" if SF_KEY else "deterministic-fallback")
WORKFLOW_URL = "https://github.com/HORACE0919/carbon-lit-final/actions/workflows/daily.yml"
MAX_FULLTEXT_BYTES = 15 * 1024 * 1024
MAX_FULLTEXT_CHARS = 12000


# ════════════════════════════════════════════════════════════════
# 工具函数1：OA状态识别（仅标注，严格不参与排序打分）
# ════════════════════════════════════════════════════════════════
def check_oa_status(item: Dict) -> Dict:
    """
    保守识别开放获取状态。Crossref 的 license/link 并不都代表 OA：
    出版商常会提供仅用于文本与数据挖掘（TDM）的许可和 PDF 链接。
    只把明确的 Creative Commons/公共领域许可视为“OA 已核验”。
    严格OA中立：不纳入打分排序，不影响推荐优先级
    """
    oa_license_markers = (
        "creativecommons.org/licenses/",
        "creativecommons.org/publicdomain/",
    )
    license_urls = [
        str(lic.get("URL", "")).strip().lower()
        for lic in (item.get("license", []) or [])
    ]
    verified_license = next(
        (url for url in license_urls
         if any(marker in url for marker in oa_license_markers)),
        "",
    )
    is_oa = bool(verified_license)

    fulltext_url = ""
    if is_oa:
        fulltext_url = next(
            (
                str(lk.get("URL", "")).strip()
                for lk in (item.get("link", []) or [])
                if lk.get("content-type") == "application/pdf"
                and str(lk.get("intended-application", "")).lower()
                not in {"text-mining", "similarity-checking"}
            ),
            "",
        )

    return {
        "oa_verified":    is_oa,
        "oa_status":      "OA已核验-Creative Commons许可" if is_oa else "需机构权限或OA待核验",
        "oa_evidence":    verified_license,
        "fulltext_url":   fulltext_url,
        "auto_fulltext_eligible": bool(is_oa and fulltext_url),
        "token_required": bool(is_oa and fulltext_url),  # 兼容旧数据字段
    }


# ════════════════════════════════════════════════════════════════
# 工具函数2：方法可实现性评估（匹配AI辅助实证场景）
# ════════════════════════════════════════════════════════════════
def calculate_implementability_score(abstract: str) -> float:
    """
    基于摘要评估实证方法可实现性
    主流计量/统计方法得分高（Python/Stata+AI可快速复现）
    小众或工程门槛极高的方法得分低
    """
    score = 0.5
    positive_keywords = [
        "面板回归", "双重差分", "DID", "倾向得分匹配", "PSM",
        "固定效应", "随机效应", "OLS", "中介效应", "调节效应",
        "计量模型", "统计分析", "面板数据", "公开数据",
        "回归分析", "实证分析", "描述性统计", "稳健性检验",
        "difference-in-differences", "panel data", "fixed effect",
        "propensity score", "input-output", "MRIO", "regression",
        "instrumental variable", "synthetic control",
    ]
    negative_keywords = [
        "深度学习", "神经网络", "复杂系统仿真", "多主体建模",
        "大规模野外实验", "一手调研", "高维机器学习", "贝叶斯复杂模型",
        "deep learning", "neural network", "agent-based",
    ]
    txt = abstract.lower()
    for kw in positive_keywords:
        if kw.lower() in txt:
            score += 0.12
    for kw in negative_keywords:
        if kw.lower() in txt:
            score -= 0.20
    return round(max(0.0, min(1.0, score)), 3)


# ════════════════════════════════════════════════════════════════
# 第1级：中文+国际双通道免费海选（Crossref + OpenAlex）
# ════════════════════════════════════════════════════════════════
def detect_language_group(title: str, abstract: str = "") -> str:
    text = f"{title} {abstract}"
    return "zh" if len(re.findall(r"[\u3400-\u9fff]", text)) >= 2 else "international"


def normalize_doi(value: str) -> str:
    return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(value or ""), flags=re.I).strip().lower()


def openalex_abstract(inverted_index: Dict) -> str:
    positioned = []
    for word, positions in (inverted_index or {}).items():
        for position in positions or []:
            positioned.append((position, word))
    return " ".join(word for _, word in sorted(positioned))


def fetch_crossref_batch(keyword: str, query_group: str) -> List[Dict]:
    """Crossref 检索；批次失败会被记录，不伪装成成功。"""
    params = {
        "query.bibliographic": keyword,
        "rows":  CONFIG["pool_size"],
        "sort":  "relevance",
        "order": "desc",
        "filter": "from-pub-date:2022-01-01",
        "select": (
            "title,author,issued,container-title,DOI,URL,"
            "abstract,is-referenced-by-count,ISSN,license,link"
        ),
        "mailto": "carbon-lit-bot@github.io",
    }
    try:
        resp = requests.get(
            "https://api.crossref.org/works",
            params=params, timeout=25
        )
        resp.raise_for_status()
        items = resp.json()["message"]["items"]
    except Exception as e:
        RETRIEVAL_STATS.append({"source": "Crossref", "group": query_group, "query": keyword, "ok": False, "count": 0, "error": str(e)})
        print(f"  [Crossref异常] {e}")
        return []

    pool = []
    for item in items:
        try:
            title   = item.get("title", [""])[0].strip()
            journal = item.get("container-title", ["未知期刊"])[0].strip()
            abstract_raw = item.get("abstract", "")
            abstract = re.sub(r"<[^>]+>", "", abstract_raw).strip()[:1600]
            year_parts = item.get("issued", {}).get("date-parts", [[None]])[0]
            year = year_parts[0] if year_parts else None
            oa   = check_oa_status(item)
            impl = calculate_implementability_score(abstract)
            issns = item.get("ISSN", []) or []
            partition = lookup_partition(journal, issns)
            pool.append({
                "title":          title,
                "authors":        [
                    f"{a.get('given','')} {a.get('family','')}".strip()
                    for a in item.get("author", [])
                ],
                "year":           year,
                "journal":        journal,
                "partition":      partition["level"],
                "partition_match": partition["match"],
                "partition_subject": partition["record"].get("subject", ""),
                "doi":            normalize_doi(item.get("DOI", "")),
                "issn":           issns,
                "official_url":   item.get("URL", ""),
                "fulltext_url":   oa["fulltext_url"],
                "oa_verified":    oa["oa_verified"],
                "oa_status":      oa["oa_status"],
                "oa_evidence":    oa["oa_evidence"],
                "auto_fulltext_eligible": oa["auto_fulltext_eligible"],
                "token_required": oa["token_required"],
                "citation":       item.get("is-referenced-by-count", 0),
                "abstract":       abstract,
                "language_group": detect_language_group(title, abstract),
                "query_group":    query_group,
                "sources":        ["Crossref"],
                "implementability_score": impl,
            })
        except Exception:
            continue  # 单条解析失败直接跳过
    RETRIEVAL_STATS.append({"source": "Crossref", "group": query_group, "query": keyword, "ok": True, "count": len(pool), "error": ""})
    return pool


def fetch_openalex_batch(keyword: str, query_group: str) -> List[Dict]:
    """OpenAlex 是独立元数据补充源；失败时 Crossref 通道仍可继续。"""
    params = {
        "search": keyword,
        "filter": "from_publication_date:2022-01-01",
        "per-page": min(CONFIG["pool_size"], 100),
        "mailto": "carbon-lit-bot@github.io",
    }
    try:
        resp = requests.get(
            "https://api.openalex.org/works",
            params=params,
            headers={"User-Agent": "carbon-literature-daily/4.0 (mailto:carbon-lit-bot@github.io)"},
            timeout=25,
        )
        resp.raise_for_status()
        items = resp.json().get("results", [])
    except Exception as e:
        RETRIEVAL_STATS.append({"source": "OpenAlex", "group": query_group, "query": keyword, "ok": False, "count": 0, "error": str(e)})
        print(f"  [OpenAlex异常] {e}")
        return []

    pool = []
    for item in items:
        try:
            title = str(item.get("title") or "").strip()
            primary = item.get("primary_location") or {}
            source = primary.get("source") or {}
            journal = str(source.get("display_name") or "未知期刊").strip()
            abstract = openalex_abstract(item.get("abstract_inverted_index") or {})[:1600]
            issns = source.get("issn") or ([source.get("issn_l")] if source.get("issn_l") else [])
            partition = lookup_partition(journal, issns)
            open_access = item.get("open_access") or {}
            best_oa = item.get("best_oa_location") or {}
            public_pdf = str(best_oa.get("pdf_url") or "").strip()
            is_oa = bool(open_access.get("is_oa"))
            evidence = f"OpenAlex:{open_access.get('oa_status', 'unknown')}" if is_oa else ""
            authors = [
                str((entry.get("author") or {}).get("display_name") or "").strip()
                for entry in (item.get("authorships") or [])
                if (entry.get("author") or {}).get("display_name")
            ]
            pool.append({
                "title": title,
                "authors": authors,
                "year": item.get("publication_year"),
                "journal": journal,
                "partition": partition["level"],
                "partition_match": partition["match"],
                "partition_subject": partition["record"].get("subject", ""),
                "doi": normalize_doi(item.get("doi", "")),
                "issn": issns,
                "official_url": item.get("doi") or item.get("id", ""),
                "fulltext_url": public_pdf,
                "oa_verified": is_oa,
                "oa_status": "OA已核验-OpenAlex" if is_oa else "需机构权限或OA待核验",
                "oa_evidence": evidence,
                "auto_fulltext_eligible": bool(is_oa and public_pdf),
                "token_required": bool(is_oa and public_pdf),
                "citation": item.get("cited_by_count", 0),
                "abstract": abstract,
                "language_group": detect_language_group(title, abstract),
                "query_group": query_group,
                "sources": ["OpenAlex"],
                "implementability_score": calculate_implementability_score(abstract),
            })
        except Exception:
            continue
    RETRIEVAL_STATS.append({"source": "OpenAlex", "group": query_group, "query": keyword, "ok": True, "count": len(pool), "error": ""})
    return pool


def fetch_global_literature_pool() -> List[Dict]:
    """
    中文与国际查询独立执行，再跨 Crossref/OpenAlex 去重合并。
    全程不读全文、不消耗付费token、不受付费墙影响
    """
    print("【第1级】中文+国际双通道免费海选…")
    RETRIEVAL_STATS.clear()
    merged = {}
    for group, queries in RESEARCH_QUERY_GROUPS.items():
        print(f"  ── {'中文' if group == 'zh' else '国际'}检索组 ──")
        for keyword in queries:
            print(f"  🔍 {keyword[:55]}…")
            for batch in (
                fetch_crossref_batch(keyword, group),
                fetch_openalex_batch(keyword, group),
            ):
                for paper in batch:
                    if not paper.get("title"):
                        continue
                    identity = (
                        f"doi:{paper['doi']}" if paper.get("doi")
                        else f"title:{normalize_journal_name(paper['title'])}"
                    )
                    if identity not in merged:
                        merged[identity] = paper
                        continue
                    existing = merged[identity]
                    existing["sources"] = sorted(set(existing.get("sources", []) + paper.get("sources", [])))
                    if len(paper.get("abstract", "")) > len(existing.get("abstract", "")):
                        existing["abstract"] = paper["abstract"]
                    if not existing.get("fulltext_url") and paper.get("fulltext_url"):
                        for key in (
                            "fulltext_url", "oa_verified", "oa_status", "oa_evidence",
                            "auto_fulltext_eligible", "token_required",
                        ):
                            existing[key] = paper.get(key)
            time.sleep(0.4)

    successes = sum(1 for stat in RETRIEVAL_STATS if stat["ok"])
    if successes == 0:
        raise RuntimeError("所有元数据通道均失败，不生成伪成功日报")
    all_papers = list(merged.values())
    zh_count = sum(1 for paper in all_papers if paper.get("language_group") == "zh")
    intl_count = len(all_papers) - zh_count
    print(
        f"✅ 海选完成：去重后{len(all_papers)}篇，"
        f"中文{zh_count}篇，国际{intl_count}篇；"
        f"成功批次{successes}/{len(RETRIEVAL_STATS)}"
    )
    if zh_count == 0 or intl_count == 0:
        print("  ⚠️ 语种覆盖不完整：保留警告，不用无关文献硬填配额")
    return all_papers


# ════════════════════════════════════════════════════════════════
# 第2级：多维打分初筛（划定付费token使用边界）
# ════════════════════════════════════════════════════════════════
def calculate_score(lit: Dict) -> float:
    """
    多维度综合打分，OA状态完全不参与计算，保证客观中立
    打分仅基于：主题相关性、期刊分区、方法可实现性、引用量、时效性
    """
    w    = CONFIG["weight"]
    kws  = COMBINED_KW.lower().split()

    # 1. 主题相关性（第一优先级）
    title_hit    = sum(1 for k in kws if k in lit["title"].lower())
    abstract_hit = sum(1 for k in kws if k in lit["abstract"].lower())
    relevance    = (title_hit * 2 + abstract_hit) / max(len(kws) * 3, 1)

    # 2. 期刊新锐分区（唯一分区标准）
    partition_rank = {"Top":1.0, "一区":0.8, "二区":0.6, "三区":0.4, "四区":0.2, "未匹配分区":0.25}
    journal_score  = partition_rank.get(lit["partition"], 0.25)

    # 3. 方法可实现性（AI辅助可完成优先）
    impl = lit["implementability_score"]

    # 4. 引用量（归一化）
    citation = min(lit["citation"] / 500, 1.0)

    # 5. 时效性（近3-5年优先）
    year     = lit["year"] or 2015
    recency  = max(0.0, 1.0 - (2026 - year) / 10)

    lit["relevance_score"] = round(relevance, 4)
    return round(
        relevance     * w["relevance"] +
        journal_score * w["journal_level"] +
        impl          * w["implementability"] +
        citation      * w["citation"] +
        recency       * w["recency"],
        4
    )


def screen_candidates(pool: List[Dict]) -> List[Dict]:
    """
    初筛：输出严格10-15篇候选，此为付费token的唯一使用范围边界
    筛选全程不考虑OA状态，仅按综合质量排序，保证客观中立
    """
    if not pool:
        return []
    for p in pool:
        p["total_score"] = calculate_score(p)
    ranked = sorted(pool, key=lambda x: x["total_score"], reverse=True)
    candidates = ranked[:CONFIG["screen_num"]]
    oa_count   = sum(1 for c in candidates if c.get("oa_verified"))
    auto_count = sum(1 for c in candidates if c["token_required"])
    print(f"\n【第2级】多维打分初筛完成：")
    print(f"  ✅ 共选出 {len(candidates)} 篇（付费token仅在此范围内使用）")
    print(f"  🔓 OA已核验 {oa_count} 篇，其中 {auto_count} 篇具有可自动读取全文")
    print(f"  🔒 需机构权限或OA待核验 {len(candidates)-oa_count} 篇")
    zh_count = sum(1 for c in candidates if c.get("language_group") == "zh")
    print(f"  🌏 语种覆盖：中文 {zh_count} 篇，国际 {len(candidates)-zh_count} 篇")
    print("  ℹ️  中英文无硬性配额，统一按综合得分入选")
    print(f"  ℹ️  排序严格按综合质量，未因OA状态倾斜")
    return candidates


# ════════════════════════════════════════════════════════════════
# 第3级：分级精读（付费token严格限定在初筛范围内）
# ════════════════════════════════════════════════════════════════
def validate_public_url(url: str) -> str:
    """拒绝本地/私有网络地址，避免将外部元数据链接变成 SSRF 入口。"""
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("全文URL无效")
    addresses = {
        entry[4][0]
        for entry in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    }
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("全文URL指向非公网地址")
    return parsed.geturl()


def fetch_public_pdf_text(lit: Dict) -> Dict:
    """仅对已核验 OA 且有公开PDF链接的文献尝试自动取文。"""
    if not lit.get("oa_verified") or not lit.get("fulltext_url"):
        return {"text": "", "error": "无已核验的公开PDF链接"}
    response = None
    try:
        url = validate_public_url(lit["fulltext_url"])
        response = requests.get(
            url,
            headers={"User-Agent": "carbon-literature-daily/4.0"},
            timeout=35,
            stream=True,
            allow_redirects=True,
        )
        response.raise_for_status()
        content_length = int(response.headers.get("Content-Length", 0) or 0)
        if content_length > MAX_FULLTEXT_BYTES:
            raise ValueError(f"PDF超过{MAX_FULLTEXT_BYTES // 1024 // 1024}MB限制")
        chunks, size = [], 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > MAX_FULLTEXT_BYTES:
                raise ValueError(f"PDF超过{MAX_FULLTEXT_BYTES // 1024 // 1024}MB限制")
            chunks.append(chunk)
        content = b"".join(chunks)
        content_type = response.headers.get("Content-Type", "").lower()
        if not content.startswith(b"%PDF") and "application/pdf" not in content_type:
            raise ValueError("公开链接返回的不是PDF")
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        page_text = []
        for page in reader.pages[:20]:
            text = page.extract_text() or ""
            if text:
                page_text.append(text)
            if sum(len(value) for value in page_text) >= MAX_FULLTEXT_CHARS:
                break
        cleaned = re.sub(r"\s+", " ", " ".join(page_text)).strip()[:MAX_FULLTEXT_CHARS]
        if len(cleaned) < 800:
            raise ValueError("PDF可读文字不足800字符")
        return {"text": cleaned, "error": ""}
    except Exception as exc:
        return {"text": "", "error": str(exc)[:300]}
    finally:
        if response is not None:
            response.close()


def fallback_analysis(lit: Dict, source_text: str, basis_label: str, error: str = "") -> Dict:
    """无Key或API失败时的非空、可追溯降级结果，不冒充AI结论。"""
    clean = re.sub(r"\s+", " ", source_text or "").strip()
    finding = clean[:320] if clean else "公开元数据未提供摘要，当前无法可靠概括研究结论。"
    text = f"{lit.get('title', '')} {clean}".lower()
    if any(term in text for term in ["mrio", "input-output", "embodied carbon", "隐含碳", "投入产出"]):
        reference = "主要服务论文二：可对照其碳流边界、MRIO部门聚合与责任分担口径。"
    elif any(term in text for term in ["forest", "ccer", "carbon sink", "林业碳汇", "生态补偿"]):
        reference = "主要服务论文三：可对照CCER制度边界、价值实现链条与激励机制。"
    else:
        reference = "主要服务论文一：可对照政策识别、减排机制与异质性分析设计。"
    return {
        "finding_zh": finding,
        "reference_value_zh": reference,
        "method_zh": "需根据全文或完整摘要进一步核验",
        "limitations_zh": f"当前仅使用{basis_label}；" + (f"AI调用失败：{error[:120]}" if error else "未配置可用AI Key"),
        "analysis_provider": "deterministic-fallback",
    }


def parse_ai_json(text: str) -> Dict:
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("AI未返回JSON对象")
    payload = json.loads(cleaned[start:end + 1])
    required = ("finding_zh", "reference_value_zh", "method_zh", "limitations_zh")
    if any(not str(payload.get(key, "")).strip() for key in required):
        raise ValueError("AI JSON缺少必填字段")
    return {key: str(payload[key]).strip() for key in required}


def ai_deepread(lit: Dict, source_text: str, basis_label: str) -> Dict:
    """优先调用 Gemini，次选硅基流动；返回统一结构化结果。"""
    prompt = f"""你是严谨的学术文献分析助手，服务于研究区域低碳治理与林业碳汇的农林经济管理博士生。

三篇论文方向：
1. 京津冀区域协调发展政策的碳减排效应（DID）
2. 京津冀与全国隐含碳责任分担（MRIO）
3. 林业碳汇CCER价值实现与生态保护激励机制

仅根据「{basis_label}」分析。若是摘要，不得声称已阅读全文，不得臆造结果。
只返回JSON：
{{
  "finding_zh": "2-3句核心发现/创新，无背景套话",
  "reference_value_zh": "明确对哪篇论文的什么环节有何可操作参考",
  "method_zh": "研究方法与数据；原文未提供则如实说明",
  "limitations_zh": "阅读依据及需要进一步核验的点"
}}

标题：{lit['title']}
期刊：{lit.get('journal', '')}
内容：{source_text}"""

    if not GEMINI_KEY and not SF_KEY:
        return fallback_analysis(lit, source_text, basis_label)
    try:
        if GEMINI_KEY:
            response = requests.post(
                GEMINI_URL,
                headers={"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "temperature": 0.2,
                        "maxOutputTokens": 2048,
                        "responseFormat": {
                            "text": {
                                "mimeType": "application/json",
                                "schema": GEMINI_RESPONSE_SCHEMA,
                            }
                        },
                    },
                },
                timeout=75,
            )
            response.raise_for_status()
            text = "".join(
                part.get("text", "")
                for part in response.json()["candidates"][0]["content"]["parts"]
            )
            provider = f"Gemini/{GEMINI_MODEL}"
        else:
            response = requests.post(
                SF_URL,
                headers={"Authorization": f"Bearer {SF_KEY}", "Content-Type": "application/json"},
                json={
                    "model": SF_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.2,
                    "max_tokens": 700,
                    "response_format": {"type": "json_object"},
                },
                timeout=75,
            )
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"]
            provider = f"SiliconFlow/{SF_MODEL}"
        result = parse_ai_json(text)
        result["analysis_provider"] = provider
        return result
    except Exception as exc:
        print(f"  [AI解读异常，已使用规则降级] {type(exc).__name__}: {str(exc)[:180]}")
        return fallback_analysis(lit, source_text, basis_label, f"{type(exc).__name__}: {str(exc)}")


def deepread_all(candidates: List[Dict]) -> List[Dict]:
    """
    仅对已核验 OA 公开PDF尝试全文取文；其余文献仅分析公开摘要。
    AI token 始终严格限定在初筛12篇以内，OA不决定推荐优先级。
    """
    print(f"\n【第3级】合法全文/摘要分层解读…")
    for i, lit in enumerate(candidates):
        fetched = fetch_public_pdf_text(lit)
        if fetched["text"]:
            source_text = fetched["text"]
            lit["access_mode"] = "verified_public_fulltext"
            lit["analysis_basis"] = "public_fulltext"
            lit["analysis_basis_label"] = "已核验公开全文"
            print(f"  [{i+1}/{len(candidates)}] 📖 公开全文解读：{lit['title'][:50]}…")
        else:
            source_text = lit.get("abstract", "")
            lit["access_mode"] = (
                "oa_without_machine_readable_fulltext" if lit.get("oa_verified")
                else "institutional_or_unknown"
            )
            lit["analysis_basis"] = "abstract" if source_text else "metadata_only"
            lit["analysis_basis_label"] = "公开摘要" if source_text else "仅元数据"
            print(f"  [{i+1}/{len(candidates)}] 📄 {lit['analysis_basis_label']}解读：{lit['title'][:50]}…")
        lit["fulltext_fetch_error"] = fetched["error"]
        lit["fulltext_chars"] = len(fetched["text"])
        lit["manual_fulltext_recommended"] = lit["analysis_basis"] != "public_fulltext"
        analysis = ai_deepread(lit, source_text, lit["analysis_basis_label"])
        lit.update(analysis)
        lit["summary_zh"] = (
            f"主要发现：{analysis['finding_zh']} "
            f"参考价值：{analysis['reference_value_zh']}"
        )
        if GEMINI_KEY or SF_KEY:
            time.sleep(0.8)
    return candidates


# ════════════════════════════════════════════════════════════════
# 第4级：终选推荐（综合得分排序，语言/OA不额外加减分）
# ════════════════════════════════════════════════════════════════
def generate_personalized_reason(lit: Dict) -> str:
    """
    生成差异化推荐理由，每篇突出独有核心优势
    禁止笼统表述，必须结合发表价值与落地可行性展开
    包含方法可实现性评估
    """
    parts = []

    # 分区与发表标杆价值
    if lit["partition"] in ("Top", "一区"):
        parts.append(
            f"新锐分区{lit['partition']}顶刊，研究范式与写作规范具备强发表参考价值，"
            f"可直接对标目标期刊的选题深度与论证逻辑"
        )

    # 学术认可度（引用量）
    if lit["citation"] > 200:
        parts.append(
            f"累计引用{lit['citation']}次，研究框架已获业内广泛验证，"
            f"结论可靠性高，适合作为本研究的理论基准"
        )
    elif lit["citation"] > 50:
        parts.append(f"引用{lit['citation']}次，研究认可度较好，方法设计具有一定参考价值")

    # 方法可实现性
    impl = lit.get("implementability_score", 0.5)
    if impl >= 0.7:
        parts.append(
            "实证方法成熟通用（面板回归/DID/MRIO等），"
            "可借助Python/Stata配合AI工具快速复现，落地门槛低"
        )
    elif impl >= 0.5:
        parts.append(
            "研究方法常规可控，核心实证环节可通过AI辅助完成代码与分析"
        )

    # 前沿选题价值
    if lit.get("year") and lit["year"] >= 2023:
        parts.append(
            f"{lit['year']}年最新发表，选题紧跟研究前沿，"
            f"创新切入点与研究缺口可直接借鉴延伸"
        )

    # 非OA文献补充提示（不因非OA而弱化推荐）
    if "需机构权限" in lit.get("oa_status", ""):
        parts.append(
            "建议校园网下载全文后深度精读，可进一步拆解实证细节与数据处理逻辑"
        )

    if not parts:
        parts.append("研究主题高度契合，核心问题与数据场景可直接延伸拓展")

    return "；".join(parts[:2])


def final_select(candidates: List[Dict]) -> List[Dict]:
    """
    终选严格按综合得分排序。分区已经是综合得分的一个组成部分，
    不再进行分区二次优先；语言与OA状态均不额外加减分。
    """
    ranked = sorted(candidates, key=lambda x: x["total_score"], reverse=True)
    final = ranked[:CONFIG["final_num"]]
    print(f"\n【第4级】综合得分终选完成：{len(final)} 篇")
    for i, p in enumerate(final):
        oa_tag = "🔓OA已核验" if p.get("oa_verified") else "🔒需权限/OA待核验"
        print(f"  [{i+1}] [{p['partition']}]{oa_tag} {p['title'][:55]}…")
    zh_count = sum(1 for p in final if p.get("language_group") == "zh")
    print(f"  🌏 终选语种：中文 {zh_count} 篇，国际 {len(final)-zh_count} 篇")
    print(f"  ℹ️  排序严格按综合得分，语言与OA均不额外加减分")
    return final


# ════════════════════════════════════════════════════════════════
# HTML 生成
# ════════════════════════════════════════════════════════════════
TOPIC_LABEL = {
    "policy":     "政策评估",
    "mrio":       "隐含碳",
    "forest":     "林业碳汇",
    "governance": "碳市场",
}

def guess_topics(lit: Dict) -> List[str]:
    txt = (lit["title"] + " " + lit["abstract"]).lower()
    topics = []
    if any(k in txt for k in ["did","difference","coordinated","policy","urban carbon","beijing-tianjin","bth"]):
        topics.append("policy")
    if any(k in txt for k in ["mrio","embodied","input-output","supply chain","inter-regional","multiregional"]):
        topics.append("mrio")
    if any(k in txt for k in ["forest","carbon sink","ccer","sequestration","forestry","timber"]):
        topics.append("forest")
    if any(k in txt for k in ["carbon market","ets","trading","governance","carbon price","emission trading"]):
        topics.append("governance")
    return topics or ["policy"]

def render_card(lit: Dict, idx: int, is_top: bool = False) -> str:
    topics   = guess_topics(lit)
    score_n  = min(int(lit.get("total_score", 0) * 10), 5)
    dots     = "".join(f'<span class="dot {"on" if i < score_n else ""}"></span>' for i in range(5))
    ttags    = "".join(f'<span class="chip ct">{TOPIC_LABEL.get(t, t)}</span>' for t in topics)
    doi      = lit.get("doi", "")
    doi_href = f'https://doi.org/{doi}' if doi and not doi.startswith("http") else doi
    dl       = f'<a class="cl" href="{doi_href}" target="_blank">🔗 原文</a>' if doi_href else ""
    public_pdf = lit.get("fulltext_url", "") if lit.get("analysis_basis") == "public_fulltext" else ""
    pdf_link = f'<a class="cl" href="{public_pdf}" target="_blank">📖 公开全文</a>' if public_pdf else ""
    gs_url   = f'https://scholar.google.com/scholar?q={requests.utils.quote(lit["title"])}'
    gs       = f'<a class="cl" href="{gs_url}" target="_blank">Google Scholar</a>'
    is_oa    = bool(lit.get("oa_verified", False))
    oa_badge = f'<span class="chip {"oa-open" if is_oa else "oa-lock"}">{"🔓 OA已核验" if is_oa else "🔒 需权限/OA待核验"}</span>'
    basis_label = lit.get("analysis_basis_label", "仅元数据")
    basis_badge = f'<span class="chip cd">🔎 {basis_label}</span>'
    part     = lit.get("partition", "未匹配分区")
    authors  = ", ".join(lit.get("authors", [])[:3]) + (" 等" if len(lit.get("authors", [])) > 3 else "")
    summary  = lit.get("summary_zh", "")
    method   = lit.get("method_zh", "")
    limits   = lit.get("limitations_zh", "")
    provider = lit.get("analysis_provider", "deterministic-fallback")
    reason   = generate_personalized_reason(lit)
    top_cls  = " top-pick" if is_top else ""
    return f"""
<div class="lc{top_cls}" data-topics="{",".join(topics)}">
  <div class="ch">
    <span class="ci">{str(idx).zfill(2)}</span>
    <div class="ct2">{lit["title"]}</div>
  </div>
  <div class="cm2">
    <span class="chip cj">{lit.get("journal","")}</span>
    <span class="chip cd">{lit.get("year","—")}</span>
    <span class="chip cp">{part}</span>
    {oa_badge}{basis_badge}{ttags}
  </div>
  <div class="ca">{authors}</div>
  <div class="csu">
    <div class="csl">主要发现 &amp; 参考价值</div>
    <div class="cst">{summary}</div>
    <div class="cst"><strong>方法：</strong>{method}<br><strong>局限：</strong>{limits}</div>
    <div class="csl">解读引擎：{provider}</div>
  </div>
  <div class="creason">
    <div class="crl">💡 推荐理由</div>
    <div class="crt">{reason}</div>
  </div>
  <div class="cft">
    {dl}{pdf_link}{gs}
    <div class="rb"><span class="rl2">综合得分</span><span class="dots">{dots}</span></div>
  </div>
</div>"""

def build_html(final: List[Dict], candidates: List[Dict]) -> str:
    final_html = "\n".join(render_card(p, i+1, True) for i, p in enumerate(final))
    cand_html  = "\n".join(render_card(p, i+1, False) for i, p in enumerate(candidates))
    oa_n       = sum(1 for p in candidates if p.get("oa_verified"))
    non_oa_n   = len(candidates) - oa_n
    providers  = sorted({p.get("analysis_provider", "deterministic-fallback") for p in candidates})
    provider_label = " + ".join(providers)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>碳治理文献日报 · {TODAY}</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@400;600&family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400&display=swap');
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --ink:#1a1a1a;--m:#555;--f:#999;--s:#fafaf8;--sa:#f0ede6;--b:#ddd9d0;
  --a:#2d5a27;--al:#e8f0e6;--aw:#8b6914;--awl:#fdf5e0;
  --bl:#1e3a5f;--blb:#e8f0f8;--gl:#1a4d1a;--glb:#e6f4e6;
  --r:4px;--rl:8px
}}
body{{font-family:'Inter',-apple-system,sans-serif;background:var(--s);color:var(--ink);font-size:14px;line-height:1.6;min-height:100vh}}
.mast{{border-bottom:2px solid var(--ink);padding:14px 28px 11px;display:flex;align-items:baseline;gap:16px;background:white;flex-wrap:wrap}}
.mast-t{{font-family:'Noto Serif SC',serif;font-size:20px;font-weight:600;letter-spacing:.04em}}
.mast-s{{font-size:11px;color:var(--f);letter-spacing:.05em;text-transform:uppercase;border-left:1px solid var(--b);padding-left:14px}}
.mast-d{{margin-left:auto;font-size:11px;color:var(--m);font-family:'JetBrains Mono',monospace}}
.ubar{{background:var(--a);color:white;padding:8px 28px;font-size:11px;display:flex;align-items:center;flex-wrap:wrap;gap:16px}}
.runbtn{{margin-left:auto;color:white;text-decoration:none;font-weight:600;border:1px solid rgba(255,255,255,.65);padding:3px 10px;border-radius:var(--r);white-space:nowrap}}
.runbtn:hover{{background:rgba(255,255,255,.14)}}
.tbar{{background:var(--ink);padding:0 28px;display:flex;overflow-x:auto}}
.tbar::-webkit-scrollbar{{height:0}}
.tbtn{{background:none;border:none;color:rgba(255,255,255,.45);font-size:11px;font-family:'Inter',sans-serif;font-weight:500;letter-spacing:.05em;text-transform:uppercase;padding:9px 14px;cursor:pointer;border-bottom:2px solid transparent;transition:color .15s;white-space:nowrap}}
.tbtn:hover{{color:white}}.tbtn.active{{color:white;border-bottom-color:#7cbf70}}
.wrap{{max-width:1100px;margin:0 auto;padding:22px 28px;display:grid;grid-template-columns:1fr 265px;gap:28px}}
.shdr{{display:flex;align-items:center;margin-bottom:14px;border-bottom:1px solid var(--b);padding-bottom:8px}}
.sl{{font-size:10px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--f)}}
.sc2{{font-size:10px;color:var(--f);margin-left:auto}}
.lc{{background:white;border:1px solid var(--b);border-radius:var(--rl);padding:16px 18px;margin-bottom:12px;transition:box-shadow .15s}}
.lc:hover{{box-shadow:0 2px 10px rgba(0,0,0,.06)}}
.lc.top-pick{{border-top:3px solid var(--a)}}
.ch{{display:flex;align-items:flex-start;gap:9px;margin-bottom:7px}}
.ci{{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--f);margin-top:2px;min-width:20px}}
.ct2{{font-size:14px;font-weight:600;line-height:1.4;flex:1}}
.cm2{{display:flex;flex-wrap:wrap;gap:4px;margin:6px 0 8px 29px}}
.chip{{font-size:10px;padding:2px 8px;border-radius:20px;font-weight:500}}
.cj{{background:var(--blb);color:var(--bl)}}.cd{{background:var(--sa);color:var(--m)}}
.ct{{background:var(--glb);color:var(--gl)}}
.cp{{background:#f3e8ff;color:#5b21b6;font-weight:600}}
.oa-open{{background:#dcfce7;color:#166534}}.oa-lock{{background:#fef9c3;color:#854d0e}}
.ca{{font-size:11px;color:var(--f);margin:0 0 9px 29px;font-style:italic}}
.csu{{margin:0 0 8px 29px;padding:10px 13px;background:var(--sa);border-left:3px solid var(--a);border-radius:0 var(--r) var(--r) 0}}
.csl{{font-size:9px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--a);margin-bottom:3px}}
.cst{{font-size:12px;color:var(--m);line-height:1.6}}
.creason{{margin:0 0 0 29px;padding:8px 13px;background:var(--awl);border-left:3px solid var(--aw);border-radius:0 var(--r) var(--r) 0}}
.crl{{font-size:9px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--aw);margin-bottom:3px}}
.crt{{font-size:12px;color:#5a3e0a;line-height:1.6}}
.cft{{margin:10px 0 0 29px;display:flex;gap:6px;align-items:center;flex-wrap:wrap}}
.cl{{font-size:10px;color:var(--a);text-decoration:none;font-weight:500;border:1px solid #c5dcc2;padding:2px 8px;border-radius:var(--r)}}
.cl:hover{{background:var(--al)}}
.rb{{margin-left:auto;display:flex;align-items:center;gap:4px}}
.rl2{{font-size:9px;color:var(--f)}}
.dots{{display:flex;gap:2px}}.dot{{width:5px;height:5px;border-radius:50%;background:var(--b)}}.dot.on{{background:var(--a)}}
.sdc{{background:white;border:1px solid var(--b);border-radius:var(--rl);padding:14px 15px;margin-bottom:12px}}
.sdt{{font-size:10px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--f);margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid var(--b)}}
.ti{{padding:7px 0;border-bottom:1px solid var(--sa)}}.ti:last-child{{border-bottom:none;padding-bottom:0}}
.tn{{font-size:9px;font-weight:600;color:var(--a);letter-spacing:.06em;margin-bottom:2px}}
.tnm{{font-size:11px;font-weight:500;line-height:1.35;margin-bottom:2px}}
.tm{{font-size:10px;color:var(--f)}}
.str{{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--sa);font-size:11px}}
.str:last-child{{border-bottom:none}}.stl{{color:var(--m)}}.stv{{font-weight:600}}
.fstep{{display:flex;align-items:center;gap:8px;padding:4px 0;font-size:11px;color:var(--m)}}
.fn{{font-weight:600;color:var(--ink);min-width:55px}}
.fb{{color:var(--a);font-weight:600;margin-left:auto}}
@media(max-width:800px){{.wrap{{grid-template-columns:1fr;padding:14px}}}}
</style>
</head>
<body>
<div class="mast">
  <div class="mast-t">碳治理文献日报</div>
  <div class="mast-s">区域低碳治理 · 林业碳汇 · 四级漏斗精选</div>
  <div class="mast-d">{TODAY}</div>
</div>
<div class="ubar">
  <span>✅ 已自动更新 · {NOW_CN}</span>
  <span>📊 海选→初筛{len(candidates)}篇→综合得分终选{len(final)}篇</span>
  <span>🔓 OA已核验 {oa_n} 篇 &nbsp;🔒 需权限/OA待核验 {non_oa_n} 篇</span>
  <span>⏰ 每天北京时间 07:00 自动刷新</span>
  <a class="runbtn" href="{WORKFLOW_URL}" target="_blank" rel="noopener" title="进入 GitHub Actions 后点击 Run workflow">⚡ 立即更新文献日报</a>
</div>
<div class="tbar">
  <button class="tbtn active" onclick="swT(this,'all','top')">⭐ 今日精选</button>
  <button class="tbtn" onclick="swT(this,'all','all')">初筛全部</button>
  <button class="tbtn" onclick="swT(this,'policy','all')">政策评估</button>
  <button class="tbtn" onclick="swT(this,'mrio','all')">投入产出·隐含碳</button>
  <button class="tbtn" onclick="swT(this,'forest','all')">林业碳汇·CCER</button>
  <button class="tbtn" onclick="swT(this,'governance','all')">碳市场·治理</button>
</div>
<div class="wrap">
  <div>
    <div class="shdr">
      <span class="sl" id="feed-label">今日精选文献</span>
      <span class="sc2" id="rcn">{len(final)} 篇（综合得分）</span>
    </div>
    <div id="top-feed">{final_html}</div>
    <div id="all-feed" style="display:none">{cand_html}</div>
  </div>
  <div>
    <div class="sdc">
      <div class="sdt">博士论文体系</div>
      <div class="ti"><div class="tn">01 · 已完成</div><div class="tnm">区域协调发展政策碳减排效应</div><div class="tm">DID · 294城市 · 2010-2023</div></div>
      <div class="ti"><div class="tn">02 · 进行中</div><div class="tnm">京津冀与全国隐含碳责任分担</div><div class="tm">MRIO扩展 · 生产/消费/受益侧</div></div>
      <div class="ti"><div class="tn">03</div><div class="tnm">林业碳汇CCER价值实现机制</div><div class="tm">制度分析 · 供需衔接框架</div></div>
    </div>
    <div class="sdc">
      <div class="sdt">四级漏斗流程</div>
      <div class="fstep"><span class="fn">第1级</span>中文+国际双源海选<span class="fb">Crossref·OpenAlex</span></div>
      <div class="fstep"><span class="fn">第2级</span>多维打分初筛<span class="fb">{len(candidates)}篇</span></div>
      <div class="fstep"><span class="fn">第3级</span>AI分级精读<span class="fb">{oa_n}篇OA</span></div>
      <div class="fstep"><span class="fn">第4级</span>综合得分终选<span class="fb">{len(final)}篇</span></div>
    </div>
    <div class="sdc">
      <div class="sdt">更新信息</div>
      <div class="str"><span class="stl">更新时间</span><span class="stv">{NOW_CN}</span></div>
      <div class="str"><span class="stl">终选文献</span><span class="stv">{len(final)} 篇</span></div>
      <div class="str"><span class="stl">分区标准</span><span class="stv">新锐分区表</span></div>
      <div class="str"><span class="stl">解读引擎</span><span class="stv">{provider_label}</span></div>
      <div class="str"><span class="stl">OA原则</span><span class="stv">不参与排序</span></div>
    </div>
  </div>
</div>
<script>
function swT(btn, topic, mode) {{
  document.querySelectorAll('.tbtn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  const tf = document.getElementById('top-feed');
  const af = document.getElementById('all-feed');
  const lb = document.getElementById('feed-label');
  const rn = document.getElementById('rcn');
  if (mode === 'top') {{
    tf.style.display = ''; af.style.display = 'none';
    lb.textContent = '今日精选文献';
    rn.textContent = '{len(final)} 篇（综合得分）';
  }} else {{
    tf.style.display = 'none'; af.style.display = '';
    lb.textContent = topic === 'all' ? '初筛全部文献' : '筛选结果';
    af.querySelectorAll('.lc').forEach(c => {{
      c.style.display = (topic === 'all' || (c.dataset.topics||'').includes(topic)) ? '' : 'none';
    }});
    const vis = af.querySelectorAll('.lc:not([style*="none"])').length;
    rn.textContent = vis + ' 篇';
  }}
}}
</script>
</body>
</html>"""


# ════════════════════════════════════════════════════════════════
# 主程序入口
# ════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print(f"\n{'='*62}")
    print(f"📚 碳治理文献日报 · 四级漏斗精选 · {TODAY}")
    print(f"{'='*62}")
    if AI_PROVIDER == "deterministic-fallback":
        print("⚠️ 未检测到 GEMINI_API_KEY 或 SILICONFLOW_API_KEY，将生成明确标注的规则降级解读")
    else:
        print(f"🤖 AI解读提供方：{AI_PROVIDER}")

    # 第1级：免费海选
    pool = fetch_global_literature_pool()
    if not pool:
        print("❌ 海选无结果，退出"); sys.exit(1)

    # 第2级：多维初筛
    candidates = screen_candidates(pool)
    if not candidates:
        print("❌ 初筛无结果，退出"); sys.exit(1)

    # 第3级：分级精读
    candidates = deepread_all(candidates)

    # 第4级：综合得分终选
    final = final_select(candidates)

    # 保存 JSON（含完整初筛数据和终选数据）
    out = {
        "date":            TODAY,
        "generated_at":    NOW_CN,
        "final_count":     len(final),
        "candidate_count": len(candidates),
        "ai_provider":     AI_PROVIDER,
        "language_counts": {
            "pool_zh": sum(1 for p in pool if p.get("language_group") == "zh"),
            "pool_international": sum(1 for p in pool if p.get("language_group") == "international"),
            "final_zh": sum(1 for p in final if p.get("language_group") == "zh"),
            "final_international": sum(1 for p in final if p.get("language_group") == "international"),
        },
        "retrieval_stats": RETRIEVAL_STATS,
        "final":           final,
        "candidates":      candidates,
    }
    (DATA_DIR / "latest.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (DATA_DIR / f"{TODAY}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n💾 JSON 已保存至 data/latest.json 和 data/{TODAY}.json")

    # 生成 HTML
    html = build_html(final, candidates)
    pathlib.Path("index.html").write_text(html, encoding="utf-8")
    print(f"✅ index.html 已生成（{len(html):,} 字节）")
    print(f"🌐 访问：https://HORACE0919.github.io/carbon-lit-final/\n")
