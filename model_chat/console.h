// console.h - 轻量 readline 风格输入 + 状态栏 (跨平台)
// 支持: 左右移动, Home/End, Backspace/Delete, 上下历史, Ctrl+C 即时中断
//       终端最后一行常驻状态栏 (滚动区域方案)
#pragma once

#include <string>
#include <vector>

namespace console {

// 初始化终端 (raw mode + 滚动区域), 必须在使用 readline 前调用
void init();

// 恢复终端原始模式 (包括恢复全屏滚动区域)
void cleanup();

// 读取一行输入. 支持行编辑和历史浏览.
// 返回值:
//   true  = 正常读取到一行 (结果在 line 中, 不含换行符)
//   false = 被 Ctrl+C 中断或 EOF
bool readline(const char *prompt, std::string &line);

// 更新状态栏内容 (绿底白字, 最后一行)
void status_draw(const char *text);

// 清除状态栏
void status_clear();

} // namespace console
