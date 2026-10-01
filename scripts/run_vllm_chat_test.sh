curl http://localhost:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen3.5-2B",
    "messages": [{"role": "user", "content": "Dis bonjour en une phrase."}],
    "max_tokens": 50
  }'