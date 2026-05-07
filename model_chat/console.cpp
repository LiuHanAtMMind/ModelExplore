// console.cpp - 轻量 readline 风格输入实现
// 特性:
//   - 左右方向键移动光标
//   - Home/End 跳转行首/行尾
//   - Backspace/Delete 删除
//   - 上下方向键浏览历史
//   - Ctrl+C 立即中断当前输入 (不等回车)
//   - Ctrl+D (空行时) = EOF
//   - UTF-8 感知
//   - 跨平台: Windows (Console API) / Linux (termios)
//
// 设计:
//   只在 readline() 内部使用 raw mode. 返回后终端恢复正常,
//   这样生成中的 Ctrl+C 仍能触发 SIGINT (由 main.cpp 的信号处理器捕获).

#include "console.h"

#include <algorithm>
#include <cstdio>
#include <cstring>

#ifdef _WIN32
#define NOMINMAX
#include <io.h>
#include <windows.h>

#else
#include <cerrno>
#include <poll.h>
#include <sys/ioctl.h>
#include <termios.h>
#include <unistd.h>
#include <wchar.h>

#endif

namespace console {

// forward declarations (状态栏, 定义在文件末尾)
static void screen_reserve_status_line();
static void screen_restore_full();

// ==================== 终端尺寸 ====================

static int g_term_rows = 24;
static int g_term_cols = 80;

static void query_terminal_size() {
#ifdef _WIN32
  CONSOLE_SCREEN_BUFFER_INFO csbi;
  if (GetConsoleScreenBufferInfo(GetStdHandle(STD_OUTPUT_HANDLE), &csbi)) {
    g_term_rows = csbi.srWindow.Bottom - csbi.srWindow.Top + 1;
    g_term_cols = csbi.srWindow.Right - csbi.srWindow.Left + 1;
  }
#else
  struct winsize ws;
  if (ioctl(STDOUT_FILENO, TIOCGWINSZ, &ws) == 0) {
    if (ws.ws_row > 0)
      g_term_rows = ws.ws_row;
    if (ws.ws_col > 0)
      g_term_cols = ws.ws_col;
  }
#endif
}

// ==================== 平台抽象 ====================

#ifdef _WIN32
static HANDLE hStdin = INVALID_HANDLE_VALUE;
static HANDLE hStdout = INVALID_HANDLE_VALUE;
static DWORD orig_mode_in = 0;
static DWORD orig_mode_out = 0;
static bool initialized = false;

void init() {
  if (initialized)
    return;
  hStdin = GetStdHandle(STD_INPUT_HANDLE);
  hStdout = GetStdHandle(STD_OUTPUT_HANDLE);
  GetConsoleMode(hStdin, &orig_mode_in);
  GetConsoleMode(hStdout, &orig_mode_out);

  // 启用虚拟终端处理 (ANSI escape 支持)
  DWORD mode_out = orig_mode_out | ENABLE_VIRTUAL_TERMINAL_PROCESSING;
  SetConsoleMode(hStdout, mode_out);

  initialized = true;
  screen_reserve_status_line();
}

void cleanup() {
  if (!initialized)
    return;
  screen_restore_full();
  SetConsoleMode(hStdin, orig_mode_in);
  SetConsoleMode(hStdout, orig_mode_out);
  initialized = false;
}

static void enter_raw_mode() {
  DWORD mode_in = orig_mode_in;
  mode_in &= ~(ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT | ENABLE_PROCESSED_INPUT);
  SetConsoleMode(hStdin, mode_in);
}

static void leave_raw_mode() { SetConsoleMode(hStdin, orig_mode_in); }

enum SpecialKey {
  KEY_NONE = 0,
  KEY_LEFT = -100,
  KEY_RIGHT = -101,
  KEY_UP = -102,
  KEY_DOWN = -103,
  KEY_HOME = -104,
  KEY_END = -105,
  KEY_DELETE = -106,
  KEY_BACKSPACE = -107,
  KEY_ENTER = -108,
  KEY_CTRL_C = -109,
  KEY_CTRL_D = -110,
  KEY_EOF = -111,
};

static int read_key() {
  INPUT_RECORD rec;
  DWORD count;
  while (true) {
    if (!ReadConsoleInputW(hStdin, &rec, 1, &count) || count == 0) {
      return KEY_EOF;
    }
    if (rec.EventType != KEY_EVENT || !rec.Event.KeyEvent.bKeyDown) {
      continue;
    }
    auto &ke = rec.Event.KeyEvent;
    wchar_t ch = ke.uChar.UnicodeChar;
    WORD vk = ke.wVirtualKeyCode;

    if (ch == 3)
      return KEY_CTRL_C; // Ctrl+C
    if (ch == 4)
      return KEY_CTRL_D; // Ctrl+D
    if (ch == '\r' || ch == '\n')
      return KEY_ENTER;
    if (ch == 8 || ch == 127)
      return KEY_BACKSPACE;

    if (ch == 0) {
      switch (vk) {
      case VK_LEFT:
        return KEY_LEFT;
      case VK_RIGHT:
        return KEY_RIGHT;
      case VK_UP:
        return KEY_UP;
      case VK_DOWN:
        return KEY_DOWN;
      case VK_HOME:
        return KEY_HOME;
      case VK_END:
        return KEY_END;
      case VK_DELETE:
        return KEY_DELETE;
      default:
        continue;
      }
    }
    return (int)ch;
  }
}

#else // POSIX

static struct termios orig_termios;
static bool initialized = false;

void init() {
  if (initialized)
    return;
  tcgetattr(STDIN_FILENO, &orig_termios);
  initialized = true;
  screen_reserve_status_line();
}

void cleanup() {
  if (!initialized)
    return;
  screen_restore_full();
  tcsetattr(STDIN_FILENO, TCSAFLUSH, &orig_termios);
  initialized = false;
}

static void enter_raw_mode() {
  struct termios raw = orig_termios;
  raw.c_iflag &= ~(BRKINT | ICRNL | INPCK | ISTRIP | IXON);
  raw.c_oflag |= OPOST; // 保留输出处理 (\n → \r\n)
  raw.c_cflag |= CS8;
  // 禁用 ISIG: Ctrl+C 作为普通字节 0x03 读取 (立即处理)
  raw.c_lflag &= ~(ECHO | ICANON | IEXTEN | ISIG);
  raw.c_cc[VMIN] = 0;
  raw.c_cc[VTIME] = 0;
  tcsetattr(STDIN_FILENO, TCSAFLUSH, &raw);
}

static void leave_raw_mode() {
  tcsetattr(STDIN_FILENO, TCSAFLUSH, &orig_termios);
}

enum SpecialKey {
  KEY_NONE = 0,
  KEY_LEFT = -100,
  KEY_RIGHT = -101,
  KEY_UP = -102,
  KEY_DOWN = -103,
  KEY_HOME = -104,
  KEY_END = -105,
  KEY_DELETE = -106,
  KEY_BACKSPACE = -107,
  KEY_ENTER = -108,
  KEY_CTRL_C = -109,
  KEY_CTRL_D = -110,
  KEY_EOF = -111,
};

// 等待并读取一个字节
static int read_byte_wait() {
  while (true) {
    struct pollfd pfd;
    pfd.fd = STDIN_FILENO;
    pfd.events = POLLIN;
    int ret = poll(&pfd, 1, -1); // 无限等待
    if (ret < 0) {
      if (errno == EINTR)
        continue;
      return -2; // 错误
    }
    if (ret == 0)
      continue;
    unsigned char c;
    ssize_t n = read(STDIN_FILENO, &c, 1);
    if (n <= 0)
      return -2; // EOF
    return c;
  }
}

// 尝试非阻塞读取一个字节 (用于读取 escape sequence 后续字节)
static int read_byte_nonblock() {
  unsigned char c;
  ssize_t n = read(STDIN_FILENO, &c, 1);
  if (n <= 0)
    return -1;
  return c;
}

static int read_key() {
  int c = read_byte_wait();
  if (c == -2)
    return KEY_EOF;

  if (c == 3)
    return KEY_CTRL_C;
  if (c == 4)
    return KEY_CTRL_D;
  if (c == 13 || c == 10)
    return KEY_ENTER;
  if (c == 127 || c == 8)
    return KEY_BACKSPACE;

  if (c == 27) {
    // Escape sequence
    int c2 = read_byte_nonblock();
    if (c2 < 0)
      return 27; // bare Escape
    if (c2 == '[') {
      int c3 = read_byte_nonblock();
      if (c3 < 0)
        return 27;
      switch (c3) {
      case 'A':
        return KEY_UP;
      case 'B':
        return KEY_DOWN;
      case 'C':
        return KEY_RIGHT;
      case 'D':
        return KEY_LEFT;
      case 'H':
        return KEY_HOME;
      case 'F':
        return KEY_END;
      case '3': {
        int c4 = read_byte_nonblock();
        if (c4 == '~')
          return KEY_DELETE;
        return KEY_NONE;
      }
      case '1':
      case '7': {
        int c4 = read_byte_nonblock();
        if (c4 == '~')
          return KEY_HOME;
        return KEY_NONE;
      }
      case '4':
      case '8': {
        int c4 = read_byte_nonblock();
        if (c4 == '~')
          return KEY_END;
        return KEY_NONE;
      }
      default:
        return KEY_NONE;
      }
    } else if (c2 == 'O') {
      int c3 = read_byte_nonblock();
      if (c3 == 'H')
        return KEY_HOME;
      if (c3 == 'F')
        return KEY_END;
      return KEY_NONE;
    }
    return KEY_NONE;
  }

  return c;
}

#endif // POSIX

// ==================== UTF-8 工具 ====================

static size_t utf8_char_len(unsigned char c) {
  if ((c & 0x80) == 0)
    return 1;
  if ((c & 0xE0) == 0xC0)
    return 2;
  if ((c & 0xF0) == 0xE0)
    return 3;
  if ((c & 0xF8) == 0xF0)
    return 4;
  return 1;
}

// 字符数 (非字节数)
static size_t utf8_strlen(const std::string &s) {
  size_t count = 0;
  for (size_t i = 0; i < s.size();) {
    i += utf8_char_len((unsigned char)s[i]);
    count++;
  }
  return count;
}

// 第 n 个字符的字节偏移
static size_t utf8_byte_offset(const std::string &s, size_t char_idx) {
  size_t byte = 0;
  for (size_t i = 0; i < char_idx && byte < s.size(); i++) {
    byte += utf8_char_len((unsigned char)s[byte]);
  }
  return byte;
}

// 从字节偏移取一个完整 UTF-8 字符的字节数
static size_t utf8_char_bytes_at(const std::string &s, size_t byte_pos) {
  if (byte_pos >= s.size())
    return 0;
  return utf8_char_len((unsigned char)s[byte_pos]);
}

// 解码一个 UTF-8 字符为 Unicode 码点
static uint32_t utf8_decode(const char *s, size_t len) {
  if (len == 0)
    return 0;
  unsigned char c = (unsigned char)s[0];
  if (len == 1)
    return c;
  if (len == 2)
    return ((c & 0x1F) << 6) | (s[1] & 0x3F);
  if (len == 3)
    return ((c & 0x0F) << 12) | ((s[1] & 0x3F) << 6) | (s[2] & 0x3F);
  if (len == 4)
    return ((c & 0x07) << 18) | ((s[1] & 0x3F) << 12) | ((s[2] & 0x3F) << 6) |
           (s[3] & 0x3F);
  return 0;
}

// 计算一个 Unicode 码点的终端显示宽度 (不依赖 wcwidth, 避免 locale 问题)
static int char_display_width(uint32_t cp) {
  // 控制字符
  if (cp < 0x20 || (cp >= 0x7F && cp < 0xA0))
    return 0;
  // East Asian Wide / Fullwidth ranges
  if ((cp >= 0x1100 && cp <= 0x115F) ||   // Hangul Jamo
      cp == 0x2329 || cp == 0x232A ||     // angle brackets
      (cp >= 0x2E80 && cp <= 0x303E) ||   // CJK Radicals + Ideographic
      (cp >= 0x3040 && cp <= 0x33BF) ||   // Hiragana..CJK Compat
      (cp >= 0x3400 && cp <= 0x4DBF) ||   // CJK Ext A
      (cp >= 0x4E00 && cp <= 0x9FFF) ||   // CJK Unified Ideographs
      (cp >= 0xA000 && cp <= 0xA4CF) ||   // Yi
      (cp >= 0xAC00 && cp <= 0xD7AF) ||   // Hangul Syllables
      (cp >= 0xF900 && cp <= 0xFAFF) ||   // CJK Compat Ideographs
      (cp >= 0xFE10 && cp <= 0xFE19) ||   // Vertical forms
      (cp >= 0xFE30 && cp <= 0xFE6F) ||   // CJK Compat Forms
      (cp >= 0xFF01 && cp <= 0xFF60) ||   // Fullwidth Latin
      (cp >= 0xFFE0 && cp <= 0xFFE6) ||   // Fullwidth Signs
      (cp >= 0x1F300 && cp <= 0x1F9FF) || // Emoji
      (cp >= 0x20000 && cp <= 0x2FFFD) || // CJK Ext B+
      (cp >= 0x30000 && cp <= 0x3FFFD))   // CJK Ext G+
    return 2;
  return 1;
}

// 计算字符串的终端显示宽度 (考虑中文等宽字符)
static int str_display_width(const std::string &s,
                             size_t byte_end = (size_t)-1) {
  int width = 0;
  size_t end = std::min(byte_end, s.size());
  for (size_t i = 0; i < end;) {
    size_t clen = utf8_char_len((unsigned char)s[i]);
    if (i + clen > end)
      break;
    uint32_t cp = utf8_decode(s.c_str() + i, clen);
    width += char_display_width(cp);
    i += clen;
  }
  return width;
}

// ==================== 输出工具 ====================

static void write_str(const char *s, size_t len) {
#ifdef _WIN32
  DWORD written;
  WriteConsoleA(hStdout, s, (DWORD)len, &written, nullptr);
#else
  // 循环写入以处理短写
  size_t off = 0;
  while (off < len) {
    ssize_t n = write(STDOUT_FILENO, s + off, len - off);
    if (n < 0) {
      if (errno == EINTR)
        continue;
      break;
    }
    off += (size_t)n;
  }
#endif
}

static void write_str(const char *s) { write_str(s, strlen(s)); }

// 输出缓冲: 将多次写入合并为一次, 减少 SSH 下的闪烁
static std::string g_outbuf;

static void buf_append(const char *s, size_t len) { g_outbuf.append(s, len); }

static void buf_append(const char *s) { g_outbuf.append(s); }

static void buf_flush() {
  if (!g_outbuf.empty()) {
    write_str(g_outbuf.c_str(), g_outbuf.size());
    g_outbuf.clear();
  }
}

// ==================== 历史 ====================

static std::vector<std::string> history;
static const size_t MAX_HISTORY = 200;

static void history_add(const std::string &line) {
  if (line.empty())
    return;
  if (!history.empty() && history.back() == line)
    return;
  history.push_back(line);
  if (history.size() > MAX_HISTORY) {
    history.erase(history.begin());
  }
}

// ==================== readline 实现 ====================

bool readline(const char *prompt, std::string &line) {
  line.clear();

  // 显示提示符 (在进入 raw mode 之前, 用正常模式输出)
  write_str(prompt);

  enter_raw_mode();

  int prompt_width = str_display_width(std::string(prompt)); // prompt 显示宽度

  std::string buf;
  size_t cursor = 0;                  // 字符位置 (非字节)
  int hist_idx = (int)history.size(); // 当前浏览位置
  std::string saved_buf;              // 浏览历史前保存当前输入

  int prev_lines = 0; // 上次 refresh 占了多少额外行 (wrap)

  auto refresh = [&]() {
    query_terminal_size();
    int cols = g_term_cols;
    if (cols < 1)
      cols = 80;

    g_outbuf.clear();

    // 如果上次渲染跨了多行, 先移到第一行
    if (prev_lines > 0) {
      char esc[32];
      snprintf(esc, sizeof(esc), "\033[%dA", prev_lines);
      buf_append(esc);
    }
    buf_append("\r");

    // 输出 prompt + buf (自然覆盖旧内容)
    buf_append(prompt);
    buf_append(buf.c_str(), buf.size());
    buf_append("\033[K"); // 清除最后一行残余

    // 计算总显示宽度, 确定占几行
    int total_width = prompt_width + str_display_width(buf);
    int total_lines = (total_width > 0) ? (total_width - 1) / cols : 0;

    // 如果之前占了更多行, 逐行清除多余行
    for (int i = total_lines; i < prev_lines; i++) {
      buf_append("\n\033[K");
    }
    // 如果清除了多余行, 回到内容末尾
    if (prev_lines > total_lines) {
      char esc[32];
      snprintf(esc, sizeof(esc), "\033[%dA", prev_lines - total_lines);
      buf_append(esc);
    }

    // 定位光标: 计算光标在第几行第几列
    size_t byte_pos = utf8_byte_offset(buf, cursor);
    int cursor_col = prompt_width + str_display_width(buf, byte_pos);
    int cursor_row = cursor_col / cols; // 光标所在的额外行数 (0=第一行)
    int cursor_x = cursor_col % cols;   // 光标在该行的列位置

    // 从末尾移到光标所在行
    int lines_back = total_lines - cursor_row;
    if (lines_back > 0) {
      char esc[32];
      snprintf(esc, sizeof(esc), "\033[%dA", lines_back);
      buf_append(esc);
    }
    // 光标列定位
    {
      char esc[32];
      snprintf(esc, sizeof(esc), "\033[%dG", cursor_x + 1);
      buf_append(esc);
    }

    prev_lines = total_lines;
    buf_flush();
  };

  while (true) {
    int key = read_key();

    if (key == KEY_CTRL_C) {
      // 立即中断: 输出换行, 返回 false
      write_str("\n");
      leave_raw_mode();
      line.clear();
      return false;
    }

    if (key == KEY_EOF || (key == KEY_CTRL_D && buf.empty())) {
      write_str("\n");
      leave_raw_mode();
      line.clear();
      return false;
    }

    if (key == KEY_ENTER) {
      write_str("\n");
      leave_raw_mode();
      line = buf;
      if (!buf.empty()) {
        history_add(buf);
      }
      return true;
    }

    if (key == KEY_BACKSPACE) {
      if (cursor > 0) {
        size_t byte_pos = utf8_byte_offset(buf, cursor - 1);
        size_t char_bytes = utf8_char_bytes_at(buf, byte_pos);
        buf.erase(byte_pos, char_bytes);
        cursor--;
        refresh();
      }
      continue;
    }

    if (key == KEY_DELETE) {
      if (cursor < utf8_strlen(buf)) {
        size_t byte_pos = utf8_byte_offset(buf, cursor);
        size_t char_bytes = utf8_char_bytes_at(buf, byte_pos);
        buf.erase(byte_pos, char_bytes);
        refresh();
      }
      continue;
    }

    if (key == KEY_LEFT) {
      if (cursor > 0) {
        cursor--;
        refresh();
      }
      continue;
    }

    if (key == KEY_RIGHT) {
      if (cursor < utf8_strlen(buf)) {
        cursor++;
        refresh();
      }
      continue;
    }

    if (key == KEY_HOME) {
      cursor = 0;
      refresh();
      continue;
    }

    if (key == KEY_END) {
      cursor = utf8_strlen(buf);
      refresh();
      continue;
    }

    if (key == KEY_UP) {
      if (hist_idx > 0) {
        if (hist_idx == (int)history.size()) {
          saved_buf = buf;
        }
        hist_idx--;
        buf = history[hist_idx];
        cursor = utf8_strlen(buf);
        refresh();
      }
      continue;
    }

    if (key == KEY_DOWN) {
      if (hist_idx < (int)history.size()) {
        hist_idx++;
        if (hist_idx == (int)history.size()) {
          buf = saved_buf;
        } else {
          buf = history[hist_idx];
        }
        cursor = utf8_strlen(buf);
        refresh();
      }
      continue;
    }

    if (key == KEY_NONE)
      continue;

    // 普通字符输入
    if (key > 0) {
      // 读取完整 UTF-8 序列
      std::string ch;
      unsigned char first = (unsigned char)key;
      ch.push_back((char)first);
      size_t expected = utf8_char_len(first);
      for (size_t i = 1; i < expected; i++) {
#ifdef _WIN32
        // Windows 的 ReadConsoleInputW 已给完整 Unicode, 这里处理代理对
        // 实际上 Windows 分支的 key 已经是完整 wchar_t 值
        break;
#else
        int next = read_byte_nonblock();
        if (next < 0)
          break;
        ch.push_back((char)next);
#endif
      }

#ifdef _WIN32
      // Windows: key 是 wchar_t, 转成 UTF-8
      if (key > 127) {
        ch.clear();
        wchar_t wc = (wchar_t)key;
        char mb[8] = {};
        int len = WideCharToMultiByte(CP_UTF8, 0, &wc, 1, mb, sizeof(mb),
                                      nullptr, nullptr);
        if (len > 0)
          ch.assign(mb, len);
      }
#endif

      if (!ch.empty()) {
        size_t byte_pos = utf8_byte_offset(buf, cursor);
        buf.insert(byte_pos, ch);
        cursor++;
        refresh();
      }
    }
  }
}

// ==================== 状态栏 (滚动区域方案) ====================

// 设置滚动区域 [1, rows-1], 保留最后一行给状态栏
static void screen_reserve_status_line() {
  query_terminal_size();
  // 清屏 (内容推入 scrollback), 光标到 (1,1)
  printf("\033[2J\033[H");
  // 设置滚动区域, 光标仍在 (1,1)
  printf("\033[1;%dr", g_term_rows - 1);
  // 画空状态栏 (绿底)
  printf("\033[%d;1H\033[42;97m%*s\033[0m", g_term_rows, g_term_cols, "");
  // 光标回到 (1,1)
  printf("\033[H");
  fflush(stdout);
}

// 恢复全屏滚动区域
static void screen_restore_full() {
  query_terminal_size();
  // 清除状态栏
  printf("\033[%d;1H\033[2K", g_term_rows);
  // 重置滚动区域 (光标跳到 1,1)
  printf("\033[r");
  // 移到底部, shell 提示符将出现在 chat 内容下方
  printf("\033[%d;1H", g_term_rows);
  fflush(stdout);
}

void status_draw(const char *text) {
  int old_rows = g_term_rows;
  query_terminal_size();
  // 窗口大小变化 → 重设滚动区域
  if (g_term_rows != old_rows) {
    printf("\0337");
    printf("\033[1;%dr", g_term_rows - 1);
    printf("\0338");
  }
  int len = (int)strlen(text);
  int pad = g_term_cols - len;
  if (pad < 0)
    pad = 0;
  printf("\0337\033[%d;1H\033[42;97m%s%*s\033[0m\0338", g_term_rows, text, pad,
         "");
  fflush(stdout);
}

void status_clear() {
  query_terminal_size();
  printf("\0337\033[%d;1H\033[2K\0338", g_term_rows);
  fflush(stdout);
}

} // namespace console
