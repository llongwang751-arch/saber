"""Production providers return retrieved documents, never simulated search answers."""

from __future__ import annotations

import hashlib
from urllib.parse import urlsplit

import requests


class ProviderUnavailable(RuntimeError):
    pass


def check_cancel(token):
    if token is not None and callable(getattr(token, "is_cancelled", None)) and token.is_cancelled():
        raise InterruptedError("Research cancelled")


class StrictLLM:
    """Use the real client transport so provider errors cannot become mock prose."""

    def __init__(self, client, *, json_mode=None):
        # The host supports arbitrary OpenAI-compatible endpoints; compatibility
        # alone does not establish JSON-mode support. Auto-enable only for the
        # verified DeepSeek endpoint. Embedded integrations can explicitly opt
        # other verified providers in, or opt a deployment out.
        if json_mode is not None and type(json_mode) is not bool:
            raise ValueError("json_mode must be a boolean or None")
        self.client = client
        self.json_mode = json_mode

    def chat(self, messages, system_prompt=""):
        return self.chat_limited(messages, system_prompt=system_prompt)

    def chat_json_limited(self, messages, system_prompt="", *, max_tokens=6000, token=None, timeout=60):
        return self.chat_limited(messages, system_prompt=system_prompt, max_tokens=max_tokens,
                                 token=token, timeout=timeout, structured=True)

    def chat_limited(self, messages, system_prompt="", *, max_tokens=6000, token=None, timeout=60,
                     structured=False):
        cfg = getattr(self.client, "cfg", None)
        if cfg is None or not cfg.is_real_llm():
            raise ProviderUnavailable("研究需要配置真实 LLM；当前未配置，未生成模拟研究结果")
        payload = {"model": cfg.llm_model, "temperature": cfg.temperature, "max_tokens": max(1, max_tokens),
                   "messages": [{"role": "system", "content": system_prompt}]
                   + [{"role": m.role, "content": m.content} for m in messages]}
        json_mode = self.json_mode
        if json_mode is None:
            json_mode = urlsplit(cfg.llm_api_url).hostname == "api.deepseek.com"
        if structured and json_mode:
            payload["response_format"] = {"type": "json_object"}
        check_cancel(token)
        from internal.resilience.budget import charge
        charge("llm")
        session = requests.Session()
        unbind = None
        register = getattr(token, "register_cancel", None)
        if callable(register):
            unbind = register(session.close)
        try:
            response = session.post(
                cfg.llm_api_url,
                headers={"Authorization": f"Bearer {cfg.llm_api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=(min(5, timeout), max(0.01, min(60, timeout))),
            )
            check_cancel(token)
            if response.status_code >= 400:
                raise ProviderUnavailable(f"LLM 服务返回 HTTP {response.status_code}；请检查模型、密钥和 API 地址")
            data = response.json()
            choices = data.get("choices") or []
            if not choices:
                raise ProviderUnavailable("LLM 服务返回空结果或不兼容的响应")
            content = choices[0].get("message", {}).get("content") or ""
            usage = data.get("usage") or {}
            measured = {k: v for k, v in usage.items()
                        if k in {"prompt_tokens", "completion_tokens", "total_tokens"}
                        and isinstance(v, int) and not isinstance(v, bool) and v >= 0}
            self.last_completion_tokens = measured.get("completion_tokens", min(max_tokens, max(1, len(content))))
            callback = getattr(self.client, "_usage_callback", None)
            if callback:
                callback(measured)
            return content
        except requests.RequestException as exc:
            raise ProviderUnavailable(f"LLM 服务连接失败：{type(exc).__name__}，请检查网络和服务地址") from exc
        finally:
            if callable(unbind):
                unbind()
            session.close()


class TavilyProvider:
    def __init__(self, cfg):
        self.api_key = getattr(cfg, "search_api_key", "") or ""
        self.search_url = getattr(cfg, "search_api_url", "") or "https://api.tavily.com/search"
        self.extract_url = self.search_url.rsplit("/", 1)[0] + "/extract"

    def _post(self, url, payload, token):
        check_cancel(token)
        if not self.api_key:
            raise ProviderUnavailable("未配置搜索服务密钥；请配置 Tavily 或注入结构化检索 provider")
        from internal.resilience.budget import charge
        charge("tool")
        session = requests.Session()
        unbind = None
        register = getattr(token, "register_cancel", None)
        if callable(register):
            unbind = register(session.close)
        try:
            response = session.post(url, json={"api_key": self.api_key, **payload}, timeout=(5, 20))
            if response.status_code >= 400:
                raise ProviderUnavailable(f"搜索服务返回 HTTP {response.status_code}；请检查搜索密钥与服务地址")
            data = response.json()
            check_cancel(token)
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise ProviderUnavailable("Search provider returned an invalid result schema")
            return data["results"]
        except requests.RequestException as exc:
            raise ProviderUnavailable(f"搜索服务连接失败：{type(exc).__name__}，请检查网络和服务地址") from exc
        finally:
            if callable(unbind):
                unbind()
            session.close()

    def search(self, query, *, token=None):
        return self._post(self.search_url, {"query": query, "search_depth": "advanced",
                                           "include_raw_content": True, "max_results": 5}, token)

    def read(self, url, *, token=None):
        results = self._post(self.extract_url, {"urls": [url], "extract_depth": "basic"}, token)
        return next((r.get("raw_content") or r.get("content") or "" for r in results if isinstance(r, dict)), "")


class RagProvider:
    """Reuse retrieval passages while discarding any synthesized RAG answer."""

    def __init__(self, agent):
        self.agent = agent

    def search(self, query, *, token=None):
        check_cancel(token)
        rag = getattr(self.agent, "rag", None)
        if rag is None or not getattr(rag, "loaded", False):
            raise ProviderUnavailable("知识库尚未加载；请先导入文档，或关闭知识库检索")
        from internal.resilience.budget import charge
        charge("tool")
        hybrid = getattr(rag, "_hybrid", None)
        if hybrid is not None:
            # Retrieval only: the research assessor owns synthesis and its budget.
            results = hybrid.search(query, max(1, min(10, getattr(rag.cfg, "top_k", 5))), {})
        else:
            _, results = rag.query(query)
        check_cancel(token)
        output = []
        for item in results or []:
            item = item if isinstance(item, dict) else vars(item)
            content = item.get("parent") or item.get("content") or item.get("text") or ""
            metadata = item.get("metadata") or {}
            document = item.get("document_id") or item.get("doc_id") or metadata.get("doc_id") or hashlib.sha256(str(content).encode()).hexdigest()[:16]
            chunk = item.get("pg_id") or item.get("id") or item.get("chunk_id") or hashlib.sha256(str(content).encode()).hexdigest()[:16]
            output.append({"url_or_doc_id": f"doc:{document}:{chunk}", "title": metadata.get("title") or str(document),
                           "content": content, "content_kind": "document_excerpt"})
        return output
