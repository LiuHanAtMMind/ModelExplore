#!/usr/bin/env python3
"""
eval_models.py - 工业相机小模型评估测试框架
在 Jetson 上运行，使用 model_chat 的 single-shot 模式 (--prompt)

用法:
    python3 eval_models.py [--model MODEL_PATH] [--all]
"""

import os
import sys
import time
import json
import subprocess
from pathlib import Path
from datetime import datetime

# ==================== 配置 ====================

import platform
_arch = platform.machine().lower()
if _arch in ("x86_64", "amd64", "x64"):
    BIN = "./model_chat/install/x64/bin/model_chat"
else:
    BIN = "./model_chat/install/jetson/bin/model_chat"
MODEL_DIR = "./models"
TEST_IMG_DIR = "./model_test"
RESULT_DIR = "./eval_results"
TEST_CASES_FILE = Path(__file__).with_name("eval_test_cases.json")
TIMEOUT_PER_TEST = 600  # 秒
MAX_RESPONSE_TOKENS = 16384
MAX_CTX_TOKENS = 16384

# ==================== 测试用例 ====================

def load_test_cases(case_file):
    """从 JSON 文件加载测试用例，返回 [(category, name, prompt, image_path)]"""

    try:
        with open(case_file, "r", encoding="utf-8") as f:
            raw_cases = json.load(f)
    except FileNotFoundError as exc:
        raise SystemExit(f"未找到测试用例文件: {case_file}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"测试用例 JSON 解析失败: {case_file} ({exc})") from exc

    if not isinstance(raw_cases, list):
        raise SystemExit(f"测试用例文件格式错误: 顶层必须是数组 ({case_file})")

    cases = []
    for index, item in enumerate(raw_cases, start=1):
        if not isinstance(item, dict):
            raise SystemExit(f"测试用例第 {index} 项格式错误: 必须是对象")

        missing_fields = [field for field in ("category", "name", "prompt") if field not in item]
        if missing_fields:
            raise SystemExit(
                f"测试用例第 {index} 项缺少字段: {', '.join(missing_fields)}"
            )

        image = item.get("image")
        if image:
            image = (Path(TEST_IMG_DIR) / image).as_posix()
        else:
            image = None

        cases.append((item["category"], item["name"], item["prompt"], image))

    return cases


TEST_CASES = load_test_cases(TEST_CASES_FILE)


def run_test_case(model_path, category, name, prompt, image_path, timeout=TIMEOUT_PER_TEST):
    """运行单个测试用例（single-shot 模式），返回 (输出文本, 耗时ms, 成功与否)"""

    cmd = [BIN, "-m", model_path,
           "-n", str(MAX_RESPONSE_TOKENS),
           "--ctx-size", str(MAX_CTX_TOKENS),
           "--use-direct-io",
           "-p", prompt]

    if image_path and os.path.isfile(image_path):
        cmd += ["--image", image_path]

    start_time = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        elapsed_ms = int((time.time() - start_time) * 1000)

        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if result.returncode != 0:
            err_info = stderr[-500:] if stderr else "(无 stderr)"
            return f"(进程退出码={result.returncode})\n{err_info}", elapsed_ms, False

        if not stdout:
            return f"(输出为空)\nstderr: {stderr[-300:]}", elapsed_ms, False

        return stdout, elapsed_ms, True

    except subprocess.TimeoutExpired:
        elapsed_ms = int((time.time() - start_time) * 1000)
        return "(超时)", elapsed_ms, False
    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        return f"(异常: {e})", elapsed_ms, False


def find_models(model_dir):
    """扫描模型目录"""
    models = []
    for root, dirs, files in os.walk(model_dir):
        for f in files:
            if f.endswith(".gguf") and "mmproj" not in f:
                models.append(os.path.join(root, f))
    return sorted(models)


def run_evaluation(models_to_test=None):
    """执行完整评估"""
    os.makedirs(RESULT_DIR, exist_ok=True)

    if models_to_test is None:
        models_to_test = find_models(MODEL_DIR)

    if not models_to_test:
        print("未找到模型文件")
        return

    print("=" * 60)
    print(" 工业相机小模型评估测试")
    print("=" * 60)
    print(f"\n模型数量: {len(models_to_test)}")
    for i, m in enumerate(models_to_test):
        print(f"  {i+1}. {m}")
    print(f"\n测试用例数: {len(TEST_CASES)}")
    print(f"结果目录: {RESULT_DIR}/")
    print()

    for model_path in models_to_test:
        model_name = Path(model_path).stem
        result_file = os.path.join(RESULT_DIR, f"{model_name}_eval.md")

        print("=" * 60)
        print(f" 正在测试: {model_name}")
        print("=" * 60)

        # 统计
        stats = {"total": 0, "success": 0, "failed": 0, "total_ms": 0}
        category_stats = {}

        with open(result_file, "w", encoding="utf-8") as f:
            f.write(f"# 模型评估报告: {model_name}\n\n")
            f.write(f"- 模型路径: {model_path}\n")
            f.write(f"- 测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"- 测试用例数: {len(TEST_CASES)}\n\n")
            f.write("---\n\n")

            for i, (category, name, prompt, image) in enumerate(TEST_CASES):
                stats["total"] += 1
                print(f"\n  [{i+1}/{len(TEST_CASES)}] [{category}] {name} ...", end="", flush=True)

                response, elapsed_ms, success = run_test_case(
                    model_path, category, name, prompt, image
                )

                stats["total_ms"] += elapsed_ms
                if success:
                    stats["success"] += 1
                    print(f" OK ({elapsed_ms}ms)")
                else:
                    stats["failed"] += 1
                    print(f" FAIL ({elapsed_ms}ms)")

                # 打印输入
                print(f"    --- 输入 ---")
                print(f"    提问: {prompt}")
                if image:
                    print(f"    图片: {image}")
                print(f"    --- 输出 ---")
                for line in response.split("\n")[:30]:
                    print(f"    {line}")
                if response.count("\n") > 30:
                    print(f"    ... (共 {response.count(chr(10))+1} 行)")
                print(f"    --- END ---", flush=True)

                # 分类统计
                if category not in category_stats:
                    category_stats[category] = {"total": 0, "success": 0, "total_ms": 0}
                category_stats[category]["total"] += 1
                category_stats[category]["total_ms"] += elapsed_ms
                if success:
                    category_stats[category]["success"] += 1

                # 写入结果（每条立即写入并刷新）
                escaped_response = response.replace("```", r"\`\`\`")
                f.write(f"## [{category}] {name}\n\n")
                f.write(f"- 耗时: {elapsed_ms}ms\n")
                f.write(f"- 状态: {'✓ 成功' if success else '✗ 失败'}\n")
                if image:
                    f.write(f"- 图片: {image}\n")
                f.write(f"- 提问: {prompt}\n\n")
                f.write("**模型回答:**\n\n")
                f.write(f"````markdown\n{escaped_response}\n````\n\n")
                f.write("---\n\n")
                f.flush()

            # 写入汇总
            f.write("\n# 评估汇总\n\n")
            f.write(f"| 指标 | 值 |\n|---|---|\n")
            f.write(f"| 总用例数 | {stats['total']} |\n")
            f.write(f"| 成功数 | {stats['success']} |\n")
            f.write(f"| 失败数 | {stats['failed']} |\n")
            f.write(f"| 成功率 | {stats['success']*100//stats['total']}% |\n")
            f.write(f"| 总耗时 | {stats['total_ms']//1000}s |\n")
            f.write(f"| 平均耗时 | {stats['total_ms']//stats['total']}ms |\n\n")

            f.write("### 分类统计\n\n")
            f.write("| 类别 | 成功/总数 | 平均耗时 |\n|---|---|---|\n")
            for cat, cs in category_stats.items():
                avg_ms = cs["total_ms"] // cs["total"] if cs["total"] > 0 else 0
                f.write(f"| {cat} | {cs['success']}/{cs['total']} | {avg_ms}ms |\n")

        print(f"\n  结果已保存: {result_file}")
        print(f"  成功率: {stats['success']}/{stats['total']} ({stats['success']*100//stats['total']}%)")
        print(f"  平均响应时间: {stats['total_ms']//stats['total']}ms")

    print("\n" + "=" * 60)
    print(" 所有测试完成！")
    print(f" 结果目录: {RESULT_DIR}/")
    print("=" * 60)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="工业相机小模型评估测试")
    parser.add_argument("--model", "-m", help="指定单个模型路径")
    parser.add_argument("--all", action="store_true", help="测试所有模型")
    parser.add_argument("--category", "-c", help="只测试指定类别")
    parser.add_argument("--start", "-s", type=int, default=1, help="从第N个用例开始 (1-based)")
    args = parser.parse_args()

    if args.category:
        TEST_CASES = [tc for tc in TEST_CASES if tc[0] == args.category]
        if not TEST_CASES:
            print(f"未找到类别: {args.category}")
            print("可用类别:", set(tc[0] for tc in TEST_CASES))
            sys.exit(1)

    if args.start > 1:
        TEST_CASES = TEST_CASES[args.start - 1:]

    if args.model:
        run_evaluation([args.model])
    else:
        run_evaluation()
