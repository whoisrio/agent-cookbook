import os
from dotenv import dotenv_values
from openai import OpenAI
import instructor

# .env 位于仓库根目录（与 .env.example 同位置）。
# 通过 __file__ 推算仓库根，避免依赖 notebook 的运行目录（cwd）。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
config = dotenv_values(os.path.join(_REPO_ROOT, ".env"))
openai_client = OpenAI(
    api_key=config["OPENAI_API_KEY"],
    base_url=config["OPENAI_API_BASE"],
    timeout=30.0,
)
model = instructor.from_openai(
    client=openai_client,
    model=config["OPENAI_MODEL"],
    mode=instructor.Mode.MD_JSON,
)