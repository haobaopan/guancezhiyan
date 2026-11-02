#!/bin/zsh
# 只做一件事：配好嵌入（向量）接口 → 备份 → 回填 33,039 条切片向量 → 核对
# 用法：
#   ./配置嵌入密钥.command              交互选择服务商并输入密钥
#   ./配置嵌入密钥.command --check      不填密钥，只检查现有配置能不能用
# 支持的 EMBEDDING_* 变量见 部署与运维.md 第四节
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
export POLICY_KB_DIR="$HERE"

# ---------- 运行日志：每次跑都落一份，事后可查"到底成没成" ----------
# --check 是只读检查，不产生日志；其余情况（含中途失败、找不到文件）都记录。
if [[ "${1:-}" != "--check" ]]; then
  LOG_DIR="$HERE/配置日志"
  mkdir -p "$LOG_DIR"
  LOG="$LOG_DIR/配置_$(date '+%Y%m%d-%H%M%S').log"
  exec > >(tee -a "$LOG") 2>&1
  print -r -- "===== 配置嵌入 $(date '+%F %T') =====" 
  print -r -- "目录：$HERE"
  print -r -- "系统：$(uname -sm)｜python3：$(command -v python3 || echo 未找到)"
  if [[ -f "$HERE/.env" ]]; then print -r -- "已有 .env：是"; else print -r -- "已有 .env：否"; fi
  print -r -- "---------------------------------------"
  # 退出时统一给出结论，避免"看着像跑完了但实际没写库"
  # 只往 stdout 写：stdout 已由上面的 tee 同步落盘，直接写文件会和 tee 的缓冲抢顺序
  on_exit() {
    local rc=$?
    print -r -- ""
    if (( rc == 0 )); then
      print -r -- "✅ 结论：配置成功 —— 向量已回填并通过严格核对（$(date '+%F %T')）"
      print -r -- "   日志：$LOG"
    else
      print -r -- "❌ 结论：未配置成功（退出码 $rc，$(date '+%F %T')）"
      print -r -- "   日志：$LOG"
      print -r -- "   按上方提示修正后重跑即可；已回填的部分会从断点续跑。"
    fi
  }
  trap on_exit EXIT
fi

BUILD="$HERE/构建示例知识库.py"
DB="$HERE/knowledge.sqlite3"

[[ -f "$BUILD" ]] || { echo '找不到 构建示例知识库.py，请把本文件与它放在同一目录。' >&2; exit 1; }
[[ -f "$DB" ]] || { echo "找不到知识库 $DB，请先运行 配置并回填知识库.command。" >&2; exit 1; }

PY="${PYTHON_BIN:-$(command -v python3 || true)}"
[[ -n "$PY" ]] || { echo '未找到 python3，请先安装 Python 3.9 及以上版本。' >&2; exit 1; }

# ---------- 读已有配置（LLM 部分要原样保留） ----------
if [[ -f "$HERE/.env" ]]; then
  set -a
  source "$HERE/.env"
  set +a
fi
LLM_BASE_SAVED="${LLM_BASE_URL:-}"
LLM_MODEL_SAVED="${LLM_MODEL:-}"
LLM_KEY_SAVED="${LLM_API_KEY:-}"

if [[ "${1:-}" == "--check" ]]; then
  echo "当前配置：${EMBEDDING_BASE_URL:-未设置}｜模型 ${EMBEDDING_MODEL:-未设置}｜维度 ${EMBEDDING_DIM:-未知}"
  echo
  exec "$PY" "$BUILD" --verify-embeddings --probe
fi

# ---------- 1. 选服务商 ----------
cat <<'MENU'
请选择向量服务商（回车默认 1）：

  1) 硅基流动  BAAI/bge-m3        1024 维，长期免费，国内直连  ← 推荐
  2) 阿里云百炼 qwen3.7-text-embedding  1024 维，100 万 token 免费额度，之后约 ¥4
  3) 其它 OpenAI 兼容接口（自填地址与模型）
  4) 本机服务（Ollama / LM Studio，免密钥）

MENU
# zsh 的 read 必须写成「变量?提示语」；写成 read "提示语" 会把整串当变量名，
# 提示语里只要含 [ 或空格就报 not an identifier（第五轮在 API 密钥那处踩过一次）。
read -r "CHOICE?序号 [1]: "
CHOICE=${CHOICE:-1}

KEYLESS=0
case "$CHOICE" in
  1)
    BASE_URL='https://api.siliconflow.cn/v1'
    MODEL='BAAI/bge-m3'
    DIM=1024
    BATCH=16
    CONC=2
    HINT='在 cloud.siliconflow.cn 用手机号注册 → 完成实名认证 → 左侧「API 密钥」→ 新建密钥（形如 sk-xxxx）'
    ;;
  2)
    BASE_URL='https://dashscope.aliyuncs.com/compatible-mode/v1'
    MODEL='qwen3.7-text-embedding'
    DIM=1024
    BATCH=20
    CONC=4
    HINT='在 bailian.console.aliyun.com 开通「百炼」→ 右上角 API-KEY → 创建我的 API-KEY（形如 sk-xxxx）'
    ;;
  3)
    read -r "BASE_URL?接口根地址（到 /v1 为止）: "
    [[ -n "${BASE_URL:-}" ]] || { echo '地址不能为空。' >&2; exit 1; }
    read -r "MODEL?模型名（如 text-embedding-3-small）: "
    [[ -n "${MODEL:-}" ]] || { echo '模型名不能为空。' >&2; exit 1; }
    read -r "DIM?向量维度（不确定就回车跳过）: "
    BATCH=16
    CONC=2
    HINT='按服务商文档填写'
    ;;
  4)
    read -r "BASE_URL?本机地址 [http://127.0.0.1:11434/v1]: "
    BASE_URL=${BASE_URL:-http://127.0.0.1:11434/v1}
    read -r "MODEL?模型名 [bge-m3]: "
    MODEL=${MODEL:-bge-m3}
    read -r "DIM?向量维度 [1024]: "
    DIM=${DIM:-1024}
    BATCH=16
    CONC=2
    KEYLESS=1
    HINT='本机服务不需要密钥'
    ;;
  *) echo '序号无效。' >&2; exit 1 ;;
esac

# ---------- 2. 输入密钥 ----------
EMB_KEY=''
if [[ "$KEYLESS" == "0" ]]; then
  echo
  echo "获取方式：$HINT"
  echo "（粘贴后按回车；输入过程不显示，属正常）"
  read -r -s "EMB_KEY?API 密钥: "
  echo
  [[ -n "${EMB_KEY:-}" ]] || { echo '没有输入密钥，已取消。' >&2; exit 1; }
  # 常见粘贴错误：带引号、带空格、带 "Bearer "
  EMB_KEY="${EMB_KEY#Bearer }"
  EMB_KEY="${EMB_KEY// /}"
  EMB_KEY="${EMB_KEY//\"/}"
  EMB_KEY="${EMB_KEY//\'/}"
  # 常见粘贴错误：整段被重复粘贴成一长串（2026-10-27 真实案例：同一个密钥粘了 7 遍，
  # 357 字符，服务端只回 401 "Token is invalid."，光看日志完全猜不到是粘贴问题）。
  # 硅基流动 51 字符、百炼 35 字符上下；超过 80 字符必然不对，按「出现几次 sk-」等分取第一段。
  if (( ${#EMB_KEY} > 80 )); then
    NKEY="$(print -r -- "$EMB_KEY" | grep -o 'sk-' | wc -l | tr -d ' ' || true)"
    NKEY=${NKEY:-1}
    UNIT=$(( ${#EMB_KEY} / NKEY ))
    if (( NKEY > 1 && UNIT >= 20 )); then
      FIRST_KEY="${EMB_KEY[1,$UNIT]}"
      echo "提醒：粘贴进 ${#EMB_KEY} 个字符、含 $NKEY 个「sk-」开头，像是同一个密钥被重复粘贴；" >&2
      echo "      已自动取第一段（$UNIT 个字符）。若仍报 401，请只复制「API 密钥」那一行重跑。" >&2
      EMB_KEY="$FIRST_KEY"
    else
      echo "提醒：密钥长度 ${#EMB_KEY} 个字符，明显偏长（正常 35–60）。请只复制密钥本身，不要带上页面上的说明文字。" >&2
    fi
  fi
  case "$EMB_KEY" in
    sk-*) ;;
    *) echo "提醒：密钥通常以 sk- 开头，你输入的是 ${EMB_KEY[1,6]}…，如果不是该平台的密钥请核对。" >&2 ;;
  esac
fi

# ---------- 3. 写入 .env（保留 LLM 配置） ----------
{
  echo '# 由 配置嵌入密钥.command 生成，含密钥，请勿外传、勿提交'
  if [[ -n "$LLM_BASE_SAVED" ]]; then echo "LLM_BASE_URL=$LLM_BASE_SAVED"; fi
  if [[ -n "$LLM_MODEL_SAVED" ]]; then echo "LLM_MODEL=$LLM_MODEL_SAVED"; fi
  if [[ -n "$LLM_KEY_SAVED" ]]; then echo "LLM_API_KEY=$LLM_KEY_SAVED"; fi
  echo "EMBEDDING_BASE_URL=$BASE_URL"
  echo "EMBEDDING_MODEL=$MODEL"
  if [[ -n "${DIM:-}" ]]; then echo "EMBEDDING_DIM=$DIM"; fi
  echo "EMBEDDING_BATCH_SIZE=$BATCH"
  echo "EMBEDDING_CONCURRENCY=$CONC"
  if [[ -n "$EMB_KEY" ]]; then echo "EMBEDDING_API_KEY=$EMB_KEY"; fi
} > "$HERE/.env"
chmod 600 "$HERE/.env"
echo "配置已写入 $HERE/.env（权限 600，仅本人可读）"

# 注意：set -e 下不能用 `[[ ]] && export`，条件为假时整条返回 1 会直接退出脚本
export EMBEDDING_BASE_URL="$BASE_URL" EMBEDDING_MODEL="$MODEL"
export EMBEDDING_BATCH_SIZE="$BATCH" EMBEDDING_CONCURRENCY="$CONC"
if [[ -n "${DIM:-}" ]]; then export EMBEDDING_DIM="$DIM"; fi
if [[ -n "$EMB_KEY" ]]; then export EMBEDDING_API_KEY="$EMB_KEY"; fi

# ---------- 4. 先探一次，确认密钥与模型/维度都对 ----------
echo
echo '第 1 步：用一条测试文本探接口（不会写库）…'
PROBE_OUT="$("$PY" - <<'PROBE'
import json, os, sys, urllib.request
from urllib.parse import urlparse

def loopback(url):
    host = (urlparse(url).hostname or '').strip().lower()
    return host in ('127.0.0.1', 'localhost', '::1') or host.startswith('127.')

base = (os.getenv('EMBEDDING_BASE_URL') or '').rstrip('/')
model = os.getenv('EMBEDDING_MODEL') or ''
key = (os.getenv('EMBEDDING_API_KEY') or os.getenv('LLM_API_KEY') or '').strip()
want = (os.getenv('EMBEDDING_DIM') or '').strip()

if not key and not loopback(base):
    sys.exit('既没有密钥，地址也不是本机回环，无法调用。')
headers = {'Content-Type': 'application/json'}
if key:
    headers['Authorization'] = 'Bearer ' + key
payload = json.dumps({'model': model, 'input': ['沧州市十五规划提出产业结构调整']}).encode()
req = urllib.request.Request(base + '/embeddings', data=payload, headers=headers)
# 回环地址强制直连：本机服务走代理必然失败（HTTP_PROXY 会把 127.0.0.1 也转给代理）
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open(req, timeout=30) as resp:
        data = json.loads(resp.read())
except Exception as exc:
    body = ''
    try:
        body = exc.read().decode('utf-8', 'replace')[:300]
    except Exception:
        pass
    sys.exit(f'调用失败：{exc} {body}\n'
             '  · 401/403：密钥错误或未实名认证；\n'
             '  · 404：模型名不对，或地址漏了 /v1；\n'
             '  · 429：限流，稍后再试或调小批大小；\n'
             '  · 超时/连接失败：检查网络，或服务商地址是否写错。')
vec = data['data'][0]['embedding']
dim = len(vec)
print(f'  接口正常，返回 {dim} 维向量')
if want and str(dim) != want:
    sys.exit(f'  维度不符：接口返回 {dim} 维，配置写的是 {want} 维。请把 EMBEDDING_DIM 改为 {dim} 后重跑。')
print(f'DIMTOKEN={dim}')
PROBE
)" || { echo; echo '接口探测未通过，已停止，知识库未被改动。' >&2; echo '改好 .env 后重跑本脚本，或加 --check 只看现有配置状态。' >&2; exit 1; }

print -r -- "$PROBE_OUT"
DETECTED="$(print -r -- "$PROBE_OUT" | sed -n 's/^DIMTOKEN=//p' | tail -1)"
if [[ -n "$DETECTED" && -z "${DIM:-}" ]]; then
  echo "  已自动记录维度 $DETECTED"
  echo "EMBEDDING_DIM=$DETECTED" >> "$HERE/.env"
  export EMBEDDING_DIM="$DETECTED"
fi

# ---------- 5. 备份 + 回填 ----------
echo
echo '第 2 步：备份知识库，然后回填全部切片向量（可中断，重跑即续跑）…'
echo '        33,039 条切片，云端约 10–30 分钟；中途 Ctrl+C 不会损坏库。'
echo
if ! "$PY" "$BUILD" --index-embeddings; then
  echo
  echo '回填没跑完。已完成的部分已落库，直接重跑本脚本会从断点继续。' >&2
  echo '若报 429（限流），把 .env 里 EMBEDDING_BATCH_SIZE 调小（如 8）、EMBEDDING_CONCURRENCY 调到 1 再试。' >&2
  exit 1
fi

# ---------- 6. 核对 ----------
echo
echo '第 3 步：核对覆盖面与模型一致性（不达标会以非零退出）…'
echo
if ! "$PY" "$BUILD" --verify-embeddings --probe --strict; then
  echo
  echo '核对未通过，见上方缺额说明。' >&2
  exit 1
fi

echo
echo '————————————————————————————————————————'
echo '向量已就绪。下一步启动应用，让语义召回生效：'
echo '  ./配置并回填知识库.command --no-index'
echo '启动后浏览器打开 http://127.0.0.1:8765，页面顶部应显示「语义检索：已启用」。'
echo '也可以直接查：curl -s localhost:8765/healthz'
echo '判据：embedded 与 ready_chunks 相等、embedded_stale 为 0、semantic_active 为 true。'
