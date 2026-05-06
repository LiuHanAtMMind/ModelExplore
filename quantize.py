import os
import argparse
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

def quantize_and_save(model_dir, output_dir, quant_type="bnb-4bit"):
    if quant_type == "bnb-4bit":
        quant_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype="float16"
        )
    elif quant_type == "bnb-8bit":
        quant_config = BitsAndBytesConfig(
            load_in_8bit=True,
            bnb_8bit_quant_type="int8",
        )
    else:
        raise ValueError("Only bnb-4bit and bnb-8bit are supported.")

    import torch
    if not torch.cuda.is_available():
        raise RuntimeError("No GPU found! Please run this script on a machine with CUDA GPU for quantization.")
    print(f"Loading model from {model_dir} with quantization: {quant_type} on GPU")
    model = AutoModelForCausalLM.from_pretrained(
        model_dir,
        device_map={"": "cuda:0"},
        quantization_config=quant_config
    )
    tokenizer = AutoTokenizer.from_pretrained(model_dir)

    os.makedirs(output_dir, exist_ok=True)
    print(f"Saving quantized model to {output_dir}")
    model.save_pretrained(output_dir)
    tokenizer.save_pretrained(output_dir)

    # 自动拷贝原模型目录下所有未被保存的文件
    import shutil
    saved_files = set(os.listdir(output_dir))
    for fname in os.listdir(model_dir):
        src = os.path.join(model_dir, fname)
        dst = os.path.join(output_dir, fname)
        # 只拷贝文件（不拷贝子目录），且目标目录不存在该文件
        if os.path.isfile(src) and fname not in saved_files:
            shutil.copy2(src, dst)
            print(f"Copied {fname} to {output_dir}")
    print("Quantization and save complete.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Quantize a HuggingFace model with bitsandbytes and save it.")
    parser.add_argument('--model-dir', type=str, required=True, help='Path to original model directory')
    parser.add_argument('--output-dir', type=str, required=True, help='Path to save quantized model')
    parser.add_argument('--quant-type', type=str, default="bnb-4bit", choices=["bnb-4bit", "bnb-8bit"], help='Quantization type')
    args = parser.parse_args()

    # 检查bitsandbytes和torch环境
    print("Checking bitsandbytes and torch environment...")
    try:
        import torch
        import bitsandbytes as bnb
        print(f"torch version: {torch.__version__}")
        print(f"bitsandbytes version: {bnb.__version__}")
        cuda_ok = torch.cuda.is_available()
        print(f"CUDA available: {cuda_ok}")
        # 检查4bit/8bit支持
        bnb4 = hasattr(bnb.nn, 'Linear4bit')
        bnb8 = hasattr(bnb.nn, 'Linear8bitLt')
        print(f"bitsandbytes 4bit support: {bnb4}")
        print(f"bitsandbytes 8bit support: {bnb8}")
        if not cuda_ok:
            print("[警告] 当前环境未检测到CUDA，量化和推理将无法使用GPU！")
        if args.quant_type == "bnb-4bit" and not bnb4:
            print("[警告] 当前bitsandbytes不支持4bit量化！")
        if args.quant_type == "bnb-8bit" and not bnb8:
            print("[警告] 当前bitsandbytes不支持8bit量化！")
    except Exception as e:
        print(f"[错误] 检查bitsandbytes/torch环境失败: {e}")

    quantize_and_save(args.model_dir, args.output_dir, args.quant_type)
