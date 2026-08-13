from dotenv import dotenv_values
import os
from langchain_openai.chat_models import ChatOpenAI
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_config = dotenv_values(os.path.join(_REPO_ROOT, ".env"))

model = ChatOpenAI(model=_config.get("OPENAI_MODEL"),
                   api_key=_config.get("OPENAI_API_KEY"),
                   base_url=_config.get("OPENAI_API_BASE"))