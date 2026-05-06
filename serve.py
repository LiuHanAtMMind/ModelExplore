"""
自定义 OpenAI 兼容 API 服务，用于加载量化后的多模态模型。
绕开 transformers serve CLI 在 5.6.0.dev0 对量化多模态模型的 bug。

用法：
  python serve.py --model-dir ./gemma-8bit-quantized --port 8000
  python serve.py --model-dir ./gemma-4bit-quantized --port 8000
"""

import argparse
import json
import time
import uuid
import os

import torch
import transformers
from transformers import AutoModelForCausalLM, AutoProcessor

os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

parser = argparse.ArgumentParser(description="Custom OpenAI-compatible serve for quantized models.")
parser.add_argument("--model-dir", type=str, required=True, help="Path to quantized model directory")
parser.add_argument("--device-map", type=str, default="auto", help="Device map (default: auto)")
parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")
parser.add_argument("--port", type=int, default=8000, help="Port to bind (default: 8000)")
parser.add_argument("--max-new-tokens", type=int, default=512, help="Max new tokens per generation")
args = parser.parse_args()

transformers.modeling_utils.caching_allocator_warmup = lambda *args, **kwargs: None


print(f"Loading model from {args.model_dir} ...")
processor = AutoProcessor.from_pretrained(args.model_dir)
model = AutoModelForCausalLM.from_pretrained(
    args.model_dir,
    device_map=args.device_map,
    low_cpu_mem_usage=True,
    torch_dtype=torch.float16
)
model.eval()
print(f"Model loaded. device_map: {getattr(model, 'hf_device_map', 'N/A')}")

# --- FastAPI / uvicorn ---
try:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse
    import uvicorn
except ImportError:
    raise ImportError("Please install fastapi and uvicorn: pip install fastapi uvicorn")

app = FastAPI()


def build_chat_response(model_name, content, finish_reason="stop"):
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": args.model_dir,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "local",
            }
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    max_tokens = body.get("max_tokens", args.max_new_tokens)
    model_name = body.get("model", args.model_dir)

    try:
        text = processor.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )
        inputs = processor(text=text, return_tensors="pt")
        device = next(model.parameters()).device
        inputs = {k: v.to(device) for k, v in inputs.items()}
        input_len = inputs["input_ids"].shape[-1]

        with torch.inference_mode():
            outputs = model.generate(
                **inputs,
                max_new_tokens=max_tokens,
                do_sample=False,
                use_cache=True,
            )

        response_text = processor.decode(outputs[0][input_len:], skip_special_tokens=True)

        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        return JSONResponse(build_chat_response(model_name, response_text))
    except Exception as e:
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        return JSONResponse(
            {"error": {"message": str(e), "type": "server_error"}},
            status_code=500,
        )


if __name__ == "__main__":
    print(f"Starting server on {args.host}:{args.port} ...")
    uvicorn.run(app, host=args.host, port=args.port)
