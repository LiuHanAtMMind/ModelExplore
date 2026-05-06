import os
import sys
import argparse

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:False")

import torch
from transformers import AutoProcessor, AutoModelForCausalLM



parser = argparse.ArgumentParser()
parser.add_argument('--quant', action='store_true', help='启用4bit量化加载')
parser.add_argument('--model-id', type=str, default="qwen/qwen3.5-2B", help='模型ID或本地路径')
parser.add_argument('--cache-dir', type=str, default="model_cache", help='模型缓存目录')
parser.add_argument('--quantized-model', type=str, default=None, help='本地已量化模型目录（含config.json和权重）')
args = parser.parse_args()

MODEL_ID = args.model_id
MODEL_CACHE_DIR = args.cache_dir
QUANTIZED_MODEL_PATH = os.path.join(MODEL_CACHE_DIR, "qwen3.5-2b-4bit.pt")
SYSTEM_MESSAGE = "You are a helpful assistant."


DEVICE_MAP = "cuda:0"
MAX_NEW_TOKENS = 256
MAX_HISTORY_MESSAGES = 6


def trim_messages(messages):
    if len(messages) <= 1 + MAX_HISTORY_MESSAGES:
        return messages

    return [messages[0], *messages[-MAX_HISTORY_MESSAGES:]]


def load_or_create_quantized_model():
    os.makedirs(MODEL_CACHE_DIR, exist_ok=True)

    # 优先从本地已量化模型目录加载
    if args.quantized_model:
        print(f"从本地已量化模型目录加载: {args.quantized_model}")
        return AutoModelForCausalLM.from_pretrained(
            args.quantized_model,
            device_map=DEVICE_MAP,
            low_cpu_mem_usage=True,
        )

    if os.path.exists(QUANTIZED_MODEL_PATH):
        print(f"从缓存加载已量化模型: {QUANTIZED_MODEL_PATH}")
        return torch.load(QUANTIZED_MODEL_PATH, weights_only=False, mmap=True)

    print("未找到已量化模型，正在从原始模型量化并保存...")
    try:
        from transformers import BitsAndBytesConfig
    except ImportError:
        raise ImportError("本地量化需要 bitsandbytes 和 transformers >= 4.30，请先安装！")

    quant_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_ID,
        device_map=DEVICE_MAP,
        low_cpu_mem_usage=True,
        quantization_config=quant_config,
        cache_dir=MODEL_CACHE_DIR,
    )
    # torch.save(model, QUANTIZED_MODEL_PATH)
    print(f"已保存量化模型: {QUANTIZED_MODEL_PATH}")
    return model



# 优先用本地量化模型目录的processor
if args.quantized_model:
    processor = AutoProcessor.from_pretrained(
        args.quantized_model,
        device_map=DEVICE_MAP,
        low_cpu_mem_usage=True
    )
else:
    processor = AutoProcessor.from_pretrained(
        MODEL_ID,
        cache_dir=MODEL_CACHE_DIR,
        device_map=DEVICE_MAP,
        low_cpu_mem_usage=True
    )


if args.quantized_model:
    model = load_or_create_quantized_model()
elif args.quant:
    model = load_or_create_quantized_model()
else:
    print("未启用 --quant，直接以 float16 加载模型...")
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_ID,
        device_map=DEVICE_MAP,
        dtype=torch.float16,
        low_cpu_mem_usage=True,
        cache_dir=MODEL_CACHE_DIR,
    )


model.eval()
runtime_device = next(model.parameters()).device
print("实际 device_map:", getattr(model, "hf_device_map", "无"))

if getattr(model, "generation_config", None) is not None:
    model.generation_config.top_k = None
    model.generation_config.top_p = None

messages = [
    {"role": "system", "content": SYSTEM_MESSAGE},
]

print("对话已启动，输入 /quit 退出，输入 /clear 清空上下文。")
while True:
    #####################
    # from PIL import Image
    # image = Image.open("task_mgr_snapshot.png").convert("RGB")
    # inputs = processor(text="describe this image", images=image, return_tensors="pt").to(runtime_device)
    # input_ids = inputs["input_ids"]
    # attention_mask = inputs["attention_mask"]
    # pixel_values = inputs["pixel_values"]
    # eos_token_id = processor.tokenizer.eos_token_id
    # generated = input_ids
    # with torch.no_grad():
    #     for _ in range(128):
    #         outputs = model(
    #             input_ids=generated,
    #             attention_mask=attention_mask,
    #             pixel_values=pixel_values
    #         )
    #         next_token_logits = outputs.logits[:, -1, :]
    #         next_token_id = torch.argmax(next_token_logits, dim=-1, keepdim=True)
    #         generated = torch.cat([generated, next_token_id], dim=-1)
    #         if next_token_id[0].item() == eos_token_id:
    #             break
    #     response = processor.decode(generated[0][input_ids.shape[-1]:], skip_special_tokens=True)
    # print(response)
    ####################
    try:
        user_input = input("\n你: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n再见！")
        break

    if not user_input:
        continue
    if user_input == "/quit":
        print("再见！")
        break
    if user_input == "/clear":
        messages = [{"role": "system", "content": SYSTEM_MESSAGE}]
        print("上下文已清空。")
        continue


    # 新增 /image 命令，加载本地图片并推理
    if user_input.startswith("/image"):
        image_path = user_input[len("/image"):].strip()
        if not image_path:
            print("用法：/image 图片路径 [可选: 问题]")
            continue
        # 可选：支持 /image 路径 问题
        if " " in image_path:
            image_path, prompt = image_path.split(" ", 1)
        else:
            prompt = None
        try:
            from PIL import Image
            image = Image.open(image_path).convert("RGB")
        except Exception as e:
            print(f"图片加载失败: {e}")
            continue
        # 构造 multimodal 输入
        msg = {"role": "user", "content": {"image": image}}
        if prompt:
            msg["content"]["text"] = prompt
        messages.append(msg)
        messages = trim_messages(messages)
        try:
            # 处理多模态输入
            inputs = processor.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=False,
            )
            # processor 支持 image/text 混合
            model_inputs = processor(
                text=inputs if isinstance(inputs, str) else None,
                images=image,
                return_tensors="pt"
            ).to(runtime_device)
            input_len = model_inputs["input_ids"].shape[-1] if "input_ids" in model_inputs else 0
            with torch.inference_mode():
                outputs = model.generate(
                    **model_inputs,
                    max_new_tokens=MAX_NEW_TOKENS,
                    do_sample=False,
                    use_cache=False,
                )
            # 解码
            if "input_ids" in model_inputs:
                response = processor.decode(outputs[0][input_len:], skip_special_tokens=False)
            else:
                response = processor.decode(outputs[0], skip_special_tokens=False)
            # 兼容 parse_response 不存在或报错的情况
            try:
                if hasattr(processor, 'parse_response'):
                    result = processor.parse_response(response)
                else:
                    result = response
            except Exception:
                result = response
            print(f"\n助手: {result}")
            messages.append({"role": "assistant", "content": str(result)})
            messages = trim_messages(messages)
        except Exception as error:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            messages.pop()
            print(f"\n生成失败: {error}")
            continue
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        continue

    # 新增 /stream 命令，触发流式逐步生成
    if user_input.startswith("/stream"):
        prompt = user_input[len("/stream"):].strip()
        if not prompt:
            print("用法：/stream 你的问题")
            continue
        messages.append({"role": "user", "content": prompt})
        messages = trim_messages(messages)
        text = processor.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )
        inputs = processor(text=text, return_tensors="pt").to(runtime_device)
        input_ids = inputs["input_ids"]
        attention_mask = inputs["attention_mask"]
        eos_token_id = processor.tokenizer.eos_token_id
        generated = input_ids
        print("\n助手: ", end="", flush=True)
        try:
            for _ in range(MAX_NEW_TOKENS):
                with torch.inference_mode():
                    outputs = model(input_ids=generated, attention_mask=attention_mask)
                    next_token_logits = outputs.logits[:, -1, :]
                    next_token_id = torch.argmax(next_token_logits, dim=-1, keepdim=True)
                generated = torch.cat([generated, next_token_id], dim=-1)
                new_token = processor.tokenizer.decode(next_token_id[0], skip_special_tokens=False)
                print(new_token, end="", flush=True)
                if next_token_id[0].item() == eos_token_id:
                    break
            print()
            response = processor.tokenizer.decode(generated[0][input_ids.shape[-1]:], skip_special_tokens=False)
            result = processor.parse_response(response)
            messages.append({"role": "assistant", "content": str(result)})
            messages = trim_messages(messages)
        except RuntimeError as error:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            messages.pop()
            print(f"\n生成失败，可能是显存或共享内存不足：{error}")
            print("可以输入 /clear 后重试，或继续提更短的问题。")
            continue
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        continue

    # 默认行为：阻塞式生成
    messages.append({"role": "user", "content": user_input})
    messages = trim_messages(messages)
    text = processor.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
        enable_thinking=False, 
    )
    inputs = processor(text=text, return_tensors="pt").to(runtime_device)
    input_len = inputs["input_ids"].shape[-1]
    model_inputs = {
        "input_ids": inputs["input_ids"].to(runtime_device),
        "attention_mask": inputs["attention_mask"].to(runtime_device)
    }

    try:
        with torch.inference_mode():
            outputs = model.generate(
                **model_inputs,
                max_new_tokens=MAX_NEW_TOKENS,
                do_sample=False,
                use_cache=False,
            )
    except RuntimeError as error:
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        messages.pop()
        print(f"\n生成失败，可能是显存或共享内存不足：{error}")
        print("可以输入 /clear 后重试，或继续提更短的问题。")
        continue

    response = processor.decode(outputs[0][input_len:], skip_special_tokens=False)

    # 兼容 parse_response 不存在或报错的情况
    try:
        if hasattr(processor, 'parse_response'):
            result = processor.parse_response(response)
        else:
            result = response
    except Exception:
        result = response
    print(f"\n助手: {result}")

    messages.append({"role": "assistant", "content": str(result)})
    messages = trim_messages(messages)

    if torch.cuda.is_available():
        torch.cuda.empty_cache()