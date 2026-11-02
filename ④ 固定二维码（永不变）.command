#!/bin/zsh
# ④ 固定二维码（永不变）.command
#
# 用途：生成一张「每次启动都一样、可以提前打印/截图贴出去」的二维码。
#
# 为什么码会「一次一个样」：
#   令牌若每次随机生成，地址里又写字面 IP（10.28.x.x），
#   令牌一换、IP 一换（校园网 DHCP 天天变），码就废了。
#
# 本脚本怎么做到不变：
#   1. 令牌只生成一次，存在 ~/Library/Application Support/管策智研/访问令牌.txt
#      以后每次都读这同一个，不再随机。
#   2. 地址不写 IP，改写本机的主机名（形如 yuyuedeMacBook-Air.local）。
#      IP 变了，主机名不变，所以码不用换。
#   3. 地址和令牌都没变 → 直接沿用上次那张 PNG，一个像素都不重画。
#
# 什么时候码会变（脚本会明确告诉你）：
#   - 手动删了令牌文件
#   - 换了电脑，或改了「共享」里的电脑名称
#   - 你自己在 公网地址.txt 里填了新的公网域名
#
# 安全：对外监听必须带令牌 + 限流，否则程序拒绝启动。令牌文件权限 600，只有你能读。

set -u

cd "$(dirname "$0")" || exit 1

APP="应用源码_示例知识库版.py"
PORT="${POLICY_PORT:-8765}"

STATE="$HOME/Library/Application Support/管策智研"
TOKEN_FILE="$STATE/访问令牌.txt"
URL_FILE="$STATE/固定码地址.txt"
PUB_FILE="$STATE/公网地址.txt"
QR_PNG="$STATE/固定二维码.png"
QR_ALT_PNG="$STATE/备用二维码_主机名.local.png"
QR_JS="/tmp/管策智研_固定码.js"
LOG="/tmp/管策智研_固定码.log"
SERVER_PID=""

blue()  { printf '\033[1;34m%s\033[0m\n' "$*"; }
green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
red()   { printf '\033[1;31m%s\033[0m\n' "$*"; }
dim()   { printf '\033[2m%s\033[0m\n' "$*"; }

cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null
    wait "$SERVER_PID" 2>/dev/null
  fi
}
trap cleanup EXIT INT TERM

echo
blue "=========================================================="
blue "   固定二维码　（生成一次，以后每次扫都进同一个地址）"
blue "=========================================================="
echo

# ---------- 0. 前置检查 ----------
if [[ ! -f "$APP" ]]; then
  red "找不到 $APP。"
  echo "请把本脚本和「应用源码_示例知识库版.py」放在同一个文件夹里，再双击。"
  echo
  read -r "?按回车关闭…"
  exit 1
fi

mkdir -p "$STATE"

# ---------- 1. 令牌：只生成一次 ----------
if [[ -s "$TOKEN_FILE" ]]; then
  TOKEN="$(cat "$TOKEN_FILE")"
  TOKEN_NEW=""
else
  TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"
  printf '%s' "$TOKEN" > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
  TOKEN_NEW="1"
fi

if (( ${#TOKEN} < 16 )); then
  red "令牌文件内容异常（少于 16 位）。请删掉下面这个文件后重跑："
  echo "  $TOKEN_FILE"
  echo
  read -r "?按回车关闭…"
  exit 1
fi

echo "令牌：${TOKEN:0:6}…（共 ${#TOKEN} 位，固定不变）"

# ---------- 2. 地址：优先公网，其次局域网 IP，最后才用 .local 主机名 ----------
# 为什么 IP 排在 .local 前面：微信内置浏览器不认 .local（Bonjour 只在苹果生态内解析），
# 用 .local 生成的码在微信里扫不开。IP 虽然会随 DHCP 变，但至少扫得开。
PUBLIC_BASE=""
if [[ -s "$PUB_FILE" ]]; then
  PUBLIC_BASE="$(head -1 "$PUB_FILE" | tr -d '[:space:]')"
fi

LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
MDNS_HOST="$(scutil --get LocalHostName 2>/dev/null || true)"

MODE=""
URL=""
URL_FALLBACK=""

if [[ -n "$PUBLIC_BASE" ]]; then
  MODE="公网"
  PUBLIC_BASE="${PUBLIC_BASE%/}"
  URL="${PUBLIC_BASE}/?token=${TOKEN}"
elif [[ -n "$LAN_IP" ]]; then
  MODE="局域网·IP"
  URL="http://${LAN_IP}:${PORT}/?token=${TOKEN}"
  # 备用码给 .local：万一 IP 变了，苹果设备（Safari）仍能靠主机名进来。
  if [[ -n "$MDNS_HOST" ]] && ping -c 1 -W 1000 "${MDNS_HOST}.local" >/dev/null 2>&1; then
    URL_FALLBACK="http://${MDNS_HOST}.local:${PORT}/?token=${TOKEN}"
  fi
elif [[ -n "$MDNS_HOST" ]] && ping -c 1 -W 1000 "${MDNS_HOST}.local" >/dev/null 2>&1; then
  MODE="局域网·主机名"
  URL="http://${MDNS_HOST}.local:${PORT}/?token=${TOKEN}"
fi

if [[ -z "$URL" ]]; then
  red "没找到可用的局域网地址（可能没连 WiFi）。"
  echo "连上网络后再双击本文件。"
  echo
  read -r "?按回车关闭…"
  exit 1
fi

# 备用码已在上面按模式设好（主码是 IP 时备用码给 .local），这里不再覆盖。

# ---------- 3. 和上次比对：一样就不重画 ----------
OLD_URL=""
if [[ -s "$URL_FILE" ]]; then
  OLD_URL="$(head -1 "$URL_FILE" | tr -d '[:space:]')"
fi

SAME=""
if [[ "$URL" == "$OLD_URL" && -f "$QR_PNG" ]]; then
  SAME="1"
fi

printf '%s' "$URL" > "$URL_FILE"

# ---------- 4. 生成二维码 ----------
cat >"$QR_JS" <<'JS'
ObjC.import('CoreImage');
ObjC.import('Foundation');
ObjC.import('AppKit');
function run(argv) {
  var text = argv[0], out = argv[1];
  var d = $.NSString.stringWithString(text).dataUsingEncoding($.NSUTF8StringEncoding);
  var f = $.CIFilter.filterWithName('CIQRCodeGenerator');
  f.setValueForKey(d, 'inputMessage');
  f.setValueForKey('M', 'inputCorrectionLevel');
  var img = f.outputImage.imageByApplyingTransform($.CGAffineTransformMakeScale(12, 12));
  var cg = $.CIContext.context.createCGImageFromRect(img, img.extent);
  var rep = $.NSBitmapImageRep.alloc.initWithCGImage(cg);
  var png = rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $.NSDictionary.dictionary);
  png.writeToFileAtomically($(out), true);
  return 'ok';
}
JS

# 给二维码补白边（quiet zone）。扫码器需要四周留白才认得出，
# 打印到纸上尤其明显。系统自带的 sips 即可，输出是确定性的。
pad_qr() {
  local f="$1" w h t
  [[ -f "$f" ]] || return 0
  w=$(sips -g pixelWidth "$f" 2>/dev/null | awk '/pixelWidth/{print $2}')
  h=$(sips -g pixelHeight "$f" 2>/dev/null | awk '/pixelHeight/{print $2}')
  [[ -z "$w" || -z "$h" ]] && return 0
  t=$(( w * 12 / 100 ))
  sips --padToHeightWidth $((h + t * 2)) $((w + t * 2)) --padColor FFFFFF "$f" >/dev/null 2>&1 || true
}

QR_OK=""
if [[ -n "$SAME" ]]; then
  green "地址与令牌都没变 → 沿用上次那张二维码（没有重画）。"
  QR_OK="1"
  if [[ -n "$URL_FALLBACK" && ! -f "$QR_ALT_PNG" ]]; then
    osascript -l JavaScript "$QR_JS" "$URL_FALLBACK" "$QR_ALT_PNG" >/dev/null 2>&1 || true
    pad_qr "$QR_ALT_PNG"
  fi
else
  rm -f "$QR_PNG"
  if osascript -l JavaScript "$QR_JS" "$URL" "$QR_PNG" >/dev/null 2>&1 && [[ -f "$QR_PNG" ]]; then
    pad_qr "$QR_PNG"
    QR_OK="1"
  fi
  if [[ -n "$URL_FALLBACK" ]]; then
    rm -f "$QR_ALT_PNG"
    osascript -l JavaScript "$QR_JS" "$URL_FALLBACK" "$QR_ALT_PNG" >/dev/null 2>&1 || true
    pad_qr "$QR_ALT_PNG"
  fi
fi

if [[ -z "$QR_OK" ]]; then
  red "二维码生成失败。不影响服务启动，可直接用下面的网址。"
fi

# ---------- 5. 端口检查 ----------
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo
  echo "端口 $PORT 上有上次启动的服务，正在自动停掉它…"
  OLDPID="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1)"
  [[ -n "$OLDPID" ]] && kill "$OLDPID" 2>/dev/null
  sleep 2
  OLDPID="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1)"
  if [[ -n "$OLDPID" ]]; then
    kill -9 "$OLDPID" 2>/dev/null
    sleep 1
  fi
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    red "端口 $PORT 仍被占用，停不下来。"
    echo "请重启电脑，或在「活动监视器」里结束占用该端口的程序，再双击本文件。"
    echo
    read -r "?按回车关闭…"
    exit 1
  fi
  green "旧进程已停止。"
fi

# ---------- 6. 启动服务 ----------
echo
echo "正在启动服务…"

ACCESS_TOKEN="$TOKEN" \
POLICY_HOST=0.0.0.0 \
RATE_LIMIT_PER_MIN=120 \
POLICY_PORT="$PORT" \
python3 "$APP" >"$LOG" 2>&1 &
SERVER_PID=$!

READY=""
for n in $(seq 1 40); do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then break; fi
  if curl -s -m 2 --noproxy '*' "http://127.0.0.1:${PORT}/healthz" 2>/dev/null | grep -q '"chunks"'; then
    READY="1"
    break
  fi
  sleep 0.5
done

if [[ -z "$READY" ]]; then
  red "服务没能起来。日志最后 20 行："
  echo "----------------------------------------------------------"
  tail -20 "$LOG" 2>/dev/null || echo "（没有日志）"
  echo "----------------------------------------------------------"
  read -r "?按回车关闭…"
  exit 1
fi

green "服务已启动。"

# ---------- 7. 展示二维码 ----------
if [[ -n "$QR_OK" ]]; then
  open "$QR_PNG"
fi

echo
green "================== 这张码永远扫同一个地址 =================="
echo
echo "  二维码文件（可以直接打印、贴墙上、发给别人）："
echo "    $QR_PNG"
echo
echo "  里面写的是："
echo "    $URL"
echo
if [[ -n "$SAME" ]]; then
  echo "  ✅ 和上次完全一致，可以放心继续用之前打印的那张。"
else
  echo "  ⚠️  这是新生成的（第一次跑，或地址变了）。以后就固定下来了。"
fi
echo
green "============================================================"
echo

if [[ -n "$URL_FALLBACK" ]]; then
  dim "──── 备用：万一 IP 变了 ────"
  dim "苹果设备（相机 / Safari）能用主机名进入，IP 变了也不用换码："
  echo "  $URL_FALLBACK"
  dim "备用二维码： $QR_ALT_PNG"
  dim "注意：微信内置浏览器解析不了 .local，这个备用地址只对苹果原生浏览器有效。"
  echo
fi

if [[ "$MODE" == "公网" ]]; then
  dim "当前用的是 公网地址.txt 里填的固定域名。"
  dim "要让这个域名真的指向本机，需要另外配「命名隧道」或部署到服务器，"
  dim "详见 部署与运维.md 5.3–5.5 节。"
  echo
fi

dim "──── 三条必读 ────"
dim "1. 手机要和电脑在同一个 WiFi（或电脑连手机热点）。"
dim "2. 学校/公司 WiFi 若开了「终端隔离」，设备之间互相看不见，局域网码必然打不开；"
dim "   这时改用 ⑤ 一张码走天下（公网），或让电脑连手机热点。"
dim "3. 服务只在你这个窗口开着时可用。关窗口 = 停止服务，二维码本身仍然有效，"
dim "   下次双击本文件，地址和码都不会变。"
dim "4. 扫码进入后，地址栏会自动变成干净的 http://…/ ，不再挂一长串 ?token=…"
dim "   （令牌已收进浏览器的 Cookie，刷新、收藏、切页面都不会掉线）。"
echo
echo "服务正在运行。要停止：关掉这个窗口，或按 Ctrl+C。"
echo

wait "$SERVER_PID"
