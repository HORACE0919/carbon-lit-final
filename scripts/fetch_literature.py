#!/usr/bin/env python3
"""
碳治理文献日报 · 四级漏斗精准筛选系统 v3.0
============================================================
第1级：CrossRef 免费海选（50-100篇候选池，零成本公开元数据）
第2级：多维打分初筛（严格10-15篇，划定 AI token 使用边界）
第3级：硅基流动 AI 分级精读（OA文献深度解析，非OA仅摘要）
第4级：新锐分区顶刊终选（2-3篇，分区优先，OA状态不参与排序）

核心原则：
- OA中立：OA状态仅影响精读方式，不参与任何打分排序
- 分区保底：高分区非OA文献优先级永远高于低分区OA文献
- Token边界：付费token严格限定在初筛12篇范围内使用
- 容错优先：单条异常直接跳过，绝不中断整体流程
============================================================
"""

import os, json, re, sys, time, datetime, pathlib
import requests
from typing import List, Dict

# ── 全局配置（对齐四级漏斗规则文档） ─────────────────────────
CONFIG = {
    "pool_size":   80,    # 第1级：海选候选池规模
    "screen_num":  12,    # 第2级：初筛入围数量（10-15区间，付费token唯一边界）
    "final_num":   3,     # 第4级：终选推荐数量（2-3区间）
    "language_limit": False,          # 中英文混合，不限制语言
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
PARTITION_PRIORITY = {"Top": 5, "一区": 4, "二区": 3, "三区": 2, "四区": 1, "未匹配分区": 0}

# ── 新锐分区表（可持续补充） ──────────────────────────────────
XINRUI_PARTITION = {
    # Top 区
    "Nature Energy": "Top",
    "Nature Climate Change": "Top",
    "Nature Sustainability": "Top",
    "One Earth": "Top",
    "Energy & Environmental Science": "Top",
    "管理世界": "Top",
    "经济研究": "Top",
    # 一区
    "Environmental Science & Technology": "一区",
    "Applied Energy": "一区",
    "Energy Policy": "一区",
    "Energy Economics": "一区",
    "Ecological Economics": "一区",
    "Journal of Cleaner Production": "一区",
    "Resources Conservation and Recycling": "一区",
    "Resources, Conservation and Recycling": "一区",
    "Global Environmental Change": "一区",
    "Renewable and Sustainable Energy Reviews": "一区",
    "Environmental and Resource Economics": "一区",
    "中国人口·资源与环境": "一区",
    "地理学报": "一区",
    "中国工业经济": "一区",
    # 二区
    "Energy": "二区",
    "Journal of Environmental Management": "二区",
    "Environment International": "二区",
    "Land Use Policy": "二区",
    "Forest Policy and Economics": "二区",
    "Science of the Total Environment": "二区",
    "生态经济": "二区",
    "林业经济": "二区",
    "林业科学": "二区",
    "资源科学": "二区",
    "世界林业研究": "二区",
    # 三区
    "Sustainability": "三区",
    "Forests": "三区",
    "Carbon Balance and Management": "三区",
}

# ── 研究关键词组（覆盖三篇论文方向） ─────────────────────────
RESEARCH_KEYWORDS = [
    "Beijing Tianjin Hebei carbon emission coordinated development difference-in-differences policy",
    "embodied carbon MRIO multi-regional input-output inter-regional transfer China responsibility",
    "forest carbon sink CCER voluntary carbon market China ecological compensation incentive",
    "low-carbon governance industrial upgrading green innovation urban agglomeration China",
    "京津冀碳排放区域协调发展政策效应准实验",
    "林业碳汇CCER价值实现生态保护激励机制",
]
COMBINED_KW = " ".join(RESEARCH_KEYWORDS)

# ── 时间 ──────────────────────────────────────────────────────
TODAY  = datetime.date.today().strftime("%Y-%m-%d")
NOW_CN = datetime.datetime.now(
    datetime.timezone(datetime.timedelta(hours=8))
).strftime("%Y年%m月%d日 %H:%M")
DATA_DIR = pathlib.Path("data")
DATA_DIR.mkdir(exist_ok=True)

# ── 硅基流动 API（第3级精读用） ───────────────────────────────
SF_KEY   = os.environ.get("SILICONFLOW_API_KEY", "")
SF_URL   = "https://api.siliconflow.cn/v1/chat/completions"
SF_MODEL = "Qwen/Qwen2.5-72B-Instruct"


# ════════════════════════════════════════════════════════════════
# 工具函数1：OA状态识别（仅标注，严格不参与排序打分）
# ════════════════════════════════════════════════════════════════
def check_oa_status(item: Dict) -> Dict:
    """
    识别开放获取状态，仅用于标注精读方式与token消耗判定
    严格OA中立：不纳入打分排序，不影响推荐优先级
    """
    has_license = len(item.get("license", [])) > 0
    has_pdf     = any(
        lk.get("content-type") == "application/pdf"
        for lk in item.get("link", [])
    )
    is_oa = has_license or has_pdf
    fulltext_url = next(
        (lk.get("URL") for lk in item.get("link", [])
         if lk.get("content-type") == "application/pdf"),
        ""
    ) if is_oa else ""
    return {
        "oa_status":      "开放获取-可直接精读" if is_oa else "需机构权限-校园网可下载",
        "fulltext_url":   fulltext_url,
        "token_required": is_oa,  # True=可自动精读并消耗token，False=需用户手动上传
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
# 第1级：CrossRef 免费海选（零成本，仅用公开元数据）
# ════════════════════════════════════════════════════════════════
def fetch_one_batch(keyword: str) -> List[Dict]:
    """单关键词海选，单条异常直接跳过，绝不中断"""
    params = {
        "query": keyword,
        "rows":  CONFIG["pool_size"],
        "sort":  "relevance",
        "order": "desc",
        "select": (
            "title,author,issued,container-title,DOI,URL,"
            "abstract,is-referenced-by-count,license,link"
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
        print(f"  [海选异常，已跳过] {e}")
        return []

    pool = []
    for item in items:
        try:
            title   = item.get("title", [""])[0].strip()
            journal = item.get("container-title", ["未知期刊"])[0].strip()
            abstract_raw = item.get("abstract", "")
            abstract = re.sub(r"<[^>]+>", "", abstract_raw).strip()[:400]
            year_parts = item.get("issued", {}).get("date-parts", [[None]])[0]
            year = year_parts[0] if year_parts else None
            oa   = check_oa_status(item)
            impl = calculate_implementability_score(abstract)
            pool.append({
                "title":          title,
                "authors":        [
                    f"{a.get('given','')} {a.get('family','')}".strip()
                    for a in item.get("author", [])
                ],
                "year":           year,
                "journal":        journal,
                "partition":      XINRUI_PARTITION.get(journal, "未匹配分区"),
                "doi":            item.get("DOI", ""),
                "official_url":   item.get("URL", ""),
                "fulltext_url":   oa["fulltext_url"],
                "oa_status":      oa["oa_status"],
                "token_required": oa["token_required"],
                "citation":       item.get("is-referenced-by-count", 0),
                "abstract":       abstract,
                "language":       item.get("language", "unknown"),
                "implementability_score": impl,
            })
        except Exception:
            continue  # 单条解析失败直接跳过
    return pool


def fetch_global_literature_pool() -> List[Dict]:
    """
    全球文献海选：多关键词批次检索，去重合并
    覆盖中英文核心期刊、国际主流出版社、预印本平台
    全程不读全文、不消耗付费token、不受付费墙影响
    """
    print("【第1级】CrossRef 免费海选…")
    all_papers, seen_dois = [], set()
    for kw in RESEARCH_KEYWORDS:
        print(f"  🔍 {kw[:55]}…")
        batch = fetch_one_batch(kw)
        for p in batch:
            if p["doi"] and p["doi"] not in seen_dois and p["title"]:
                seen_dois.add(p["doi"])
                all_papers.append(p)
        time.sleep(1)  # 礼貌延迟，避免被限速
    print(f"✅ 海选完成，去重后共 {len(all_papers)} 篇候选（公开元数据，无全文成本）")
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
    ranked     = sorted(pool, key=lambda x: x["total_score"], reverse=True)
    candidates = ranked[:CONFIG["screen_num"]]
    oa_count   = sum(1 for c in candidates if c["token_required"])
    print(f"\n【第2级】多维打分初筛完成：")
    print(f"  ✅ 共选出 {len(candidates)} 篇（付费token仅在此范围内使用）")
    print(f"  🔓 开放获取 {oa_count} 篇（可自动AI精读）")
    print(f"  🔒 需机构权限 {len(candidates)-oa_count} 篇（建议校园网下载后手动精读）")
    print(f"  ℹ️  排序严格按综合质量，未因OA状态倾斜")
    return candidates


# ════════════════════════════════════════════════════════════════
# 第3级：分级精读（付费token严格限定在初筛范围内）
# ════════════════════════════════════════════════════════════════
def ai_deepread(lit: Dict) -> str:
    """
    对OA文献调用硅基流动AI深度解析，生成120字中文主要发现
    非OA文献：不消耗token，基于摘要生成兜底总结并标注需手动精读
    """
    if not SF_KEY:
        return f"主要发现：{lit['abstract'][:150]}（未配置AI Key，基于摘要生成）"

    prompt = f"""你是学术文献分析专家，服务于研究"区域低碳治理与林业碳汇"的农林经济管理博士生。

博士生三篇论文方向：
1. 区域协调发展政策的碳减排效应（DID，294城市面板2010-2023，京津冀处理组）
2. 京津冀与全国隐含碳责任分担（MRIO扩展，生产/消费/受益侧）
3. 林业碳汇CCER价值实现与生态保护激励机制（供需衔接框架）

请基于以下文献摘要，写出约120字的中文主要发现：
【要求】
- 必须以"主要发现："开头
- 直接说核心结论/发现/创新，不写研究背景
- 最后一句明确说明对上述三篇论文哪篇有何具体参考价值

标题：{lit['title']}
摘要：{lit['abstract']}"""

    try:
        resp = requests.post(
            SF_URL,
            headers={
                "Authorization": f"Bearer {SF_KEY}",
                "Content-Type":  "application/json"
            },
            json={
                "model":       SF_MODEL,
                "messages":    [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens":  300,
            },
            timeout=60
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"  [AI精读异常，摘要兜底] {e}")
        return f"主要发现：{lit['abstract'][:150]}（AI解析失败，基于摘要生成）"


def deepread_all(candidates: List[Dict]) -> List[Dict]:
    """
    分级精读入口：
    - OA文献（token_required=True）：调用AI深度解析，消耗付费token
    - 非OA文献（token_required=False）：不消耗token，基于摘要评估，标注需手动精读
    付费token使用范围严格限定在初筛12篇以内，不得超出
    """
    print(f"\n【第3级】分级精读（付费token仅用于OA文献）…")
    for i, lit in enumerate(candidates):
        if lit["token_required"]:
            print(f"  [{i+1}/{len(candidates)}] 🤖 AI精读（OA）：{lit['title'][:50]}…")
            lit["summary_zh"] = ai_deepread(lit)
            time.sleep(0.8)
        else:
            print(f"  [{i+1}/{len(candidates)}] 📄 摘要评估（非OA）：{lit['title'][:50]}…")
            lit["summary_zh"] = (
                f"主要发现：{lit['abstract'][:150]}"
                f"（非OA文献，建议校园网下载全文后手动精读，可深度拆解方法细节）"
            )
    return candidates


# ════════════════════════════════════════════════════════════════
# 第4级：终选推荐（分区优先，OA状态不参与排序）
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
    终选排序规则（严格执行）：
    第一优先级：新锐分区等级（Top > 一区 > 二区 > 三区 > 四区 > 未匹配）
    第二优先级：综合质量得分
    禁止因OA可精读而提升排序，高分区非OA永远优先于低分区OA
    淘汰逻辑：优先淘汰主题契合度低、方法可借鉴性弱、复现难度高的文献
    """
    ranked = sorted(
        candidates,
        key=lambda x: (
            PARTITION_PRIORITY.get(x["partition"], 0),
            x["total_score"]
        ),
        reverse=True
    )
    final = ranked[:CONFIG["final_num"]]
    print(f"\n【第4级】顶刊终选完成：{len(final)} 篇")
    for i, p in enumerate(final):
        oa_tag = "🔓OA" if "开放" in p["oa_status"] else "🔒非OA"
        print(f"  [{i+1}] [{p['partition']}]{oa_tag} {p['title'][:55]}…")
    print(f"  ℹ️  排序严格遵循「分区优先、质量其次」，未因OA倾斜")
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
    gs_url   = f'https://scholar.google.com/scholar?q={requests.utils.quote(lit["title"])}'
    gs       = f'<a class="cl" href="{gs_url}" target="_blank">Google Scholar</a>'
    is_oa    = "开放" in lit.get("oa_status", "")
    oa_badge = f'<span class="chip {"oa-open" if is_oa else "oa-lock"}">{"🔓 OA" if is_oa else "🔒 需权限"}</span>'
    part     = lit.get("partition", "未匹配分区")
    authors  = ", ".join(lit.get("authors", [])[:3]) + (" 等" if len(lit.get("authors", [])) > 3 else "")
    summary  = lit.get("summary_zh", "")
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
    {oa_badge}{ttags}
  </div>
  <div class="ca">{authors}</div>
  <div class="csu">
    <div class="csl">主要发现 &amp; 参考价值</div>
    <div class="cst">{summary}</div>
  </div>
  <div class="creason">
    <div class="crl">💡 推荐理由</div>
    <div class="crt">{reason}</div>
  </div>
  <div class="cft">
    {dl}{gs}
    <div class="rb"><span class="rl2">综合得分</span><span class="dots">{dots}</span></div>
  </div>
</div>"""

def build_html(final: List[Dict], candidates: List[Dict]) -> str:
    final_html = "\n".join(render_card(p, i+1, True) for i, p in enumerate(final))
    cand_html  = "\n".join(render_card(p, i+1, False) for i, p in enumerate(candidates))
    oa_n       = sum(1 for p in candidates if "开放" in p.get("oa_status",""))
    non_oa_n   = len(candidates) - oa_n

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
.ubar{{background:var(--a);color:white;padding:8px 28px;font-size:11px;display:flex;flex-wrap:wrap;gap:16px}}
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
  <span>📊 海选→初筛{len(candidates)}篇→终选{len(final)}篇顶刊</span>
  <span>🔓 OA可精读 {oa_n} 篇 &nbsp;🔒 需校园下载 {non_oa_n} 篇</span>
  <span>⏰ 每天北京时间 08:00 自动刷新</span>
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
      <span class="sc2" id="rcn">{len(final)} 篇（分区优先）</span>
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
      <div class="fstep"><span class="fn">第1级</span>CrossRef免费海选<span class="fb">~80篇</span></div>
      <div class="fstep"><span class="fn">第2级</span>多维打分初筛<span class="fb">{len(candidates)}篇</span></div>
      <div class="fstep"><span class="fn">第3级</span>AI分级精读<span class="fb">{oa_n}篇OA</span></div>
      <div class="fstep"><span class="fn">第4级</span>顶刊终选推荐<span class="fb">{len(final)}篇</span></div>
    </div>
    <div class="sdc">
      <div class="sdt">更新信息</div>
      <div class="str"><span class="stl">更新时间</span><span class="stv">{NOW_CN}</span></div>
      <div class="str"><span class="stl">终选文献</span><span class="stv">{len(final)} 篇</span></div>
      <div class="str"><span class="stl">分区标准</span><span class="stv">新锐分区表</span></div>
      <div class="str"><span class="stl">AI引擎</span><span class="stv">Qwen2.5-72B</span></div>
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
    rn.textContent = '{len(final)} 篇（分区优先）';
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

    # 第4级：顶刊终选
    final = final_select(candidates)

    # 保存 JSON（含完整初筛数据和终选数据）
    out = {
        "date":            TODAY,
        "generated_at":    NOW_CN,
        "final_count":     len(final),
        "candidate_count": len(candidates),
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
    print(f"🌐 访问：https://HORACE0919.github.io/carbon-lit/\n")
