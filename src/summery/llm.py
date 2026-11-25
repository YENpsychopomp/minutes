"""LLM 客戶端（OpenAI 相容 ``/chat/completions``）+ JSON 模式與用量統計。"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from . import _http
from .config import LLMConfig
from .util import LOG, estimate_tokens


class LLMError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable

    @property
    def retry_after(self) -> float | None:
        return getattr(self.__cause__, "retry_after", None)


@dataclass
class ChatResult:
    text: str
    model: str | None = None
    usage: dict = field(default_factory=dict)
    elapsed: float = 0.0
    finish_reason: str | None = None

    @property
    def input_tokens(self) -> int:
        return int(self.usage.get("prompt_tokens") or self.usage.get("input_tokens") or 0)

    @property
    def output_tokens(self) -> int:
        return int(self.usage.get("completion_tokens") or self.usage.get("output_tokens") or 0)


def extract_json(text: str) -> Any:
    """從模型回覆中盡力取出 JSON（容忍 code fence 與前後說明文字）。"""
    if text is None:
        raise ValueError("空回覆")
    cleaned = text.strip()
    fence = re.search(r"```(?:json|JSON)?\s*(.+?)```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # 抓取第一個可平衡的物件／陣列
    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        if start < 0:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(cleaned)):
            ch = cleaned[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    snippet = cleaned[start : i + 1]
                    try:
                        return json.loads(snippet)
                    except json.JSONDecodeError:
                        break
    # 最後手段：去掉尾逗號
    stripped = re.sub(r",(\s*[}\]])", r"\1", cleaned)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise ValueError(f"無法從回覆解析 JSON：{text[:200]}")


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0

    def add(self, res: ChatResult) -> None:
        self.calls += 1
        self.input_tokens += res.input_tokens
        self.output_tokens += res.output_tokens
        self.seconds += res.elapsed

    def to_dict(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "seconds": round(self.seconds, 2),
        }


class LLMClient:
    """執行緒安全的聊天客戶端（每次請求獨立，可並行）。"""

    def __init__(self, cfg: LLMConfig, *, api_key: str | None = None, verify_ssl: bool = True):
        self.cfg = cfg
        self.api_key = api_key
        self.verify_ssl = verify_ssl
        self.url = _http.endpoint(cfg.base_url, "chat/completions")
        self.usage = Usage()
        self._disable_response_format = not cfg.supports_response_format
        self._stub = None
        if cfg.stub_dir:
            stub = Path(cfg.stub_dir).expanduser()
            if not stub.is_dir():
                raise LLMError(f"llm.stub_dir 不存在：{stub}", retryable=False)
            self._stub = stub
        elif not api_key:
            raise LLMError("缺少 LLM API key", retryable=False)

    # ------------------------------------------------------------------
    def chat(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
        stub_key: str | None = None,
    ) -> ChatResult:
        if self._stub is not None:
            res = self._stub_reply(messages, stub_key, json_mode)
            self.usage.add(res)
            return res
        body: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": list(messages),
            "temperature": self.cfg.temperature if temperature is None else temperature,
        }
        limit = max_tokens or self.cfg.max_output_tokens
        token_field = self.cfg.max_tokens_field or "max_tokens"
        if limit:
            body[token_field] = int(limit)
        if json_mode and not self._disable_response_format:
            body["response_format"] = {"type": "json_object"}
        body.update(self.cfg.extra_body or {})

        def send(payload: dict[str, Any]) -> ChatResult:
            return _http.with_retries(
                lambda: self._post(payload),
                attempts=self.cfg.max_retries,
                backoff=self.cfg.retry_backoff,
                on_retry=lambda i, exc, delay: LOG.warning(
                    "LLM 重試 %d/%d：%s（%.1fs 後）", i, self.cfg.max_retries, exc, delay
                ),
            )

        try:
            result = send(body)
        except _http.HttpError as exc:
            if (
                exc.status in (400, 404, 422)
                and "response_format" in body
                and _mentions_response_format(exc.body)
            ):
                LOG.warning("服務不支援 response_format，改用提示詞要求 JSON 後續試")
                self._disable_response_format = True
                body.pop("response_format", None)
                result = send(body)
            else:
                raise LLMError(str(exc), retryable=exc.retryable) from exc

        # 推理型模型會把 token 預算花在思考（reasoning_content）上，
        # 常見症狀是 finish_reason=length 且 content 為空 → 加大預算再試一次。
        budget = int(body.get(token_field) or 0)
        if result.finish_reason == "length" and budget:
            self.usage.add(result)  # 截斷的那次同樣已計費
            bigger = min(max(budget * 2, budget + 4000), 32000)
            if bigger > budget:
                LOG.warning(
                    "模型輸出被截斷（finish_reason=length，上限 %d tokens），改以 %d tokens 重試",
                    budget, bigger,
                )
                body[token_field] = bigger
                result = send(body)
                self.usage.add(result)
            if result.finish_reason == "length":
                raise LLMError(
                    f"模型輸出連續被截斷（finish_reason=length，上限 {body[token_field]} tokens）。"
                    "推理型模型會把預算花在思考上，請調高 llm.max_output_tokens（例如 8000）、"
                    "縮短單次輸入，或改用非推理型模型。",
                    retryable=False,
                )
        else:
            self.usage.add(result)
        return result

    def _post(self, body: dict[str, Any]) -> ChatResult:
        started = time.perf_counter()
        data = _http.post_json(
            self.url, body, timeout=self.cfg.timeout, headers=_http.auth_headers(self.api_key),
            verify=self.verify_ssl,
        )
        elapsed = time.perf_counter() - started
        result = parse_chat_response(data, elapsed=elapsed)
        if not result.usage:
            # 部分 Gateway 不回 usage：以字元數估算，讓成本統計仍然可用
            prompt_chars = sum(len(str(m.get("content", ""))) for m in body.get("messages", []))
            result.usage = {
                "prompt_tokens": estimate_tokens("x" * prompt_chars),
                "completion_tokens": estimate_tokens(result.text),
                "estimated": True,
            }
        return result

    def complete(self, system: str, user: str, **kwargs) -> ChatResult:
        return self.chat([{"role": "system", "content": system}, {"role": "user", "content": user}], **kwargs)

    def complete_json(self, system: str, user: str, **kwargs) -> Any:
        """要求 JSON 輸出；解析失敗自動以較寬鬆方式再試一次。"""
        res = self.complete(system, user, json_mode=True, **kwargs)
        try:
            return extract_json(res.text)
        except ValueError:
            retry_sys = system + "\n\n【重要】只輸出合法的 JSON 物件，不要任何說明或 code fence。"
            res2 = self.complete(retry_sys, user, json_mode=False, temperature=0.0)
            return extract_json(res2.text)

    # ------------------------------------------------------------------
    def _stub_reply(self, messages: Sequence[dict[str, str]], stub_key: str | None, json_mode: bool) -> ChatResult:
        """離線/測試模式：依 stub_key 或 user 內容中的 marker 回傳固定答案。"""
        user = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
        manifest = self._stub / "manifest.json"
        data: Any = None
        if stub_key and (self._stub / f"{stub_key}.json").is_file():
            data = json.loads((self._stub / f"{stub_key}.json").read_text(encoding="utf-8"))
        elif manifest.is_file():
            table = json.loads(manifest.read_text(encoding="utf-8"))
            for key in (stub_key or "", *_markers(user)):
                if key and key in table:
                    data = table[key]
                    break
            if data is None and "*" in table:
                data = table["*"]
        if data is None:
            payload = {"summary": f"[stub] {user[:120]}", "key_points": [], "chapters": []}
            return ChatResult(
                text=json.dumps(payload, ensure_ascii=False),
                model="stub",
                usage={"prompt_tokens": max(1, len(user) // 4), "completion_tokens": 32},
            )
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        return ChatResult(
            text=text, model="stub",
            usage={"prompt_tokens": max(1, len(user) // 4), "completion_tokens": max(1, len(text) // 4)},
        )


def _mentions_response_format(body: str) -> bool:
    low = (body or "").lower()
    return "response_format" in low or "json_object" in low or "json mode" in low


def _markers(text: str) -> list[str]:
    return re.findall(r"\[\[stub:([^\]]+)\]\]", text)


def parse_chat_response(data: Any, *, elapsed: float = 0.0) -> ChatResult:
    if not isinstance(data, dict):
        raise LLMError(f"LLM 回應格式異常：{str(data)[:200]}", retryable=False)
    choices = data.get("choices") or []
    if not choices:
        raise LLMError(f"LLM 回應沒有 choices：{str(data)[:200]}", retryable=False)
    msg = choices[0].get("message") or {}
    finish_reason = choices[0].get("finish_reason")
    content = msg.get("content")
    if content is None:
        content = choices[0].get("text") or ""
    if isinstance(content, list):  # 部分服務回 content blocks
        content = "".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content
        )
    if not str(content).strip():
        # 推理型模型常把 token 預算花在 reasoning_content 上：截斷時先讓上層加大預算重試
        if finish_reason == "length":
            return ChatResult(text="", model=data.get("model"), usage=dict(data.get("usage") or {}),
                              elapsed=elapsed, finish_reason=finish_reason)
        if str(msg.get("reasoning_content") or "").strip():
            raise LLMError(
                "模型只產生思考內容（reasoning_content）而沒有正式回覆；"
                "請調高 llm.max_output_tokens，或於 llm.extra_body 關閉思考模式。",
                retryable=False,
            )
        raise LLMError("LLM 回傳空內容", retryable=True)
    return ChatResult(
        text=str(content).strip(),
        model=data.get("model"),
        usage=dict(data.get("usage") or {}),
        elapsed=elapsed,
        finish_reason=finish_reason,
    )
