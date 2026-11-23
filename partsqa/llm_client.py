"""LLM Client Layer：金鑰管理、模型設定、Timeout/Retry、用量與成本計算、Mock。

- API Key 只從環境變數 ANTHROPIC_API_KEY 讀取，不印出、不寫入日誌。
- 端點從 PQA_LLM_BASE_URL 讀取；若是 Microsoft Foundry（*.services.ai.azure.com）使用 AnthropicFoundry 客戶端。
- 一般測試使用 MockLLM（PQA_MOCK_LLM=1），不呼叫真實 API。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from types import SimpleNamespace

import anthropic

from .config import Settings

log = logging.getLogger(__name__)

# 每百萬 token 美元價格（Anthropic 官方定價；Foundry 依相同標準價計費）
# input / output / cache_write(5 分鐘, 1.25x input) / cache_read
PRICING = {
    "claude-haiku-5-5": {"in": 0.10, "out": 0.50, "cw": 0.125, "cr": 0.01},
    "claude-sonnet-5-5": {"in": 2.00, "out": 10.00, "cw": 2.50, "cr": 0.10},
    "claude-opus-5-5": {"in": 4.00, "out": 20.00, "cw": 5.00, "cr": 0.20},
}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    calls: int = 0
    by_model: dict = field(default_factory=dict)

    def add(self, model: str, u) -> None:
        it = getattr(u, "input_tokens", 0) or 0
        ot = getattr(u, "output_tokens", 0) or 0
        cr = getattr(u, "cache_read_input_tokens", 0) or 0
        cw = getattr(u, "cache_creation_input_tokens", 0) or 0
        self.input_tokens += it
        self.output_tokens += ot
        self.cache_read_tokens += cr
        self.cache_write_tokens += cw
        self.calls += 1
        m = self.by_model.setdefault(model, {"in": 0, "out": 0, "cr": 0, "cw": 0})
        m["in"] += it
        m["out"] += ot
        m["cr"] += cr
        m["cw"] += cw

    @property
    def cost_usd(self) -> float:
        total = 0.0
        for model, m in self.by_model.items():
            p = PRICING.get(model)
            if p:
                total += (m["in"] * p["in"] + m["out"] * p["out"] + m["cw"] * p["cw"] + m["cr"] * p["cr"]) / 1e6
        return round(total, 6)

    @property
    def total_input(self) -> int:
        """含快取的總輸入 token（input + cache read + cache write）。"""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


class LLMError(RuntimeError):
    """模型呼叫失敗（清楚回報原因，不假裝成功）。"""


def make_client(s: Settings):
    """建立 SDK 客戶端；無金鑰時拋出 LLMError。"""
    if s.use_mock_llm:
        return MockLLM()
    if not s.has_api_key:
        raise LLMError("尚未設定 ANTHROPIC_API_KEY（請在 .env 設定），無法呼叫模型。")
    base = s.anthropic_base_url.rstrip("/")
    if base.endswith("/v1/messages"):
        base = base[: -len("/v1/messages")]
    kwargs = {"api_key": s.anthropic_api_key, "timeout": s.llm_timeout_s, "max_retries": s.llm_max_retries}
    if "services.ai.azure.com" in base:
        return anthropic.AnthropicFoundry(base_url=base, **kwargs)
    if base:
        kwargs["base_url"] = base
    return anthropic.Anthropic(**kwargs)


def call_messages(client, **params):
    """呼叫 Messages API，將 SDK 例外轉為清楚的錯誤訊息。"""
    try:
        return client.messages.create(**params)
    except anthropic.AuthenticationError as e:
        raise LLMError("API Key 驗證失敗（401），請確認 ANTHROPIC_API_KEY。") from e
    except anthropic.PermissionDeniedError as e:
        raise LLMError("API 權限不足（403），請確認此金鑰可用的模型。") from e
    except anthropic.NotFoundError as e:
        raise LLMError(f"模型或端點不存在（404）：{params.get('model')}") from e
    except anthropic.RateLimitError as e:
        raise LLMError("超過速率限制（429），請稍後再試。") from e
    except anthropic.BadRequestError as e:
        raise LLMError(f"請求格式錯誤（400）：{getattr(e, 'message', e)}") from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"模型服務錯誤（{e.status_code}）") from e
    except anthropic.APIConnectionError as e:
        raise LLMError("無法連線到模型端點（網路或 PQA_LLM_BASE_URL 設定）") from e


# ====================================================================== Mock（測試用）
def _block(**kw):
    ns = SimpleNamespace(**kw)
    ns.model_dump = lambda **_: dict(kw)
    return ns


class MockLLM:
    """可預先排定回應的假模型：script 為依序回傳的回應清單。

    每個回應可以是 {"tool": name, "input": {...}} 或 {"text": "..."}。
    未排定時：第一次回傳文字「(mock) 已收到問題」。
    """

    def __init__(self, script: list[dict] | None = None):
        self.script = list(script or [])
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **params):
        self.calls.append(params)
        step = self.script.pop(0) if self.script else {"text": "(mock) 已收到問題"}
        usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        if step.get("refusal"):
            return SimpleNamespace(content=[], stop_reason="refusal", usage=usage, model=params["model"])
        if "tool" in step:
            content = [_block(type="tool_use", id=f"tu_{len(self.calls)}", name=step["tool"], input=step["input"])]
            return SimpleNamespace(content=content, stop_reason="tool_use", usage=usage, model=params["model"])
        return SimpleNamespace(content=[_block(type="text", text=step["text"])], stop_reason="end_turn", usage=usage,
                               model=params["model"])
