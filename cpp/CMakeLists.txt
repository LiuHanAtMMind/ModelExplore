cmake_minimum_required(VERSION 3.14)
project(qwen_chat LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# ============================================================
# 平台检测
#   TARGET_PLATFORM 由构建脚本传入: "x64" 或 "jetson"
# ============================================================

if(NOT DEFINED TARGET_PLATFORM)
    if(CMAKE_SYSTEM_PROCESSOR MATCHES "aarch64|ARM64")
        set(TARGET_PLATFORM "jetson")
    else()
        set(TARGET_PLATFORM "x64")
    endif()
endif()

message(STATUS "========================================")
message(STATUS "  目标平台: ${TARGET_PLATFORM}")
message(STATUS "  架构:     ${CMAKE_SYSTEM_PROCESSOR}")
message(STATUS "  系统:     ${CMAKE_SYSTEM_NAME}")
message(STATUS "========================================")

# ============================================================
# 链接预构建的 llama.cpp
#
# LLAMA_INSTALL_DIR: llama.cpp 的 cmake --install 输出目录
#   构建脚本会自动设置, 也可手动:
#   cmake -DLLAMA_INSTALL_DIR=/path/to/llama-install ...
# ============================================================

if(NOT DEFINED LLAMA_INSTALL_DIR)
    message(FATAL_ERROR
        "请设置 LLAMA_INSTALL_DIR 指向 llama.cpp 的安装目录:\n"
        "  先构建 llama.cpp:\n"
        "    cmake -B llama.cpp/build/x64 -S llama.cpp\n"
        "    cmake --build llama.cpp/build/x64 --config Release\n"
        "    cmake --install llama.cpp/build/x64 --prefix llama.cpp/install/x64\n"
        "  再构建本项目:\n"
        "    cmake -B build/x64 -DLLAMA_INSTALL_DIR=llama.cpp/install/x64")
endif()

message(STATUS "  llama.cpp 安装目录: ${LLAMA_INSTALL_DIR}")

# find_package(llama) 会自动找到 ggml
list(APPEND CMAKE_PREFIX_PATH "${LLAMA_INSTALL_DIR}")
find_package(llama REQUIRED)
message(STATUS "  llama 版本: ${LLAMA_VERSION}")
message(STATUS "  llama 头文件: ${LLAMA_INCLUDE_DIR}")
message(STATUS "  llama 库目录: ${LLAMA_LIB_DIR}")

# mtmd 没有 cmake package config, 手动查找
find_library(MTMD_LIBRARY NAMES mtmd HINTS "${LLAMA_INSTALL_DIR}/lib" REQUIRED)
find_path(MTMD_INCLUDE_DIR NAMES mtmd.h HINTS "${LLAMA_INSTALL_DIR}/include" REQUIRED)

add_library(mtmd UNKNOWN IMPORTED)
set_target_properties(mtmd PROPERTIES
    IMPORTED_LOCATION "${MTMD_LIBRARY}"
    INTERFACE_INCLUDE_DIRECTORIES "${MTMD_INCLUDE_DIR}"
    INTERFACE_LINK_LIBRARIES "llama"
)
message(STATUS "  mtmd 库: ${MTMD_LIBRARY}")

# llama-common (Jinja 模板引擎 + chat template)
find_library(LLAMA_COMMON_LIBRARY NAMES llama-common HINTS "${LLAMA_INSTALL_DIR}/lib" REQUIRED)
add_library(llama-common UNKNOWN IMPORTED)
set_target_properties(llama-common PROPERTIES
    IMPORTED_LOCATION "${LLAMA_COMMON_LIBRARY}"
    INTERFACE_LINK_LIBRARIES "llama"
)
message(STATUS "  llama-common 库: ${LLAMA_COMMON_LIBRARY}")

# llama.cpp 源码目录 (用于 common/ 和 vendor/ 头文件)
if(NOT DEFINED LLAMA_CPP_SOURCE_DIR)
    # 默认: LLAMA_INSTALL_DIR 的上两级 (install/x64/ -> llama.cpp/)
    get_filename_component(LLAMA_CPP_SOURCE_DIR "${LLAMA_INSTALL_DIR}/../.." ABSOLUTE)
endif()
if(EXISTS "${LLAMA_CPP_SOURCE_DIR}/common/chat.h")
    message(STATUS "  llama.cpp 源码: ${LLAMA_CPP_SOURCE_DIR}")
else()
    message(FATAL_ERROR "找不到 llama.cpp 源码目录 (需要 common/chat.h):\n"
                        "  当前路径: ${LLAMA_CPP_SOURCE_DIR}\n"
                        "  请设置 -DLLAMA_CPP_SOURCE_DIR=/path/to/llama.cpp")
endif()

# ============================================================
# 平台特定配置
# ============================================================

if(TARGET_PLATFORM STREQUAL "jetson")
    add_definitions(-DPLATFORM_JETSON=1)
elseif(TARGET_PLATFORM STREQUAL "x64")
    add_definitions(-DPLATFORM_X64=1)
    if(MSVC)
        add_compile_options(/utf-8)
    endif()
endif()

# ============================================================
# 构建可执行文件
# ============================================================

add_executable(qwen_chat main.cpp console.cpp)

target_link_libraries(qwen_chat PRIVATE
    llama
    mtmd
    llama-common
)

# 添加 llama.cpp 源码中的 common/ 和 vendor/ 头文件路径
target_include_directories(qwen_chat PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}"
    "${LLAMA_CPP_SOURCE_DIR}/common"
    "${LLAMA_CPP_SOURCE_DIR}/vendor"
)

# Linux: 设置 RPATH 为 $ORIGIN, 让可执行文件在同级目录查找 .so
if(NOT WIN32)
    set_target_properties(qwen_chat PROPERTIES
        INSTALL_RPATH "$ORIGIN"
        BUILD_WITH_INSTALL_RPATH OFF
    )
endif()

# ============================================================
# 安装: 可执行文件 + 依赖的共享库 → 同一目录
# ============================================================

install(TARGETS qwen_chat RUNTIME DESTINATION bin)

# 收集依赖的 DLL / .so 并安装到 bin/ 目录
if(WIN32)
    file(GLOB _LLAMA_DLLS "${LLAMA_INSTALL_DIR}/bin/*.dll")
else()
    file(GLOB _LLAMA_DLLS "${LLAMA_INSTALL_DIR}/lib/*.so*")
endif()
# 将反斜杠统一为正斜杠, 避免 cmake install 时转义警告
if(_LLAMA_DLLS)
    set(_LLAMA_DLLS_FIXED)
    foreach(_f ${_LLAMA_DLLS})
        file(TO_CMAKE_PATH "${_f}" _f_fixed)
        list(APPEND _LLAMA_DLLS_FIXED "${_f_fixed}")
    endforeach()
    install(FILES ${_LLAMA_DLLS_FIXED} DESTINATION bin)
endif()

# Linux: 给安装到 bin/ 的 .so 文件 patch RPATH=$ORIGIN
# 因为 llama.cpp install 的 .so 默认没有 RPATH, 放到非标准目录后找不到彼此的依赖
if(NOT WIN32)
    find_program(PATCHELF_EXECUTABLE patchelf)
    if(PATCHELF_EXECUTABLE)
        install(CODE "
            file(GLOB _SO_FILES \"\${CMAKE_INSTALL_PREFIX}/bin/*.so\" \"\${CMAKE_INSTALL_PREFIX}/bin/*.so.*\")
            foreach(_so \${_SO_FILES})
                execute_process(COMMAND ${PATCHELF_EXECUTABLE} --set-rpath \$ORIGIN \${_so} ERROR_QUIET)
            endforeach()
            message(STATUS \"patched RPATH for \${_SO_FILES}\")
        ")
    else()
        message(WARNING "patchelf 未找到, install 后的 .so 可能缺少 RPATH, 请安装: sudo apt install patchelf")
    endif()
endif()
