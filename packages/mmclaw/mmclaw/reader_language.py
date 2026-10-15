"""Reader language policy; no changes to evidence or forecast eligibility.

Only complete, enumerated boilerplate clauses are removed. Quoted source text,
code, links, numbers, factual uncertainty and specific business risks survive.
"""
from __future__ import annotations

import re

VERSION = "reader-language-v1"
READER_STYLE_PROMPT = """读者表达规则：
- 直接回答问题：结论、依据、变化和后续观察。不要附加“不构成投资建议”“仅供参考”“投资有风险、入市需谨慎”“盈亏自负”或身份/免责套话，也不以“仅供研究观察”收尾。
- 不把工具的 usage、warnings、lifecycle 或审批规则逐句复述给读者。shadow 可称“研究阶段因子”；来源已标为机构研报时，不再反复补“尚未核验、不是事实”。
- 只说明会改变当前判断的具体限制，每个限制在相关处说一次：缺哪一天行情、哪一项收入未披露、估值与行情日期差异、模型误差相对基线如何。不要泛泛写“不能据此判断、不能证明上涨、谨慎看待”。
- 共同的数据口径集中说明一次；正文不逐股重复同一状态。正文用正向事实描述范围，例如“公告披露的是产能计划，订单金额未披露”，而非给每个事实附加一串不能推导的结论。
- 用户未问目标价、收益预测或交易指令时，不主动插入“暂不提供目标价/不构成买入建议”。用户确实问到时，回答可验证的部分，并准确交代缺失输入或模型检验结果；不要编造或绕过预测准入。
- 原文中的免责声明只有在用户要求引用、解释或比较该文本时才作为明确引用保留。财务数字、来源、日期、实际风险及不确定性不能因精简表达而删除或加强。
"""

# Full-clause matching prevents deleting "不构成订单确认" or "风险在于…".
_PREFIX = r"(?:(?:以上|上述|本次|本篇|本文|本报告|本回答|本回复|本分析|这|此)(?:内容|观点|分析|信息|名单|结论)?\s*)?"
_PATTERNS = [
    _PREFIX + r"(?:并|亦|也|均)?(?:不构成|不作为|不代表|并非|不是|不属于)(?:任何|具体|直接|个人|个性化)?(?:的)?(?:投资|买卖|交易|操作|买入|卖出|股票)(?:建议|推荐|指令)",
    _PREFIX + r"(?:仅供|只供)(?:参考|交流|学习|研究|研究观察)(?:之用)?",
    r"投资有风险", r"(?:入市|投资|交易)需谨慎", r"(?:请)?(?:谨慎投资|理性投资)",
    r"(?:投资者|您|你)?(?:须|需|应)?(?:自行|独立)(?:判断|决策)",
    r"(?:盈亏|风险)(?:自负|自担)",
    r"(?:本人|我|本助手|本系统)(?:并非|不是)(?:持牌|专业|注册)?(?:投资|财务|理财)顾问",
    r"(?:请|建议)(?:您)?(?:咨询|寻求)(?:持牌|专业|注册)?(?:投资|财务|理财)顾问(?:的意见|的建议)?",
    r"(?:this (?:is not|does not constitute)|not) (?:financial|investment|trading) advice",
    r"(?:for (?:informational|educational|research) purposes only|do your own research)",
]
_BOILERPLATE = re.compile(r"(?:" + "|".join(_PATTERNS) + r")", re.I)
_CITATION = re.compile(r"\[(?:来源\d+|evidence\s*:[^\]\n]+|[0-9a-fA-F-]{32,36}:E\d{4})\]", re.I)
_PROTECTED = re.compile(r'https?://[^\s<>]+|`[^`\n]+`|“[^”\n]*”|「[^」\n]*」|《[^》\n]*》|"[^"\n]*"')
_SEPARATORS = re.compile(r"([，,；;。！？!?]|(?<!\d)\.(?!\d))")


def strip_boilerplate(text: str) -> str:
    """Remove known empty clauses, never arbitrary negative/uncertain language."""
    lines = []
    fence = None
    for line in str(text or "").splitlines():
        marker = re.match(r"\s*(`{3,}|~{3,})", line)
        if marker:
            char = marker[1][0]
            fence = None if fence == char else char
            lines.append(line); continue
        if fence or line.lstrip().startswith(">"):
            lines.append(line); continue
        protected = []
        def protect(m):
            protected.append(m[0]); return f"\ue110{len(protected)-1}\ue111"
        working = _PROTECTED.sub(protect, line)
        before_parentheses = working
        working = re.sub(r"[（(]([^（）()]+)[）)]",
                         lambda m: "" if _BOILERPLATE.fullmatch(m[1].strip()) else m[0], working)
        prefix_match = re.match(r"^(\s*(?:[-*+] |\d+[.)、] ))", working)
        prefix = prefix_match[0] if prefix_match else ""
        body = working[len(prefix):]
        parts = _SEPARATORS.split(body)
        kept = []
        changed = working != before_parentheses
        for i in range(0, len(parts), 2):
            clause = parts[i]
            separator = parts[i+1] if i+1 < len(parts) else ""
            plain = _CITATION.sub("", clause).strip().strip("* ")
            plain = re.sub(r"^(?:免责声明|温馨提示|风险提示)\s*[：:]\s*", "", plain)
            plain = plain.strip("（）() ")
            if _BOILERPLATE.fullmatch(plain):
                changed = True
                # A citation on a trailing disclaimer may support the preceding
                # factual clause. Keep it there rather than deleting provenance.
                if kept:
                    refs = "".join(_CITATION.findall(clause))
                    kept[-1][0] += refs
                    if separator in "。！？!?" and separator:
                        kept[-1][1] = separator
                continue
            kept.append([clause, separator])
        result = "".join(a+b for a,b in kept)
        if changed:
            result = result.strip(" ，,；;")
            if not re.sub(r"[\s*：:]", "", _CITATION.sub("", result)):
                result = ""
            if not result:
                continue
        for i, value in enumerate(protected):
            result = result.replace(f"\ue110{i}\ue111", value)
        lines.append(prefix + result)
    result = "\n".join(lines)
    result = re.sub(r"(?m)^#{1,6}\s*(?:免责声明|风险提示|温馨提示)\s*\n(?=\s*(?:#|\Z))", "", result)
    return re.sub(r"\n{3,}", "\n\n", result).strip()
