"""Robust slot and preference extraction engine.

Inspired by TencentDB-Agent-Memory's structured memory classification,
this module extracts fine-grained user profiles, preferences, constraints,
and dislikes with explicit polarity handling (negation support).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class ExtractedSlot:
    category: str  # "profile", "like", "dislike", "style", "domain"
    key: str
    value: str
    polarity: str  # "positive", "negative"
    confidence: float = 1.0


# Clean common tail punctuation and conversational particles
_TRAILING_CLEAN_RE = re.compile(r"[\s，。！？!?,.~～啦呢吧呀啊哈\(\)（）]+$")
_LEADING_CLEAN_RE = re.compile(r"^[\s，。！？!?,.~～:：]+")


def _clean_text(text: str) -> str:
    cleaned = _LEADING_CLEAN_RE.sub("", text)
    cleaned = _TRAILING_CLEAN_RE.sub("", cleaned)
    return cleaned.strip()


class SlotExtractor:
    """Deterministic slot extraction engine with robust negation and condition handling."""

    # Negative preferences (dislikes / restrictions)
    NEGATIVE_PATTERNS: List[Tuple[re.Pattern, str, str]] = [
        (re.compile(r"(?:我(?:极不|很不|并不|从不|绝不|不|不要|完全不)(?:太)?(?:喜欢|爱|中意|习惯))\s*([^，,；;。!?！？\n\r]+)", re.I), "dislike", "忌口与偏好限制"),
        (re.compile(r"(?:我(?:极度)?(?:讨厌|反感|排斥|忌讳|受不了))\s*([^，,；;。!?！？\n\r]+)", re.I), "dislike", "忌口与偏好限制"),
        (re.compile(r"(?:千万别|请勿|不要|别给我|严禁)(?:向我)?(?:推荐|提供|使用|说|发)\s*([^，,；;。!?！？\n\r]+)", re.I), "constraint", "交互禁忌"),
    ]

    # Positive preferences (likes / habits)
    POSITIVE_PATTERNS: List[Tuple[re.Pattern, str, str]] = [
        (re.compile(r"(?:我(?:很|特别|非常|超|最|比较)?(?:喜欢|爱好|偏好|青睐|热衷于))\s*([^，,；;。!?！？\n\r]+)", re.I), "like", "喜好"),
        (re.compile(r"(?:我(?:极度)?爱)\s*([^，,；;。!?！？\n\r]+)", re.I), "like", "喜好"),
        (re.compile(r"(?:我的爱好是|我的兴趣是)\s*([^，,；;。!?！？\n\r]+)", re.I), "like", "喜好"),
    ]

    # Profile & Identity
    PROFILE_PATTERNS: List[Tuple[re.Pattern, str, str]] = [
        (re.compile(r"(?:我叫|我的名字(?:是|叫)|称呼我(?:为)?)\s*([^，,；;。!?！？\n\r]+)", re.I), "profile", "姓名"),
        (re.compile(r"(?:我(?:是一名|是做|主要做|从事|是|的职业是))\s*([^，,；;。!?！？\n\r]+)", re.I), "profile", "职业与身份"),
        (re.compile(r"(?:我的公司是|我在|我目前在)\s*([^，,；;。!?！？\n\r]+?)(?:做|当|工作|任职)", re.I), "profile", "工作单位"),
    ]

    # Language & Style
    STYLE_PATTERNS: List[Tuple[re.Pattern, str, str]] = [
        (re.compile(r"(?:请用|回答(?:请)?用|尽量用|默认用)\s*(繁体中文|简体中文|英文|日语|韩语|粤语|专业术语|通俗易懂的语言)\s*(?:回答|回复|交流|写)?", re.I), "style", "语言偏好"),
        (re.compile(r"(?:请用|用)\s*([^，,；;。!?！？\n\r]+?)(?:的)?风格(?:回答|回复)", re.I), "style", "回复风格"),
        (re.compile(r"(?:回答(?:请)?(?:务必)?|回答风格(?:请)?(?:用)?)\s*(简短(?:点|一些)?|详细(?:点|一些)?|幽默|严肃|精炼|列点式|结构化)", re.I), "style", "回复风格"),
        (re.compile(r"(?:代码(?:请)?(?:默认)?用|用)\s*([a-zA-Z0-9#+]+(?:\s*语言)?)\s*(?:实现|编写|写)", re.I), "domain", "编程语言偏好"),
    ]

    def extract(self, message: str) -> List[ExtractedSlot]:
        """Convenience instance method for extracting slots."""
        return self.extract_slots(message)

    @classmethod
    def extract_slots(cls, message: str) -> List[ExtractedSlot]:
        if not message or not message.strip():
            return []

        text = message.strip()
        extracted: List[ExtractedSlot] = []

        # 1. 优先检测负向偏好（否定判断），若命中则跳过被包裹的正向关键词
        for pattern, category, key in cls.NEGATIVE_PATTERNS:
            m = pattern.search(text)
            if m:
                val = _clean_text(m.group(1))
                # 截断后续可能出现的从句，例如 "我不喜欢吃香菜，因为太难闻了" -> "香菜"
                val = re.split(r"[，,；;。]|因为|但是|但|却|所以", val)[0].strip()
                if val and len(val) <= 60:
                    extracted.append(ExtractedSlot(
                        category=category,
                        key=key,
                        value=f"避免/不喜欢: {val}",
                        polarity="negative",
                        confidence=0.95,
                    ))
                    # 避免对同一片段重复抽取正向偏好
                    text = text.replace(m.group(0), "")

        # 2. 检测正向偏好
        for pattern, category, key in cls.POSITIVE_PATTERNS:
            m = pattern.search(text)
            if m:
                val = _clean_text(m.group(1))
                val = re.split(r"[，,；;。]|但是|但|却", val)[0].strip()
                if val and len(val) <= 60:
                    extracted.append(ExtractedSlot(
                        category=category,
                        key=key,
                        value=val,
                        polarity="positive",
                        confidence=0.95,
                    ))

        # 3. 身份与画像检测
        for pattern, category, key in cls.PROFILE_PATTERNS:
            m = pattern.search(text)
            if m:
                val = _clean_text(m.group(1))
                val = re.split(r"[，,；;。]", val)[0].strip()
                if val and len(val) <= 50:
                    extracted.append(ExtractedSlot(
                        category=category,
                        key=key,
                        value=val,
                        polarity="positive",
                        confidence=0.98,
                    ))

        # 4. 风格与技术偏好检测
        for pattern, category, key in cls.STYLE_PATTERNS:
            m = pattern.search(text)
            if m:
                val = _clean_text(m.group(1))
                if val and len(val) <= 40:
                    extracted.append(ExtractedSlot(
                        category=category,
                        key=key,
                        value=val,
                        polarity="positive",
                        confidence=0.90,
                    ))

        return extracted

    @classmethod
    def extract_single_preference(cls, message: str) -> Tuple[str, str, bool]:
        """Backward-compatible helper aligned with previous extract_and_save signature."""
        slots = cls.extract_slots(message)
        if not slots:
            return "", "", False
        primary = slots[0]
        return primary.key, primary.value, True
