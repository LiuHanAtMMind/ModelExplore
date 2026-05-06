// main.cpp - 多模态对话程序 (文本 + 图片)
// 基于 llama.cpp + mtmd (multimodal)
//
// 编译方式见 CMakeLists.txt
//
// 用法:
//   ./qwen_chat -m <模型路径> [-v <mmproj路径>] [-ngl -1] [-c 4096]
//   对话中:
//     直接输入文字进行对话
//     /image <图片路径> [问题]   发送图片
//     /clear                    清空对话历史
//     /exit 或 /quit            退出

#include "ggml.h"
#include "llama.h"
#include "mtmd-helper.h"
#include "mtmd.h"

#include "chat.h"
#include "console.h"

#include <algorithm>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <string>
#include <vector>

#ifdef _WIN32
#define NOMINMAX
#include <windows.h>
#else
#include <climits>
#include <libgen.h>
#include <signal.h>
#include <unistd.h>
#endif

// ==================== 信号处理 ====================

static volatile sig_atomic_t g_is_generating = 0;
static volatile sig_atomic_t g_interrupted = 0;

static void sigint_handler(int signo) {
  (void)signo;
  if (g_is_generating) {
    g_interrupted = 1; // 生成中: 中断生成, 不退出
  }
  // 非生成状态: 不做任何事. console::readline 通过 raw mode 直接读取 Ctrl+C
  // (0x03).
}

static void install_signal_handlers() {
#ifdef _WIN32
  signal(SIGINT, sigint_handler);
#else
  struct sigaction sa{};
  sa.sa_handler = sigint_handler;
  sigemptyset(&sa.sa_mask);
  sa.sa_flags = 0; // 不使用 SA_RESTART, 让阻塞输入被 Ctrl+C 立即打断
  sigaction(SIGINT, &sa, nullptr);
#endif
}

// ==================== 日志过滤 ====================

static bool g_verbose = false;

static void log_callback(enum ggml_log_level level, const char *text,
                         void *user_data) {
  (void)user_data;
  static bool last_printed = false;

  if (level == GGML_LOG_LEVEL_CONT) {
    // 续行: 仅在上一条被输出时才输出
    if (last_printed)
      fputs(text, stderr);
    return;
  }

  if (!g_verbose && level < GGML_LOG_LEVEL_WARN) {
    last_printed = false;
    return; // 非 verbose: 只显示 WARN 和 ERROR
  }

  // 非 verbose 下过滤掉多模态推理产生的无害 WARN
  if (!g_verbose && text && strstr(text, "non-consecutive token position")) {
    last_printed = false;
    return;
  }

  last_printed = true;
  fputs(text, stderr);
}

// ==================== 配置与上下文 ====================

struct AppConfig {
  std::string model_path =
      "./models/Qwen3.5-2B-GGUF/Qwen3.5-2B-UD-Q8_K_XL.gguf";
  std::string mmproj_path; // empty = auto-discover from model directory
  bool no_vision = false;
  bool vision_cpu = false; // mmproj 强制走 CPU (Jetson 显存不足时使用)
  bool no_think = false;
  bool no_mmap = false;
  bool use_direct_io = false;
  bool use_mlock = false;
  int n_gpu_layers = -1;
  int n_ctx = 8192;
  int n_batch = 2048;
  int max_tokens = 4096;
  float temperature = 0.7f;
  float top_p = 0.9f;
  int top_k = 40;
  float min_p = 0.05f;
  float repeat_penalty = 1.15f;
  int repeat_last_n = 256;
};

struct ChatContext {
  llama_model *model = nullptr;
  llama_context *ctx = nullptr;
  mtmd_context *ctx_mtmd = nullptr;
  llama_sampler *smpl = nullptr;
  const llama_vocab *vocab = nullptr;
  common_chat_templates_ptr chat_tmpls;

  std::vector<common_chat_msg> messages;
  std::vector<std::string> image_paths; // 与 messages 平行, 无图片则为空
  int n_past = 0;                       // 位置计数器 (下一个 token 的位置编号)
  int n_kv_used = 0; // 实际 KV cache slot 消耗 (含 M-RoPE 图片 token)
  std::string prev_formatted;
};

// ==================== 辅助函数 ====================

static std::vector<llama_token> my_tokenize(const llama_vocab *vocab,
                                            const std::string &text,
                                            bool add_special,
                                            bool parse_special = true) {
  int n = (int)text.size() + 256;
  std::vector<llama_token> tokens(n);
  n = llama_tokenize(vocab, text.c_str(), (int32_t)text.size(), tokens.data(),
                     (int32_t)tokens.size(), add_special, parse_special);
  if (n < 0) {
    tokens.resize(-n);
    n = llama_tokenize(vocab, text.c_str(), (int32_t)text.size(), tokens.data(),
                       (int32_t)tokens.size(), add_special, parse_special);
  }
  tokens.resize(n >= 0 ? n : 0);
  return tokens;
}

static std::string token_to_piece(const llama_vocab *vocab, llama_token token) {
  char buf[256];
  int n = llama_token_to_piece(vocab, token, buf, sizeof(buf), 0, true);
  if (n < 0) {
    std::string result(-n, '\0');
    llama_token_to_piece(vocab, token, result.data(), (int32_t)result.size(), 0,
                         true);
    return result;
  }
  return std::string(buf, n);
}

static std::string
apply_chat_template(const common_chat_templates *tmpls,
                    const std::vector<common_chat_msg> &messages,
                    bool add_generation_prompt, bool enable_thinking = true) {
  common_chat_templates_inputs inputs;
  inputs.messages = messages;
  inputs.add_generation_prompt = add_generation_prompt;
  inputs.use_jinja = true;
  inputs.enable_thinking = enable_thinking;

  auto params = common_chat_templates_apply(tmpls, inputs);
  return params.prompt;
}

static bool parse_input(const std::string &input, std::string &image_path,
                        std::string &text) {
  image_path.clear();
  text = input;

  if (input.size() < 8 || input.substr(0, 7) != "/image ") {
    return false;
  }

  std::string rest = input.substr(7);

  if (!rest.empty() && rest[0] == '"') {
    size_t end_quote = rest.find('"', 1);
    if (end_quote != std::string::npos) {
      image_path = rest.substr(1, end_quote - 1);
      std::string tail = rest.substr(end_quote + 1);
      size_t start = tail.find_first_not_of(" \t");
      text = (start == std::string::npos) ? "描述这张图片" : tail.substr(start);
      return true;
    }
  }

  size_t space = rest.find(' ');
  if (space != std::string::npos) {
    image_path = rest.substr(0, space);
    text = rest.substr(space + 1);
  } else {
    image_path = rest;
    text = "描述这张图片";
  }
  return true;
}

static bool file_exists(const char *path) {
  FILE *f = fopen(path, "rb");
  if (f) {
    fclose(f);
    return true;
  }
  return false;
}

// ==================== 解析命令行参数 ====================

static bool parse_args(int argc, char **argv, AppConfig &config) {
  for (int i = 1; i < argc; i++) {
    std::string arg = argv[i];
    if ((arg == "-m" || arg == "--model") && i + 1 < argc) {
      config.model_path = argv[++i];
    } else if ((arg == "-v" || arg == "--mmproj") && i + 1 < argc) {
      config.mmproj_path = argv[++i];
      if (config.mmproj_path == "none" || config.mmproj_path == "off") {
        config.no_vision = true;
      }
    } else if (arg == "--no-vision") {
      config.no_vision = true;
    } else if (arg == "--vision-cpu") {
      config.vision_cpu = true;
    } else if (arg == "--no-think") {
      config.no_think = true;
    } else if (arg == "--no-mmap") {
      config.no_mmap = true;
    } else if (arg == "--use-direct-io") {
      config.use_direct_io = true;
    } else if (arg == "--mlock") {
      config.use_mlock = true;
    } else if (arg == "--verbose") {
      g_verbose = true;
    } else if ((arg == "-ngl" || arg == "--n-gpu-layers") && i + 1 < argc) {
      config.n_gpu_layers = std::atoi(argv[++i]);
    } else if ((arg == "-c" || arg == "--ctx-size") && i + 1 < argc) {
      config.n_ctx = std::atoi(argv[++i]);
    } else if (arg == "--temp" && i + 1 < argc) {
      config.temperature = (float)std::atof(argv[++i]);
    } else if ((arg == "-n" || arg == "--max-tokens") && i + 1 < argc) {
      config.max_tokens = std::atoi(argv[++i]);
    } else if (arg == "-h" || arg == "--help") {
      printf("多模态对话 (llama.cpp)\n\n");
      printf("用法: %s [选项]\n\n", argv[0]);
      printf("  -m,   --model <path>       模型文件路径  (默认: %s)\n",
             config.model_path.c_str());
      printf("  -v,   --mmproj <path>      视觉编码器路径 (默认: 模型目录下 "
             "mmproj-F16.gguf)\n");
      printf("  -ngl, --n-gpu-layers <n>   GPU 层数, -1=全部 (默认: %d)\n",
             config.n_gpu_layers);
      printf("  -c,   --ctx-size <n>       上下文大小     (默认: %d)\n",
             config.n_ctx);
      printf("  -n,   --max-tokens <n>     最大生成 token (默认: %d)\n",
             config.max_tokens);
      printf("        --temp <f>           温度           (默认: %.2f)\n",
             config.temperature);
      printf("        --no-vision          禁用视觉编码器\n");
      printf("        --vision-cpu         视觉编码器在 CPU 上运行 (避免 "
             "显存不足)\n");
      printf("        --no-think           禁用思考模式 (Qwen3.5 等)\n");
      printf(
          "        --no-mmap            禁用 mmap, 改为直接读取模型到内存\n");
      printf(
          "        --use-direct-io      启用 direct I/O (支持时优先于 mmap)\n");
      printf("        --mlock              锁定模型到物理内存 (防止换出)\n");
      printf("        --verbose            显示详细日志 (含 CUDA debug)\n");
      printf("  -h,   --help               显示帮助\n");
      return false;
    } else {
      fprintf(stderr, "未知参数: %s (使用 -h 查看帮助)\n", arg.c_str());
      return false;
    }
  }
  return true;
}

//  ==================== 初始化 / 清理 ====================

static bool init_resources(const AppConfig &config, ChatContext &chat) {
  llama_backend_init();

  printf("正在加载模型: %s\n", config.model_path.c_str());

  llama_model_params model_params = llama_model_default_params();
  model_params.n_gpu_layers = config.n_gpu_layers;
  model_params.use_mmap = !config.no_mmap;
  model_params.use_direct_io = config.use_direct_io;
  model_params.use_mlock = config.use_mlock;

  chat.model =
      llama_model_load_from_file(config.model_path.c_str(), model_params);
  if (!chat.model) {
    fprintf(stderr, "错误: 无法加载模型 %s\n", config.model_path.c_str());
    return false;
  }

  chat.vocab = llama_model_get_vocab(chat.model);

  llama_context_params ctx_params = llama_context_default_params();
  ctx_params.n_ctx = config.n_ctx;
  ctx_params.n_batch = config.n_batch;

  chat.ctx = llama_init_from_model(chat.model, ctx_params);
  if (!chat.ctx) {
    fprintf(stderr, "错误: 无法创建推理上下文\n");
    return false;
  }

  // 视觉编码器: 若未手动指定, 自动从模型目录查找 mmproj-F16.gguf
  AppConfig &cfg = const_cast<AppConfig &>(config);
  if (cfg.mmproj_path.empty() && !cfg.no_vision) {
    std::string model_dir = cfg.model_path;
    size_t sep = model_dir.find_last_of("/\\");
    if (sep != std::string::npos) {
      model_dir = model_dir.substr(0, sep);
    } else {
      model_dir = ".";
    }
    std::string auto_mmproj = model_dir + "/mmproj-F16.gguf";
    if (file_exists(auto_mmproj.c_str())) {
      cfg.mmproj_path = auto_mmproj;
      printf("[INFO] 自动发现视觉编码器: %s\n", auto_mmproj.c_str());
    }
  }

  if (!config.no_vision && !config.mmproj_path.empty() &&
      file_exists(config.mmproj_path.c_str())) {
    printf("正在加载视觉编码器: %s\n", config.mmproj_path.c_str());
    mtmd_context_params mtmd_params = mtmd_context_params_default();
    mtmd_params.n_threads = 4;
    mtmd_params.use_gpu = !config.vision_cpu;
    if (config.vision_cpu) {
      printf("[INFO] 视觉编码器将在 CPU 上运行\n");
    }
    chat.ctx_mtmd = mtmd_init_from_file(config.mmproj_path.c_str(), chat.model,
                                        mtmd_params);
    if (chat.ctx_mtmd) {
      printf("[OK] 视觉编码器加载成功\n");
    } else {
      fprintf(stderr, "[WARN] 视觉编码器加载失败, 将以纯文本模式运行\n");
    }
  } else if (config.no_vision) {
    printf("[INFO] 已禁用视觉编码器, 以纯文本模式运行\n");
  } else {
    printf("[INFO] 未找到视觉编码器, 以纯文本模式运行\n");
  }

  // 采样器链
  llama_sampler_chain_params sparams = llama_sampler_chain_default_params();
  chat.smpl = llama_sampler_chain_init(sparams);
  llama_sampler_chain_add(
      chat.smpl, llama_sampler_init_penalties(
                     config.repeat_last_n, config.repeat_penalty, 0.0f, 0.0f));
  llama_sampler_chain_add(chat.smpl, llama_sampler_init_min_p(config.min_p, 1));
  llama_sampler_chain_add(chat.smpl, llama_sampler_init_top_k(config.top_k));
  llama_sampler_chain_add(chat.smpl, llama_sampler_init_top_p(config.top_p, 1));
  llama_sampler_chain_add(chat.smpl,
                          llama_sampler_init_temp(config.temperature));
  llama_sampler_chain_add(chat.smpl,
                          llama_sampler_init_dist(LLAMA_DEFAULT_SEED));

  // 聊天模板 (Jinja 引擎)
  chat.chat_tmpls = common_chat_templates_init(chat.model, "");
  if (!chat.chat_tmpls) {
    fprintf(stderr, "错误: 无法初始化聊天模板\n");
    return false;
  }
  printf("[OK] 聊天模板已加载\n");

  return true;
}

static void cleanup_resources(ChatContext &chat) {
  llama_sampler_free(chat.smpl);
  if (chat.ctx_mtmd)
    mtmd_free(chat.ctx_mtmd);
  llama_free(chat.ctx);
  llama_model_free(chat.model);
  llama_backend_free();
}

// ==================== 上下文容量控制 ====================

// 获取上下文剩余可用 KV slot 数
static int ctx_available(const ChatContext &chat, const AppConfig &config) {
  return config.n_ctx - chat.n_kv_used;
}

// 检查输入 token 数是否能放入上下文, 不够则警告并返回 false
static bool ctx_check_input(const ChatContext &chat, const AppConfig &config,
                            int n_input_tokens) {
  int avail = ctx_available(chat, config);
  if (n_input_tokens > avail) {
    fprintf(stderr,
            "[警告] 输入 (%d tokens) 超过上下文剩余容量 (%d), "
            "请 /clear 或增大 --ctx-size\n\n",
            n_input_tokens, avail);
    return false;
  }
  int left_for_output = avail - n_input_tokens;
  if (left_for_output < 32) {
    fprintf(stderr, "[警告] 输入后仅剩 %d tokens 用于生成, 回复可能不完整\n",
            left_for_output);
  }
  return true;
}

// ==================== Prompt 评估 ====================

// 评估多模态 prompt (文本 + 图片, 支持多张)
static bool eval_multimodal(ChatContext &chat, const AppConfig &config,
                            const std::string &new_text,
                            const std::vector<std::string> &img_paths) {
  // 加载所有图片
  std::vector<mtmd_bitmap *> bitmaps;
  for (const auto &p : img_paths) {
    mtmd_bitmap *bmp =
        mtmd_helper_bitmap_init_from_file(chat.ctx_mtmd, p.c_str());
    if (!bmp) {
      fprintf(stderr, "错误: 无法加载图片 %s\n\n", p.c_str());
      for (auto *b : bitmaps)
        mtmd_bitmap_free(b);
      return false;
    }
    bitmaps.push_back(bmp);
  }

  mtmd_input_chunks *chunks = mtmd_input_chunks_init();
  mtmd_input_text input_text;
  input_text.text = new_text.c_str();
  input_text.add_special = false;
  input_text.parse_special = true;

  std::vector<const mtmd_bitmap *> ptrs(bitmaps.begin(), bitmaps.end());
  int32_t ret = mtmd_tokenize(chat.ctx_mtmd, chunks, &input_text, ptrs.data(),
                              (int32_t)ptrs.size());
  if (ret != 0) {
    fprintf(stderr, "错误: mtmd_tokenize 失败 (code=%d)\n\n", ret);
    mtmd_input_chunks_free(chunks);
    for (auto *b : bitmaps)
      mtmd_bitmap_free(b);
    return false;
  }

  // 检查各 chunk: batch 限制用 n_tokens, 容量检查也用 n_tokens (实际 KV slot
  // 消耗)
  uint32_t actual_n_batch = llama_n_batch(chat.ctx);
  size_t n_chunks = mtmd_input_chunks_size(chunks);
  int total_n_tokens = 0;
  for (size_t i = 0; i < n_chunks; i++) {
    const mtmd_input_chunk *chunk = mtmd_input_chunks_get(chunks, i);
    size_t chunk_n_tokens = mtmd_input_chunk_get_n_tokens(chunk);
    total_n_tokens += (int)chunk_n_tokens;
    // 单个 chunk 的 token 数不能超过 batch 上限 (否则 llama_decode 会 assert)
    if (chunk_n_tokens > actual_n_batch) {
      fprintf(stderr,
              "[警告] 图片 token 数 (%d) 超过 batch 上限 (%u), "
              "请增大 --ctx-size (建议 >= 2048)\n\n",
              (int)chunk_n_tokens, actual_n_batch);
      mtmd_input_chunks_free(chunks);
      for (auto *b : bitmaps)
        mtmd_bitmap_free(b);
      return false;
    }
  }

  // 检查实际 KV slot 消耗是否超过上下文剩余容量
  if (!ctx_check_input(chat, config, total_n_tokens)) {
    mtmd_input_chunks_free(chunks);
    for (auto *b : bitmaps)
      mtmd_bitmap_free(b);
    return false;
  }

  llama_pos new_n_past = chat.n_past;
  ret = mtmd_helper_eval_chunks(chat.ctx_mtmd, chat.ctx, chunks, chat.n_past, 0,
                                config.n_batch, true, &new_n_past);
  chat.n_past = new_n_past;
  chat.n_kv_used += total_n_tokens;

  mtmd_input_chunks_free(chunks);
  for (auto *b : bitmaps)
    mtmd_bitmap_free(b);

  if (ret != 0) {
    fprintf(stderr, "错误: 图片推理失败 (code=%d)\n\n", ret);
    return false;
  }
  return true;
}

// 评估纯文本 prompt
static bool eval_text(ChatContext &chat, const AppConfig &config,
                      const std::string &new_text) {
  std::vector<llama_token> tokens =
      my_tokenize(chat.vocab, new_text, false, true);

  if (!ctx_check_input(chat, config, (int)tokens.size()))
    return false;

  for (size_t i = 0; i < tokens.size(); i += config.n_batch) {
    int n_eval = std::min((int)(tokens.size() - i), config.n_batch);
    llama_batch batch = llama_batch_get_one(tokens.data() + i, n_eval);

    if (llama_decode(chat.ctx, batch) != 0) {
      fprintf(stderr, "错误: llama_decode 失败\n");
      return false;
    }
    chat.n_past += n_eval;
    chat.n_kv_used += n_eval;
  }
  return true;
}

// ==================== 状态栏辅助 ====================

static std::string format_count(int n) {
  if (n < 1000)
    return std::to_string(n);
  if (n < 10000) {
    char buf[16];
    snprintf(buf, sizeof(buf), "%.1fk", n / 1000.0);
    return buf;
  }
  return std::to_string(n / 1000) + "k";
}

static void status_show_ctx(const ChatContext &chat, const AppConfig &config) {
  char bar[256];
  snprintf(bar, sizeof(bar), " ctx: %s / %s",
           format_count(chat.n_kv_used).c_str(),
           format_count(config.n_ctx).c_str());
  console::status_draw(bar);
}

static void status_show_gen(const ChatContext &chat, const AppConfig &config,
                            int gen_tokens, double speed) {
  char bar[256];
  snprintf(
      bar, sizeof(bar), " %.1f t/s | %s / %s | ctx: %s / %s", speed,
      format_count(gen_tokens).c_str(), format_count(config.max_tokens).c_str(),
      format_count(chat.n_kv_used).c_str(), format_count(config.n_ctx).c_str());
  console::status_draw(bar);
}

// ==================== 生成回复 ====================

static std::string generate_response(ChatContext &chat,
                                     const AppConfig &config) {
  printf("AI: ");
  fflush(stdout);

  std::string response;
  int gen_tokens = 0;

  auto t_start = std::chrono::steady_clock::now();

  g_interrupted = 0;
  g_is_generating = 1;

  for (int i = 0; i < config.max_tokens; i++) {
    if (g_interrupted) {
      printf("\n[生成已中断]");
      break;
    }
    llama_token token = llama_sampler_sample(chat.smpl, chat.ctx, -1);
    llama_sampler_accept(chat.smpl, token);

    if (llama_vocab_is_eog(chat.vocab, token)) {
      break;
    }

    std::string piece = token_to_piece(chat.vocab, token);
    printf("%s", piece.c_str());
    fflush(stdout);
    response += piece;
    gen_tokens++;

    // 更新底部状态栏
    auto t_now = std::chrono::steady_clock::now();
    double elapsed = std::chrono::duration<double>(t_now - t_start).count();
    double speed = (elapsed > 0.0) ? gen_tokens / elapsed : 0.0;
    status_show_gen(chat, config, gen_tokens, speed);

    llama_batch batch = llama_batch_get_one(&token, 1);
    if (llama_decode(chat.ctx, batch) != 0) {
      fprintf(stderr, "\n错误: llama_decode 失败 (生成第 %d 个 token)\n", i);
      break;
    }
    chat.n_past += 1;
    chat.n_kv_used += 1;

    if (chat.n_kv_used >= config.n_ctx) {
      printf("\n[上下文已满, 生成终止, 请 /clear 或增大 --ctx-size]");
      break;
    }
  }

  g_is_generating = 0;
  g_interrupted = 0;

  // 生成结束后状态栏只显示 context 占用
  status_show_ctx(chat, config);

  if (!g_interrupted && gen_tokens >= config.max_tokens) {
    printf("\n[已达生成上限 %d tokens, 可用 -n 调整]", config.max_tokens);
  }

  auto t_end = std::chrono::steady_clock::now();
  double elapsed = std::chrono::duration<double>(t_end - t_start).count();
  double speed = (elapsed > 0.0) ? gen_tokens / elapsed : 0.0;
  printf("\n[%d tokens, %.1f tokens/s]\n\n", gen_tokens, speed);
  return response;
}

static void chat_loop(ChatContext &chat, const AppConfig &config) {
  printf("\n===== 对话开始 =====\n");
  printf("  /image <图片路径> [问题]  - 发送图片\n");
  printf("  /clear                    - 清空对话\n");
  printf("  /exit 或 /quit            - 退出\n\n");

  while (true) {
    std::string user_input;
    if (!console::readline("You: ", user_input)) {
      // Ctrl+C 或 EOF: 另起一行重新输入
      continue;
    }

    while (!user_input.empty() &&
           (user_input.back() == '\n' || user_input.back() == '\r' ||
            user_input.back() == ' ' || user_input.back() == '\t'))
      user_input.pop_back();

    if (user_input.empty())
      continue;
    if (user_input == "/quit" || user_input == "/exit")
      break;

    if (user_input == "/clear" || user_input == "/reset") {
      chat.messages.clear();
      chat.image_paths.clear();
      chat.n_past = 0;
      chat.n_kv_used = 0;
      chat.prev_formatted.clear();
      llama_memory_clear(llama_get_memory(chat.ctx), true);
      llama_sampler_reset(chat.smpl);
      printf("[对话已清空]\n\n");
      status_show_ctx(chat, config);
      continue;
    }

    std::string image_path, text;
    bool has_image = parse_input(user_input, image_path, text);

    // 构建用户消息
    std::string user_content;
    if (has_image) {
      if (!chat.ctx_mtmd) {
        printf("错误: 未加载视觉编码器, 无法处理图片\n\n");
        continue;
      }
      if (!file_exists(image_path.c_str())) {
        printf("错误: 图片不存在 - %s\n\n", image_path.c_str());
        continue;
      }
      user_content = std::string(mtmd_default_marker()) + "\n" + text;
    } else {
      user_content = text;
    }

    common_chat_msg user_msg;
    user_msg.role = "user";
    user_msg.content = user_content;
    chat.messages.push_back(user_msg);
    chat.image_paths.push_back(has_image ? image_path : "");

    // 格式化 prompt, 只取新增部分
    std::string full_formatted = apply_chat_template(
        chat.chat_tmpls.get(), chat.messages, true, !config.no_think);
    if (full_formatted.empty()) {
      chat.messages.pop_back();
      chat.image_paths.pop_back();
      continue;
    }

    // 检查增量前缀是否匹配, 不匹配则清空 KV cache 重新评估
    bool prefix_ok = full_formatted.size() >= chat.prev_formatted.size() &&
                     full_formatted.compare(0, chat.prev_formatted.size(),
                                            chat.prev_formatted) == 0;
    if (!prefix_ok && !chat.prev_formatted.empty()) {
      llama_memory_clear(llama_get_memory(chat.ctx), true);
      chat.n_past = 0;
      chat.n_kv_used = 0;
      chat.prev_formatted.clear();
    }

    std::string new_text = full_formatted.substr(chat.prev_formatted.size());

    // 记录 eval 前的状态, 用于计算实际消耗
    int n_past_before_eval = chat.n_past;
    int n_kv_before_eval = chat.n_kv_used;

    // 收集本次评估需要的所有图片路径
    std::vector<std::string> eval_images;
    if (!prefix_ok && chat.prev_formatted.empty()) {
      // 完整重新评估: 收集所有历史图片 (不含当前轮, 当前轮在下面加)
      for (size_t j = 0; j + 1 < chat.image_paths.size(); j++) {
        if (!chat.image_paths[j].empty())
          eval_images.push_back(chat.image_paths[j]);
      }
    }
    if (has_image) {
      eval_images.push_back(image_path);
    }

    // 评估 prompt
    bool ok;
    if (!eval_images.empty() && chat.ctx_mtmd) {
      ok = eval_multimodal(chat, config, new_text, eval_images);
    } else {
      ok = eval_text(chat, config, new_text);
    }

    if (!ok) {
      chat.messages.pop_back();
      chat.image_paths.pop_back();
      continue;
    }

    int n_prompt_eval = chat.n_past - n_past_before_eval;
    int n_kv_delta = chat.n_kv_used - n_kv_before_eval;
    if (n_kv_delta != n_prompt_eval) {
      printf("[prompt: %d pos, %d KV slots, 剩余 %d]\n", n_prompt_eval,
             n_kv_delta, ctx_available(chat, config));
    } else {
      printf("[prompt: %d tokens, 剩余 %d]\n", n_prompt_eval,
             ctx_available(chat, config));
    }

    // 更新状态栏显示 context 占用
    status_show_ctx(chat, config);

    // 采样生成回复
    std::string response = generate_response(chat, config);

    common_chat_msg asst_msg;
    asst_msg.role = "assistant";
    asst_msg.content = response;
    chat.messages.push_back(asst_msg);
    chat.image_paths.push_back("");

    chat.prev_formatted = apply_chat_template(
        chat.chat_tmpls.get(), chat.messages, false, !config.no_think);
  }
}

// ==================== 主函数 ====================

int main(int argc, char **argv) {
#ifdef _WIN32
  SetConsoleOutputCP(CP_UTF8);
  SetConsoleCP(CP_UTF8);
#endif

  llama_log_set(log_callback, nullptr);
  mtmd_helper_log_set(log_callback, nullptr);

  install_signal_handlers();

  AppConfig config;
  if (!parse_args(argc, argv, config)) {
    return 1;
  }

  // 启用 readline 风格输入 (上下左右方向键 + 历史记录) + 状态栏
  console::init();
  std::atexit([]() { console::cleanup(); });

  ChatContext chat;
  if (!init_resources(config, chat)) {
    cleanup_resources(chat);
    return 1;
  }

  // 初始状态栏
  status_show_ctx(chat, config);

  chat_loop(chat, config);

  printf("\n再见!\n");
  cleanup_resources(chat);
  return 0;
}
