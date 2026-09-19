"""Security Guardrail Plugin for Harness Runtime 2.0.

Inspired by NVIDIA NeMo Guardrails and Meta Llama-Guard, this plugin acts as
an impenetrable safety gateway: intercepting Prompt Injections, system role
spoofing, and masking sensitive PII before responses leave the engine.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from internal.harness.events import EventType
from internal.harness.plugins import HarnessContext, HarnessPlugin

logger = logging.getLogger(__name__)


# Common prompt injection, jailbreak, and system role spoofing signatures
_INJECTION_PATTERNS: List[re.Pattern] = [
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|above|prior)\s+(?:instructions|prompts|directions)", re.I),
    re.compile(r"disregard\s+(?:all\s+)?(?:rules|instructions)", re.I),
    re.compile(r"forget\s+(?:all\s+)?instructions", re.I),
    re.compile(r"(?:忽略|无视|丢弃|放弃|抛弃)(?:之前|上述|所有|一切|当前)?.{0,20}?(?:指令|规则|提示词|限制|设定)", re.I),
    re.compile(r"(?:输出|打印|泄露|给出|显示)(?:你的)?(?:系统|内部|内置)?.{0,20}?(?:提示词|system\s*prompt|密钥|token|环境变量|env|模板)", re.I),
    re.compile(r"(?:<\|im_start\|>|<system>|\[INST\]|<\|system\|>|<<SYS>>)", re.I),
    re.compile(r"(?:进入|开启|切换为?)(?:开发者模式|越狱模式|上帝模式|无限制模式|dan\s*mode)", re.I),
    re.compile(r"(?:关闭|禁用|绕过|停止|消除).{0,20}?(?:安全|护栏|guardrails?|审核|拦截|防御|过滤)", re.I),
    re.compile(r"(?:不要|无需|不用|切勿)遵循.{0,15}?(?:安全|策略|规则)", re.I),
    re.compile(r"(?:合成|制造|制作).{0,15}?(?:危险化学品|炸药|毒品|武器)", re.I),
    re.compile(r"(?:泄露|窃取|导出|外发).{0,25}?(?:机密|内部数据|租户|消息|数据)", re.I),
    re.compile(r"(?:伪造|篡改).{0,15}?(?:公章|合同|证件)", re.I),
    re.compile(r"(?:黑客攻击|网络攻击|入侵指南|渗透利用)", re.I),
    re.compile(r"(?:无限制开发者特权|管理员级指令)", re.I),
    re.compile(r"(?:把系统|修改系统|系统默认).{0,20}?(?:阈值|配置|安全).{0,15}?为", re.I),
    re.compile(r"(?:没有任何道德限制|邪恶\s*ai|发动勒索攻击)", re.I),
    re.compile(r"(?:保存到|存入|归档).{0,20}?(?:记忆|库).{0,20}?(?:身份证|手机号|密码|环境变量|所有租户|数据库)", re.I),
    re.compile(r"(?:身份证|手机号|取款密码|管理员密码|数据库连接串|所有租户).{0,60}?(?:存入|保存|归档|写入).{0,15}?(?:记忆|库)", re.I),
    re.compile(r"(?:管理员密码|取款密码|数据库连接串).{0,20}?(?:记住|存入|可见|告诉)", re.I),
    re.compile(r"发往\s*https?://", re.I),
    re.compile(r"安全防御机制.{0,15}?关闭", re.I),
]

# Sensitive PII Patterns
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?86)?(1[3-9]\d{9})(?!\d)")
_ID_CARD_RE = re.compile(r"(?<!\d)(\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?!\d)")
_API_KEY_RE = re.compile(r"(sk-[A-Za-z0-9]{15,}|ghp_[A-Za-z0-9]{30,})")


def find_injection(text: str) -> Optional[Tuple[int, str]]:
    """返回第一个命中的注入规则 (index, pattern)，未命中返回 None。

    供 RAG 等链路在拼接 prompt 前过滤不可信检索内容复用。
    """
    if not text:
        return None
    for idx, pat in enumerate(_INJECTION_PATTERNS):
        if pat.search(text):
            return idx, pat.pattern
    return None


def sanitize_pii(text: str) -> str:
    """Mask Chinese phone numbers, ID cards, and common API keys."""
    if not text:
        return ""

    # Mask Phone: 13812345678 -> 138****5678
    def _mask_phone(m):
        raw = m.group(0)
        return raw[:3] + "****" + raw[-4:]
    text = _PHONE_RE.sub(_mask_phone, text)

    # Mask ID Card: 110101199001011234 -> 110101********1234
    def _mask_id(m):
        raw = m.group(0)
        return raw[:6] + "********" + raw[-4:]
    text = _ID_CARD_RE.sub(_mask_id, text)

    # Mask API Key: sk-... -> sk-****, ghp_... -> ghp_****
    def _mask_key(m):
        raw = m.group(0)
        if raw.startswith("ghp_"):
            return "ghp_****"
        if raw.startswith("sk-"):
            return "sk-****"
        return raw[:4] + "****"
    text = _API_KEY_RE.sub(_mask_key, text)

    return text


class SecurityGuardrailPlugin(HarnessPlugin):
    """Safety guardrail plugin enforcing input sanitization and PII output protection."""

    name = "security_guardrail"
    priority = 1  # Runs before any other plugin to protect the engine

    def on_session_start(self, ctx: HarnessContext) -> None:
        """Inspect input query for prompt injection and jailbreak attempts."""
        query = ctx.query
        for pat in _INJECTION_PATTERNS:
            if pat.search(query):
                logger.warning(f"Security Alert: Blocked prompt injection in query: '{query}'")
                ctx.interrupted = True
                ctx.interrupted_reason = "Blocked by Security Guardrail: Prompt injection detected"
                ctx.emit(EventType.ERROR, {
                    "security_alert": "prompt_injection_blocked",
                    "matched_pattern": pat.pattern,
                    "query": query,
                })
                # Set a safe polite denial
                ctx.state["final_answer"] = "抱歉，您的请求包含违规指令或越狱提示，系统已拒绝执行该操作。"
                break

    def on_session_end(self, ctx: HarnessContext, final_result: Any) -> None:
        """Sanitize output to ensure no PII or sensitive keys are leaked."""
        if isinstance(final_result, dict) and "answer" in final_result:
            final_result["answer"] = sanitize_pii(str(final_result["answer"]))
        elif isinstance(final_result, str):
            ctx.state["sanitized_output"] = sanitize_pii(final_result)
