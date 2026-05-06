import os
import sys
import base64
import glob

# Manually point to your CUDA path
if sys.platform == "win32":
    cuda_path = r"C:\\Program Files\\NVIDIA GPU Computing Toolkit\\CUDA\\v12.6\\bin"
    if os.path.exists(cuda_path):
        print("Adding CUDA path to DLL search directories...")
        os.add_dll_directory(cuda_path)

from llama_cpp import Llama
from llama_cpp.llama_chat_format import Llava15ChatHandler, Llava16ChatHandler, Qwen25VLChatHandler

# --- 配置 ---

MODEL_DIR = "./models/Qwen3.5-2B-GGUF"
MODEL_PATH = os.path.join(MODEL_DIR, "Qwen3.5-2B-UD-Q8_K_XL.gguf")

# MODEL_DIR = "./models/gemma-4-E2B-it-GGUF"
# MODEL_PATH = os.path.join(MODEL_DIR, "gemma-4-E2B-it-Q8_0.gguf")

N_GPU_LAYERS = int(os.environ.get("N_GPU_LAYERS", -1))

# --- 查找视觉编码器 (mmproj) ---
# 视觉流水线通过 mtmd 统一，但 chat format（prompt 模板）仍因模型而异
# 各子类只覆写 CHAT_FORMAT，视觉处理完全继承自 Llava15ChatHandler
VISION_HANDLERS = {
    "qwen":  Qwen25VLChatHandler,
    "gemma": Llava16ChatHandler,
}

def _pick_handler_cls(model_dir):
    dir_lower = os.path.basename(model_dir).lower()
    for key, cls in VISION_HANDLERS.items():
        if key in dir_lower:
            return cls
    return Llava15ChatHandler  # 默认 fallback

chat_handler = None
mmproj_files = glob.glob(os.path.join(MODEL_DIR, "*mmproj*"))
if mmproj_files:
    mmproj_path = mmproj_files[0]
    handler_cls = _pick_handler_cls(MODEL_DIR)
    print(f"✓ 已找到视觉编码器: {mmproj_path}")
    print(f"  chat format: {handler_cls.__name__}")
    chat_handler = handler_cls(clip_model_path=mmproj_path)
else:
    print(f"ℹ 未找到 mmproj 文件，以纯文本模式运行")

# --- 实例化模型 ---
llm = Llama(
    model_path=MODEL_PATH,
    chat_handler=chat_handler,
    n_gpu_layers=N_GPU_LAYERS,
    n_ctx=4096,
    n_threads=4,
    verbose=True
)


def image_to_data_uri(image_path):
    """将图片文件转为 base64 data URI"""
    with open(image_path, "rb") as f:
        data = base64.b64encode(f.read()).decode("utf-8")
    ext = os.path.splitext(image_path)[1].lower().lstrip(".")
    mime_map = {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
    }
    return f"data:{mime_map.get(ext, 'image/png')};base64,{data}"


def parse_input(user_input):
    """解析用户输入，支持 /image <路径> <问题> 格式"""
    if user_input.startswith("/image "):
        rest = user_input[7:].strip()
        # 支持带引号的路径
        if rest.startswith('"'):
            end_quote = rest.find('"', 1)
            if end_quote != -1:
                image_path = rest[1:end_quote]
                text = rest[end_quote + 1:].strip() or "描述这张图片"
                return image_path, text
        parts = rest.split(maxsplit=1)
        if parts:
            image_path = parts[0]
            text = parts[1] if len(parts) > 1 else "描述这张图片"
            return image_path, text
    return None, user_input


# --- 多轮对话 ---
messages = []
print("\n开始对话（输入 quit 退出）")
print("发送图片: /image <图片路径> <问题>")
print('例如: /image test.png 这张图片里有什么?\n')

while True:
    user_input = input("You: ").strip()
    if not user_input or user_input.lower() == "quit":
        break

    image_path, text = parse_input(user_input)

    if image_path:
        if not os.path.exists(image_path):
            print(f"错误: 图片不存在 - {image_path}\n")
            continue
        if chat_handler is None:
            print("错误: 未加载视觉编码器 (mmproj)，无法处理图片\n")
            continue
        data_uri = image_to_data_uri(image_path)
        content = [
            {"type": "image_url", "image_url": {"url": data_uri}},
            {"type": "text", "text": text},
        ]
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": text})

    try:
        response = llm.create_chat_completion(
            messages=messages,
            max_tokens=4096,
        )
    except Exception as e:
        err_msg = str(e).lower()
        if "context" in err_msg or "token" in err_msg or "too long" in err_msg or "exceed" in err_msg:
            print(f"⚠ 上下文长度不足，正在裁剪早期对话重试...")
            # 保留最新一条用户消息，逐步删除最早的对话
            while len(messages) > 1:
                messages.pop(0)
                try:
                    response = llm.create_chat_completion(
                        messages=messages,
                        max_tokens=4096,
                    )
                    break
                except Exception:
                    continue
            else:
                print(f"错误: 即使只保留最后一条消息仍然超长，请缩短输入或使用更小的图片\n")
                messages.pop()  # 移除刚加入的那条无法处理的消息
                continue
        else:
            print(f"错误: {e}\n")
            messages.pop()  # 移除失败的消息，保持对话历史干净
            continue

    assistant_text = response["choices"][0]["message"]["content"]
    messages.append({"role": "assistant", "content": assistant_text})
    print(f"AI: {assistant_text}\n")
