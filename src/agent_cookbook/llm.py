"""LLM 客户端：直接用 openai 原生 SDK，不依赖 instructor / langchain。

- `config`        —— 仓库根 `.env` 的键值
- `openai_client` —— OpenAI 兼容客户端（base_url / timeout 可配）
- `structured`    —— 结构化输出助手：注入 schema → `json_object` → pydantic 校验回对象
"""

import json
import os
from typing import Any, TypeVar, cast

from dotenv import dotenv_values
from langchain_openai.chat_models import ChatOpenAI
from openai import OpenAI
from pydantic import SecretStr

T = TypeVar("T")

# .env 位于仓库根目录（与 .env.example 同位置）。
# 通过 __file__ 推算仓库根，避免依赖 notebook 的运行目录（cwd）。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
config = dotenv_values(os.path.join(_REPO_ROOT, ".env"))

openai_client = OpenAI(
    api_key=config["OPENAI_API_KEY"],
    base_url=config["OPENAI_API_BASE"],
    # 非流式调用要等整段响应返回，思考模型会把思考链也算进去；
    # 可用 .env 的 OPENAI_TIMEOUT 覆盖（默认 120 秒）。
    timeout=float(config.get("OPENAI_TIMEOUT") or 120.0),
)

# SDK 的重载对 messages / response_format 卡得很死，这里统一走 Any，
# 由调用方（notebook）保证消息结构正确。
_client: Any = openai_client
_model_name = str(config["OPENAI_MODEL"])

# langchain 版的 chat model：只有 notebook 07（langchain `create_agent` 专题）需要，
# 其余地方一律用上面的 `openai_client` / `structured` 走原生 API。
model = ChatOpenAI(
    model=str(config.get("OPENAI_MODEL") or ""),
    api_key=SecretStr(str(config.get("OPENAI_API_KEY") or "")),
    base_url=str(config.get("OPENAI_API_BASE") or ""),
)


def structured(
    response_model: type[T],
    messages: list[dict[str, Any]],
    **kwargs: Any,
) -> T:
    """用原生 API 拿结构化输出。

    做法：把 pydantic schema 拼进 system prompt，请求 `response_format=json_object`，
    再把返回的 JSON 用 `response_model` 校验成对象。

    `response_model=str` 时退化成纯文本调用（不注入 schema、不要求 JSON）。
    """
    if response_model is str:
        resp = _client.chat.completions.create(
            model=_model_name,
            messages=messages,
            **kwargs,
        )
        return cast("T", resp.choices[0].message.content)

    model_cls: Any = response_model
    schema = model_cls.model_json_schema()
    system_message = {
        "role": "system",
        "content": (
            "Always respond with a valid JSON object matching this schema:\n"
            f"{json.dumps(schema, ensure_ascii=False)}\n"
            "Output ONLY the JSON object, no other text."
        ),
    }
    resp = _client.chat.completions.create(
        model=_model_name,
        messages=[system_message, *messages],
        response_format={"type": "json_object"},
        **kwargs,
    )
    return cast("T", model_cls.model_validate_json(resp.choices[0].message.content or "{}"))
