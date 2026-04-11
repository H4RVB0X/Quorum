import sys
sys.path.insert(0, '.')
from app.utils.llm_client import LLMClient
llm = LLMClient()
messages = [
    {'role': 'system', 'content': 'You generate diverse investor personas. Return valid JSON only.'},
    {'role': 'user', 'content': 'Generate 3 investor personas. Return JSON with key personas containing a list of 3 objects each with name, backstory, archetype.'}
]
result = llm.chat_json(messages, temperature=0.9, max_tokens=1000)
print(result)
