curl http://localhost:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen3.5-2B",
    "messages": [{"role": "user", "content": "Quel temps fait-il à Paris ?"}],
    "tools": [{
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "Obtenir la météo pour une ville",
        "parameters": {
          "type": "object",
          "properties": {
            "city": {"type": "string", "description": "Nom de la ville"}
          },
          "required": ["city"]
        }
      }
    }],
    "tool_choice": "auto"
  }'