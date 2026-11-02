#!/bin/zsh
# 一键：配置 API →（按需）建库 / 回填向量 → 启动本地应用
# 用法：
#   ./配置并回填知识库.command            正常：缺库则建库，然后续跑向量，最后启动应用
#   ./配置并回填知识库.command --rebuild  强制重建知识库（原地 DROP 三张表再建，不删文件）
#   ./配置并回填知识库.command --no-index 跳过向量回填，直接启动应用
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
export POLICY_KB_DIR="$HERE"
unset POLICY_DATASET_DIR   # 交给脚本自动探测数据集目录

BUILD="$HERE/构建示例知识库.py"
APP="$HERE/应用源码_示例知识库版.py"
DB="$HERE/knowledge.sqlite3"
ACTION="${1:-}"

[[ -f "$BUILD" && -f "$APP" ]] || { echo '找不到构建脚本或应用脚本，请把本文件与它们放在同一目录。' >&2; exit 1; }

# ---------- 1. Python 版本自检 ----------
PY="${PYTHON_BIN:-$(command -v python3 || true)}"
[[ -n "$PY" ]] || { echo '未找到 python3，请先安装 Python 3.9 及以上版本。' >&2; exit 1; }
"$PY" - <<'PYVER'
import sys
if sys.version_info < (3, 9):
    sys.exit(f'需要 Python 3.9 及以上，当前为 {sys.version.split()[0]}')
print(f'使用 Python {sys.version.split()[0]}')
PYVER

# ---------- 2. 读取已有配置 ----------
if [[ -f "$HERE/.env" ]]; then
  set -a
  source "$HERE/.env"
  set +a
  echo "已载入配置：$HERE/.env"
fi

# ---------- 3. 问答模型（可选）：缺它不该拦住启动 ----------
# 旧版这里一进来就要求输入 LLM 密钥，没配问答模型时脚本会停在提示符上等输入，
# 表现为「双击后没反应、浏览器打不开 127.0.0.1:8765」。改成询问式，回车默认跳过。
if [[ -z "${LLM_API_KEY:-}" ]]; then
  echo
  echo '未检测到问答模型密钥（LLM_API_KEY）。'
  echo '不配也能启动：检索、溯源、四要素、跨期对照、导出都正常，只是不生成综合结论。'
  read -r "WANT_LLM?现在配置问答模型吗？[y/N]: "
  case "${WANT_LLM:-n}" in
    [Yy]*)
      read -r "API_BASE_URL?API 根地址 [https://maas.qianwenaiapi.com/compatible-mode/v1]: "
      API_BASE_URL=${API_BASE_URL:-https://maas.qianwenaiapi.com/compatible-mode/v1}
      read -r "LLM_MODEL?问答模型 [qwen3.8-flash]: "
      LLM_MODEL=${LLM_MODEL:-qwen3.8-flash}
      read -r -s "API_KEY?API 密钥（输入时不会显示）: "
      echo
      if [[ -z "${API_KEY:-}" ]]; then
        echo '没有输入密钥，跳过问答模型配置，继续启动。' >&2
      else
        # 合并而不是覆盖：保留 .env 里已有的 EMBEDDING_*（向量模型、维度、密钥），只补 LLM_*。
        # 旧版会整份重写并 `export LLM_MODEL EMBEDDING_MODEL`，等于把向量模型写成问答模型名，
        # 已回填的 33,039 条向量会被判成 stale、语义检索直接失效。
        TMP_ENV="$HERE/.env.tmp"
        if [[ -f "$HERE/.env" ]]; then
          grep -vE '^LLM_(BASE_URL|MODEL|API_KEY)=' "$HERE/.env" > "$TMP_ENV" || true
        else
          : > "$TMP_ENV"
        fi
        {
          echo "LLM_BASE_URL=$API_BASE_URL"
          echo "LLM_MODEL=$LLM_MODEL"
          echo "LLM_API_KEY=$API_KEY"
        } >> "$TMP_ENV"
        mv "$TMP_ENV" "$HERE/.env"
        chmod 600 "$HERE/.env"
        echo "问答模型配置已合并写入 $HERE/.env（向量配置保持不变）"
        export LLM_BASE_URL="$API_BASE_URL" LLM_MODEL="$LLM_MODEL" LLM_API_KEY="$API_KEY"
      fi
      ;;
    *) echo '跳过问答模型配置，直接启动。' ;;
  esac
fi

echo "接口地址：${LLM_BASE_URL:-未设置}｜问答模型：${LLM_MODEL:-未设置}｜向量模型：${EMBEDDING_MODEL:-未设置}"

# ---------- 4. 建库（仅首次或 --rebuild） ----------
if [[ ! -f "$DB" || "$ACTION" == "--rebuild" ]]; then
  echo '第 1 步：建立知识库与全文索引…'
  "$PY" "$BUILD"
else
  echo "第 1 步：已存在知识库 $DB，跳过建库（需要重建请加 --rebuild）"
fi

# ---------- 5. 向量回填（可中断续跑；无密钥时跳过，不影响启动） ----------
EMB_KEY="${EMBEDDING_API_KEY:-}"
[[ -n "$EMB_KEY" ]] || EMB_KEY="${LLM_API_KEY:-}"
if [[ "$ACTION" == "--no-index" ]]; then
  echo '第 2 步：按要求跳过向量回填'
elif [[ -z "$EMB_KEY" ]]; then
  echo '第 2 步：未配置密钥，跳过向量回填（关键词检索、溯源、导出均可用）'
else
  echo '第 2 步：先备份知识库，再回填切片向量（可中断，重新运行会续跑）…'
  if ! "$PY" "$BUILD" --index-embeddings; then
    echo '向量回填写入失败：备份已保留在 backups/，修复接口或密钥后重跑本脚本即可，先启动应用。' >&2
  fi
fi

# ---------- 6. 对外监听前置检查（与应用的强制校验保持一致） ----------
BIND_HOST="${POLICY_HOST:-127.0.0.1}"
case "$BIND_HOST" in
  127.0.0.1|localhost|::1|127.*) ;;
  *)
    TOKEN_VALUE="${ACCESS_TOKEN:-}"
    TOKEN_LEN=${#TOKEN_VALUE}
    if [[ "$TOKEN_LEN" -lt 16 || "${RATE_LIMIT_PER_MIN:-120}" == "0" ]]; then
      echo "拒绝启动：POLICY_HOST=$BIND_HOST 不是本机回环，但 ACCESS_TOKEN 不足 16 位或未限流。" >&2
      echo '本机自用请去掉 POLICY_HOST；确需对外提供服务，请同时设置 ACCESS_TOKEN 与 RATE_LIMIT_PER_MIN。' >&2
      exit 1
    fi
    ;;
esac

# ---------- 7. 启动应用 ----------
echo '第 3 步：启动本地应用（按 Ctrl+C 停止）'
exec "$PY" "$APP"
