import sys
import json
from pydantic import BaseModel
from langgraph.config import get_stream_writer
from typing import TypeVar


# 定义一个类型变量，限定其必须是 BaseModel 的子类
T = TypeVar('T', bound=BaseModel)

def stream_json(client, prompt: str, response_model: type[T], model: str)->T:
    """流式输出 thinking + answer，自动注入 JSON schema 到 prompt，返回 Pydantic 对象"""
    schema = response_model.model_json_schema()

    # 自动拼接 JSON 格式要求，不用每次手写
    system_msg = (
        "You are a helpful assistant. Always respond with a valid JSON object "
        f"matching this schema:\n{json.dumps(schema, ensure_ascii=False)}\n"
        "Output ONLY the JSON object, no other text."
    )

    writer = get_stream_writer()

    stream = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": prompt},
        ],
        stream=True,
        response_format={"type": "json_object"},
    )

    answer = ""
    in_thinking = False
    for chunk in stream:
        delta = chunk.choices[0].delta if chunk.choices else None
        if not delta:
            continue
        if reasoning := getattr(delta, "reasoning_content", None) or "":
            if not in_thinking:
                in_thinking = True
                sys.stdout.write("\n🤔 ")
            # sys.stdout.write(reasoning)
            # sys.stdout.flush()
            writer({"reasoning":reasoning})
        if content := delta.content or "":
            if in_thinking:
                in_thinking = False
                sys.stdout.write("\n\n💬 ")
            # sys.stdout.write(content)
            # sys.stdout.flush()
            answer += content
            writer({"content":content})

    print()

    return response_model.model_validate(json.loads(answer))
