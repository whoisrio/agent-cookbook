from dotenv import dotenv_values
from openai import OpenAI
import instructor

config = dotenv_values('.env')
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