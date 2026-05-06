#!/bin/bash
# build.sh - Jetson 原生构建脚本
#
# 两步构建:
#   1. 在 llama.cpp 目录下构建并 install → llama.cpp/install/jetson/
#   2. qwen_chat 通过 find_package 链接预构建的 llama.cpp
#
# 用法:
#   ./build.sh                    # 构建全部
#   ./build.sh llama              # 只构建 llama.cpp
#   ./build.sh app                # 只构建 qwen_chat (需先构建 llama)
#   CLEAN=1 ./build.sh            # 清除后重新构建
#   CUDA=0 ./build.sh             # 不启用 CUDA

set -e

TARGET="${1:-all}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 检测 LLAMA_CPP_DIR
if [ -z "$LLAMA_CPP_DIR" ]; then
    DEFAULT_DIR="$(dirname "$SCRIPT_DIR")/llama.cpp"
    if [ -d "$DEFAULT_DIR" ]; then
        LLAMA_CPP_DIR="$DEFAULT_DIR"
    else
        echo "错误: 请设置 LLAMA_CPP_DIR 环境变量"
        echo "  export LLAMA_CPP_DIR=/path/to/llama.cpp"
        exit 1
    fi
fi

LLAMA_BUILD_DIR="$LLAMA_CPP_DIR/build/jetson"
LLAMA_INSTALL_DIR="$LLAMA_CPP_DIR/install/jetson"
APP_BUILD_DIR="$SCRIPT_DIR/build/jetson"

CUDA_FLAG=""
if [ "${CUDA:-1}" != "0" ]; then
    CUDA_FLAG="-DGGML_CUDA=ON"
fi

# ============================================================
# Step 1: 构建 llama.cpp 并 install
# ============================================================
build_llama() {
    echo ""
    echo "===================================="
    echo "  [Step 1] 构建 llama.cpp (jetson)"
    echo "  源码: $LLAMA_CPP_DIR"
    echo "  构建: $LLAMA_BUILD_DIR"
    echo "  安装: $LLAMA_INSTALL_DIR"
    echo "===================================="

    if [ "${CLEAN:-0}" = "1" ]; then
        [ -d "$LLAMA_BUILD_DIR" ]   && rm -rf "$LLAMA_BUILD_DIR"
        [ -d "$LLAMA_INSTALL_DIR" ] && rm -rf "$LLAMA_INSTALL_DIR"
    fi

    mkdir -p "$LLAMA_BUILD_DIR"

    local cmake_args=(
        -B "$LLAMA_BUILD_DIR"
        -S "$LLAMA_CPP_DIR"
        -DCMAKE_BUILD_TYPE=Release
        -DCMAKE_INSTALL_PREFIX="$LLAMA_INSTALL_DIR"
        -DLLAMA_FLASH_ATTN=ON
    )
    [ -n "$CUDA_FLAG" ] && cmake_args+=("$CUDA_FLAG")

    echo ""
    echo "[cmake configure]"
    cmake "${cmake_args[@]}"

    echo ""
    echo "[cmake build]"
    cmake --build "$LLAMA_BUILD_DIR" --config Release -j "$(nproc)"

    echo ""
    echo "[cmake install] (强制覆盖: 先清空 $LLAMA_INSTALL_DIR)"
    [ -d "$LLAMA_INSTALL_DIR" ] && rm -rf "$LLAMA_INSTALL_DIR"
    cmake --install "$LLAMA_BUILD_DIR" --config Release

    echo ""
    echo "[OK] llama.cpp 构建 + 安装完成!"
    echo "  安装目录: $LLAMA_INSTALL_DIR"
}

# ============================================================
# Step 2: 构建 qwen_chat
# ============================================================
build_app() {
    if [ ! -d "$LLAMA_INSTALL_DIR/lib/cmake/llama" ]; then
        echo "错误: llama.cpp 尚未构建/安装, 请先运行: $0 llama"
        exit 1
    fi

    echo ""
    echo "===================================="
    echo "  [Step 2] 构建 qwen_chat (jetson)"
    echo "  源码: $SCRIPT_DIR"
    echo "  构建: $APP_BUILD_DIR"
    echo "  链接: $LLAMA_INSTALL_DIR"
    echo "===================================="

    if [ "${CLEAN:-0}" = "1" ] && [ -d "$APP_BUILD_DIR" ]; then
        rm -rf "$APP_BUILD_DIR"
    fi

    mkdir -p "$APP_BUILD_DIR"

    local cmake_args=(
        -B "$APP_BUILD_DIR"
        -S "$SCRIPT_DIR"
        -DLLAMA_INSTALL_DIR="$LLAMA_INSTALL_DIR"
        -DTARGET_PLATFORM=jetson
        -DCMAKE_BUILD_TYPE=Release
    )

    echo ""
    echo "[cmake configure]"
    cmake "${cmake_args[@]}"

    echo ""
    echo "[cmake build]"
    cmake --build "$APP_BUILD_DIR" --config Release -j "$(nproc)"

    # install: 可执行文件 + 依赖 .so → install/jetson/bin/
    local app_install_dir="$SCRIPT_DIR/install/jetson"
    echo ""
    echo "[cmake install] (强制覆盖: 先清空 $app_install_dir)"
    [ -d "$app_install_dir" ] && rm -rf "$app_install_dir"
    cmake --install "$APP_BUILD_DIR" --config Release --prefix "$app_install_dir"

    echo ""
    echo "[OK] qwen_chat 构建完成!"
    echo "  产物: $app_install_dir/bin/qwen_chat (含依赖 .so)"
}

# ---- 执行 ----
case "$TARGET" in
    all)   build_llama; build_app ;;
    llama) build_llama ;;
    app)   build_app ;;
    *)
        echo "用法: $0 {all|llama|app}"
        exit 1
        ;;
esac
