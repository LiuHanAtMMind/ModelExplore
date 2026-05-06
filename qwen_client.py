import os
from transformers import AutoProcessor, AutoModelForCausalLM
from PIL import Image
import torch
import argparse


# 默认参数，可通过命令行覆盖
DEFAULT_IMAGE_PATH = "task_mgr_snapshot.png"
DEFAULT_PROMPT = "请描述这张图片"
DEFAULT_MODEL_NAME = "./model_cache/models--qwen--qwen3.5-2B/snapshots/15852e8c16360a2fea060d615a32b45270f8a8fc"
DEFAULT_API_BASE = "http://localhost:8000/v1"
DEFAULT_MODEL_NAME = "./model_cache/models--google--gemma-4-E2B-it/snapshots/b4a601102c3d45e2b7b50e2057a6d5ec8ed4adcf"
#DEFAULT_MODEL_NAME = "./gemma-4bit-quantized"


# 用 OpenAI SDK 调用本地 Qwen3.5-2B API 进行图片推理
from openai import OpenAI


def image_to_base64(image_path):
    import base64
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")
def infer_image(api_base, model_name, image_path, prompt):
    client = OpenAI(
        base_url=api_base,
        api_key="EMPTY"  # 本地API无需密钥
    )
    image_b64 = image_to_base64(image_path)
    image_url = f"data:image/png;base64,{image_b64}"
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": image_url}
                },
                {
                    "type": "text",
                    "text": prompt
                }
            ]
        }
    ]
    chat_response = client.chat.completions.create(
        model=model_name,
        messages=messages,
        max_tokens=32768,
        temperature=0.7,
        top_p=0.8,
        presence_penalty=1.5,
    )
    print("\n模型输出：\n", chat_response.choices[0].message.content)
    return chat_response.choices[0].message.content



def main():
    parser = argparse.ArgumentParser(description="Qwen3.5-2B OpenAI API 图像推理客户端")
    parser.add_argument("--api-base", type=str, default=DEFAULT_API_BASE, help="OpenAI API base url")
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL_NAME, help="模型名")
    parser.add_argument("--image", type=str, default=DEFAULT_IMAGE_PATH, help="图片路径")
    parser.add_argument("--prompt", type=str, default=DEFAULT_PROMPT, help="图片描述问题")
    args = parser.parse_args()
    infer_image(args.api_base, args.model, args.image, args.prompt)

if __name__ == "__main__":
    main()
