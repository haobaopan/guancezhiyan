#!/bin/zsh
# ============================================================
#  【最简单】双击这一个文件就能用。
#  它只做三件事：检查 → 启动 → 自动打开浏览器。
#  不建库、不改配置、不动知识库里的任何东西。
# ============================================================
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1
export POLICY_KB_DIR="$HERE"
PORT="${POLICY_PORT:-8765}"
APP="$HERE/应用源码_示例知识库版.py"

big() {
  print ""
  print "================================================================"
  print "  $1"
  print "================================================================"
  print ""
}

say() { print "  $1"; }

big "管策智研 · 正在启动，请等 5 秒，不要关这个窗口"

# ---------- 1. 文件在不在 ----------
if [[ ! -f "$APP" ]]; then
  big "出错了：找不到主程序文件"
  say "请确认「应用源码_示例知识库版.py」和这个文件在同一个文件夹里。"
  say "按任意键关闭。"
  read -k 1
  exit 1
fi
if [[ ! -f "$HERE/knowledge.sqlite3" ]]; then
  big "出错了：找不到知识库文件 knowledge.sqlite3"
  say "请确认它和这个文件在同一个文件夹里（大小约 287 MB）。"
  say "按任意键关闭。"
  read -k 1
  exit 1
fi

# ---------- 2. Python 有没有 ----------
PY="$(command -v python3 || true)"
if [[ -z "$PY" ]]; then
  big "出错了：这台电脑没有 Python"
  say "请先安装 Python 3（python.org 下载），装好后再双击一次。"
  say "按任意键关闭。"
  read -k 1
  exit 1
fi

# ---------- 3. 已经在运行？那就直接开网页 ----------
# 判断服务是否真的在跑：不看端口通不通，而看 /healthz 有没有返回 JSON。
# （只看「连接成功」会误判：本机可能有代理占着端口，连上了却不是这个程序。）
is_up() {
  curl -s -m 2 "http://127.0.0.1:$1/healthz" 2>/dev/null | grep -q '"chunks"'
}

if is_up "$PORT"; then
  say "服务已经在运行了，直接帮你打开网页。"
  [[ -n "${NO_BROWSER:-}" ]] || open "http://127.0.0.1:$PORT" 2>/dev/null || true
  big "浏览器已打开。这个窗口不要关，用完再关。"
  exit 0
fi

say "程序：$APP"
say "知识库：knowledge.sqlite3"
say "网址：http://127.0.0.1:$PORT"
print ""

# ---------- 4. 启动 ----------
"$PY" "$APP" &
APP_PID=$!

# 关闭窗口 / 按 Control+C 时，把后台服务一起收掉，避免残留
cleanup() {
  print ""
  print "  正在停止服务…"
  kill "$APP_PID" 2>/dev/null || true
  wait "$APP_PID" 2>/dev/null || true
  print "  已停止。下次要用，再双击一次这个文件就行。"
  exit 0
}
trap cleanup INT TERM HUP

# ---------- 5. 等服务起来，自动打开浏览器 ----------
READY=0
for i in {1..40}; do
  if is_up "$PORT"; then
    READY=1
    break
  fi
  if ! kill -0 "$APP_PID" 2>/dev/null; then
    break
  fi
  sleep 0.5
done

if (( READY == 1 )); then
  [[ -n "${NO_BROWSER:-}" ]] || open "http://127.0.0.1:$PORT" 2>/dev/null || true
  print ""
  print "================================================================"
  print "  启动成功！浏览器应该已经自己打开了。"
  print ""
  print "  如果没有自动打开，请手动打开浏览器（Safari 或 Chrome），"
  print "  在最上面的地址栏里输入：  127.0.0.1:$PORT   然后按回车。"
  print ""
  print "  这个窗口【不要关】，它就是程序本身。"
  print "  用完了要关掉：点这个窗口，按住 Control 键不放，再按一下 C 键。"
  print "  或者直接关掉这个窗口也可以。"
  print ""
  print "  不知道怎么操作？回到文件夹，双击「① 先看这个（使用说明）」。"
  print "================================================================"
  print ""
else
  big "启动失败了，请把上面这几行字拍照发给帮你装的人"
fi

wait "$APP_PID" 2>/dev/null || true
print ""
print "  服务已停止。下次要用，再双击一次这个文件就行。"
