"""Send one message to whichever LLM .env points at. For poking at prompts.

    make ask MSG="why is this transaction suspicious?"
"""

import sys

from agent.llm import MODEL, client

message = " ".join(sys.argv[1:]) or "Say hello in one sentence."
response = client().chat.completions.create(
    model=MODEL, messages=[{"role": "user", "content": message}], max_tokens=400)
print(f"[{MODEL}]\n{response.choices[0].message.content}")
print(f"\ntokens: {response.usage.prompt_tokens} in, "
      f"{response.usage.completion_tokens} out")
