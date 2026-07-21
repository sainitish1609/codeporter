from mlx_lm import load, stream_generate

model, tokenizer = load("/Volumes/SAMSUNG T20/models/qwen3-8b")

messages = [{"role": "user", "content": "Explain async/await in Python."}]
prompt = tokenizer.apply_chat_template(messages, add_generation_prompt=True, enable_thinking=False,)

for response in stream_generate(model, tokenizer, prompt=prompt, max_tokens=1024):
    print(response.text, end="", flush=True)
