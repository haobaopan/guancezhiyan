#!/bin/zsh
# ⑤ 一张码走天下（公网·任何网络都能扫）.command
#
# 用途：生成一张「不管手机连校园网、连 4G/5G、还是电脑自己点开，都能马上进」的二维码。
#
# 为什么必须走公网：
#   校园 Wi-Fi 的无线侧禁止客户端互访（AP 隔离），手机和电脑即使连同一个 SSID，
#   TCP 也连不通，表现就是「一直转圈、最后无法访问」。这不是二维码的问题，
#   换任何局域网地址（IP 或 .local）都打不开。
#
# 原理：cloudflared 从这台电脑主动向外建立加密隧道，Cloudflare 把公网请求送回来。
#      不需要公网 IP、不需要路由器端口映射、不需要域名。
#
# 与 ④ 的分工：
#   ④ 出局域网码，手机须与电脑同一个 WiFi，校园网开了终端隔离就打不开；
#   ⑤ 出公网码，手机用流量、连别的 WiFi 都能开，两者复用同一枚固定令牌。
#
# 安全：应用会强制要求「令牌 ≥16 位」+「限流 > 0」，缺一项直接拒绝启动。

set -u
cd "$(dirname "$0")" || exit 1

APP="应用源码_示例知识库版.py"
PORT="${POLICY_PORT:-8765}"
STATE="$HOME/Library/Application Support/管策智研"
TOKEN_FILE="$STATE/访问令牌.txt"
QR_PNG="$STATE/公网二维码.png"
QR_URL_FILE="$STATE/公网码地址.txt"
DESK_PNG="$HOME/Desktop/手机扫码（公网·任何网络都能开）.png"
TLOG="/tmp/管策智研_公网隧道.log"
ALOG="/tmp/管策智研_公网服务.log"
QR_JS="/tmp/管策智研_公网二维码.js"
CF_BIN=""
TOKEN=""
APP_PID=""
CF_PID=""

blue()  { printf '\033[1;34m%s\033[0m\n' "$*"; }
green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
red()   { printf '\033[1;31m%s\033[0m\n' "$*"; }
yellow(){ printf '\033[1;33m%s\033[0m\n' "$*"; }
dim()   { printf '\033[2m%s\033[0m\n' "$*"; }

cleanup() {
  echo
  dim "正在关闭…"
  [[ -n "$CF_PID"  ]] && kill "$CF_PID"  2>/dev/null
  [[ -n "$APP_PID" ]] && kill "$APP_PID" 2>/dev/null
  sleep 1
  dim "已停止。公网地址同时失效，下次启动会换一个新的。"
}
trap cleanup EXIT INT TERM

echo
blue "=========================================================="
blue "   一张码走天下　（公网地址，任何网络、任何设备都能开）"
blue "=========================================================="
echo

# ---------- 0. 检查文件 ----------
if [[ ! -f "$APP" ]]; then
  red "找不到 $APP。"
  echo "请把本脚本和「应用源码_示例知识库版.py」放在同一个文件夹里，再双击。"
  echo
  read -r "?按回车关闭…"
  exit 1
fi

mkdir -p "$STATE"

# ---------- 1. 固定令牌（与 ④ 共用同一枚，换脚本不用换码） ----------
if [[ -s "$TOKEN_FILE" ]]; then
  TOKEN="$(cat "$TOKEN_FILE")"
else
  TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"
  printf '%s' "$TOKEN" > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
fi
if (( ${#TOKEN} < 16 )); then
  red "令牌文件内容异常（少于 16 位）。请删掉下面这个文件后重跑："
  echo "  $TOKEN_FILE"
  echo
  read -r "?按回车关闭…"
  exit 1
fi
echo "令牌：${TOKEN:0:6}…（共 ${#TOKEN} 位，与 ④ 共用，固定不变）"

# ---------- 2. 准备隧道程序 ----------
for P in "$HOME/bin/cloudflared" /opt/homebrew/bin/cloudflared /usr/local/bin/cloudflared; do
  if [[ -z "$CF_BIN" && -x "$P" ]]; then CF_BIN="$P"; fi
done

if [[ -z "$CF_BIN" ]]; then
  echo
  echo "第一次使用需要下载隧道小工具（cloudflared，约 19 MB，只下一次）。"
  read -r "ANS?现在下载吗？[Y/n] "
  if [[ "${ANS:-Y}" == [nN]* ]]; then
    red "已取消。"
    exit 1
  fi
  case "$(uname -m)" in
    arm64) PKG="cloudflared-darwin-arm64.tgz" ;;
    *)     PKG="cloudflared-darwin-amd64.tgz" ;;
  esac
  mkdir -p "$HOME/bin"
  echo "正在下载（网络不稳时会自动续传）…"
  for attempt in 1 2 3; do
    if curl -fL -C - -m 300 -o /tmp/cloudflared.tgz \
        "https://github.com/cloudflare/cloudflared/releases/latest/download/$PKG"; then
      break
    fi
    dim "第 $attempt 次下载中断，正在续传…"
  done
  if ! tar xzf /tmp/cloudflared.tgz -C /tmp 2>/dev/null; then
    red "下载或解压失败。请检查网络后重试。"
    echo
    read -r "?按回车关闭…"
    exit 1
  fi
  cp /tmp/cloudflared "$HOME/bin/cloudflared"
  chmod +x "$HOME/bin/cloudflared"
  CF_BIN="$HOME/bin/cloudflared"
  rm -f /tmp/cloudflared.tgz
  green "隧道工具已就绪。"
fi

# ---------- 3. 清理上次残留 ----------
# 3a. 端口占用：一律自动停掉旧服务，不再询问（旧服务是我们自己上次启动的，留着只会让页面和码对不上）
OLD="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1)"
if [[ -n "$OLD" ]]; then
  echo "端口 $PORT 上有上次启动的服务（PID $OLD），正在自动停掉…"
  kill "$OLD" 2>/dev/null
  sleep 2
  OLD="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1)"
  if [[ -n "$OLD" ]]; then
    kill -9 "$OLD" 2>/dev/null
    sleep 1
  fi
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    red "端口 $PORT 仍被占用，停不下来。"
    echo "请重启电脑，或在「活动监视器」里结束占用该端口的程序，再双击本文件。"
    echo
    read -r "?按回车关闭…"
    exit 1
  fi
  green "旧服务已停止。"
fi

# 3b. 遗留的隧道进程：多个同时存在会互相抢同一个域名，表现为手机扫开是 530 错误页
LEFT="$(pgrep -f "cloudflared tunnel" 2>/dev/null | tr '\n' ' ')"
LEFT="${LEFT// /}"
if [[ -n "$LEFT" ]]; then
  echo "清理上次残留的隧道进程（PID $LEFT）…"
  for p in $LEFT; do kill "$p" 2>/dev/null; done
  sleep 2
  for p in $(pgrep -f "cloudflared tunnel" 2>/dev/null); do kill -9 "$p" 2>/dev/null; done
  sleep 1
  green "隧道进程已清理。"
fi

# ---------- 4. 启动应用 ----------
echo
echo "正在启动系统…"
POLICY_HOST=0.0.0.0 ACCESS_TOKEN="$TOKEN" RATE_LIMIT_PER_MIN=120 \
  POLICY_PORT="$PORT" python3 "$APP" > "$ALOG" 2>&1 &
APP_PID=$!

READY=""
for n in $(seq 1 40); do
  if ! kill -0 "$APP_PID" 2>/dev/null; then break; fi
  if curl -s -m 2 --noproxy '*' "http://127.0.0.1:$PORT/healthz" 2>/dev/null | grep -q '"chunks"'; then
    READY="1"; break
  fi
  sleep 0.5
done

if [[ -z "$READY" ]]; then
  red "系统没有启动成功。日志最后 20 行："
  echo "----------------------------------------------------------"
  tail -20 "$ALOG" 2>/dev/null || echo "（没有日志）"
  echo "----------------------------------------------------------"
  read -r "?按回车关闭…"
  exit 1
fi
green "系统已启动。"

# ---------- 5. 建立公网隧道 ----------
echo
echo "正在建立公网隧道（通常 5–15 秒）…"
rm -f "$TLOG"

PUB=""
LIMIT_HIT=""
ATTEMPT=1
MAX_ATTEMPT=5
# 每次失败后递增的等待秒数：Cloudflare 的免费隧道配额是按时间窗口滚动的，
# 等得越久越容易恢复；手点得越勤，窗口内累计次数越多，反而恢复得更慢。
WAITS=(45 90 150 210 300)

while : ; do
  # 用 HTTP/2（TCP）而不是默认的 QUIC：校园网/公司网常对 UDP 443 做限速或丢弃，
  # 实测 QUIC 会在 40 秒左右超时断开并把节点从香港切到洛杉矶（延迟从 50ms 涨到 180ms）。
  # 走 TCP 更稳，代价是首字节略慢一点。
  "$CF_BIN" tunnel --url "http://127.0.0.1:$PORT" --protocol http2 --no-autoupdate > "$TLOG" 2>&1 &
  CF_PID=$!

  for n in $(seq 1 60); do
    PUB="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TLOG" 2>/dev/null | head -1)"
    [[ -n "$PUB" ]] && break
    if ! kill -0 "$CF_PID" 2>/dev/null; then break; fi
    sleep 1
  done

  [[ -n "$PUB" ]] && break

  # 没拿到地址：只在「限流」时自动重试，其它错误（断网等）立刻报出来，不无谓等待
  if grep -q "429" "$TLOG" 2>/dev/null; then
    kill "$CF_PID" 2>/dev/null
    CF_PID=""
    if (( ATTEMPT >= MAX_ATTEMPT )); then
      LIMIT_HIT="1"
      break
    fi
    W="${WAITS[$ATTEMPT]}"
    echo
    yellow "Cloudflare 暂时不给新地址（免费隧道限流 429，短时间重启次数太多）。"
    echo "这是免费额度限制，不是配置出错，等一会儿会自动恢复。"
    echo "本脚本会自己等并重试——请不要关窗口，也不要再双击这个文件。"
    echo
    for s in $(seq "$W" -1 1); do
      printf "\r  第 %d/%d 次重试，%3d 秒后自动开始…    " "$((ATTEMPT+1))" "$MAX_ATTEMPT" "$s"
      sleep 1
    done
    printf "\r%70s\r" ""
    ATTEMPT=$((ATTEMPT+1))
    rm -f "$TLOG"
    continue
  fi
  break
done

if [[ -z "$PUB" ]]; then
  if [[ -n "$LIMIT_HIT" ]]; then
    red "隧道一直没建立成功：Cloudflare 免费隧道限流（429），多次重试仍未放行。"
    echo
    echo "这是 Cloudflare 对免账号隧道的配额限制，与你的代码、密钥、二维码都无关。"
    echo "配额按时间窗口滚动，通常 10–30 分钟自行恢复。"
    echo
    echo "这段时间请注意：不要反复双击本文件。每点一次就是一次新的申请，"
    echo "会让窗口内的次数更多，恢复得更慢。过一会儿双击一次就好。"
    echo
    echo "在等待期间，本机仍然可用：http://127.0.0.1:$PORT"
  else
    red "隧道没有建立成功。日志最后 20 行："
    echo "----------------------------------------------------------"
    tail -20 "$TLOG" 2>/dev/null
    echo "----------------------------------------------------------"
    echo "常见原因：电脑没联网，或所在网络封了 UDP 443。"
    echo "换个网络（比如手机热点）再试一次。"
  fi
  echo
  read -r "?按回车关闭…"
  exit 1
fi

FULL="$PUB/?token=$TOKEN"
printf '%s' "$FULL" > "$QR_URL_FILE"

# 等隧道真正可以对外应答，再出码，避免"码有了但还打不开"
OK=""
for n in $(seq 1 20); do
  CODE="$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$PUB/healthz" 2>/dev/null)"
  if [[ "$CODE" == "200" ]]; then OK="1"; break; fi
  sleep 1
done
if [[ -z "$OK" ]]; then
  dim "提示：隧道已建立，但外部应答还慢，稍等几秒再扫。"
fi

# ---------- 6. 生成二维码 ----------
cat > "$QR_JS" <<'JS'
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

QR_OK=""
rm -f "$QR_PNG"
if osascript -l JavaScript "$QR_JS" "$FULL" "$QR_PNG" >/dev/null 2>&1 && [[ -f "$QR_PNG" ]]; then
  # 补白边：扫码器需要四周留白才认得出
  W=$(sips -g pixelWidth "$QR_PNG" 2>/dev/null | awk '/pixelWidth/{print $2}')
  if [[ -n "$W" ]]; then
    T=$(( W / 8 ))
    sips --padToHeightWidth $(( W + T * 2 )) $(( W + T * 2 )) --padColor FFFFFF "$QR_PNG" >/dev/null 2>&1 || true
  fi
  QR_OK="1"
  cp "$QR_PNG" "$DESK_PNG" 2>/dev/null || true
  open "$QR_PNG" 2>/dev/null || true
fi

# 桌面只留这一张有效码：把以前留下的旧版「手机扫码*.png」挪进旧二维码/，
# 否则手上会有好几张码，每张指向不同时刻的地址，扫哪个都打不开，还很难分辨。
if [[ -n "$QR_OK" ]]; then
  ARCDIR="$HOME/Desktop/旧二维码"
  MOVED=""
  for f in "$HOME/Desktop"/手机扫码*.png(N); do
    [[ "$f" == "$DESK_PNG" ]] && continue
    mkdir -p "$ARCDIR" 2>/dev/null || continue
    if mv "$f" "$ARCDIR/" 2>/dev/null; then MOVED="$MOVED
    · $(basename "$f")"; fi
  done
  if [[ -n "$MOVED" ]]; then
    dim "已把桌面上的旧码挪进 $ARCDIR（它们指向已关闭的隧道，扫了会报错）：$MOVED"
  fi
fi

# ---------- 7. 交付信息 ----------
echo
green "=========================================================="
green "   这张码任何网络都能扫（校园网 / 流量 / 任何设备）"
green "=========================================================="
echo
echo "  地址（也可直接发到手机点开）："
blue "  $FULL"
echo

# 把地址放进剪贴板，省得手动选中复制（草料后台里直接 Cmd+V 就行）
if printf '%s' "$FULL" | pbcopy 2>/dev/null; then
  green "  ✅ 这个地址已经复制到剪贴板（去草料后台 Cmd+V 即可）"
  echo
fi

# 若用户填过活码管理页地址，直接打开，少一步找入口
HOT_FILE="$STATE/活码管理页.txt"
HOT_PAGE=""
if [[ -s "$HOT_FILE" ]]; then
  HOT_PAGE="$(head -1 "$HOT_FILE" | tr -d '[:space:]')"
fi
if [[ -n "$QR_OK" ]]; then
  echo "  二维码文件： $QR_PNG"
  echo "  桌面副本　： $DESK_PNG"
else
  red "  二维码生成失败，请把上面那行地址手动发到手机。"
fi
echo
dim "──── 三条必读 ────"
dim "1. 地址里带着令牌，谁拿到谁就能用。别贴到公开群里。"
dim "2. 这个地址由免费隧道临时分配，关掉本窗口就失效，下次启动会换一个新的。"
dim "   所以别直接印这张图发出去——走下面的「活码」，那张图才永久有效。"
dim "3. 想让它「永远不变」也可以买域名（详见 部署与运维.md 5.3–5.5 节），"
dim "   把域名填进 公网地址.txt，④ 就会优先用它出码。"
echo

if [[ -n "$HOT_PAGE" ]]; then
  dim "──── 活码更新（三步，已自动打开后台）────"
  dim "1. 浏览器里打开的就是你的草料活码编辑页"
  dim "2. 把「网址/链接」那一栏清空，Cmd+V 粘贴（新地址已在剪贴板）"
  dim "3. 点保存 —— 墙上那张旧码立刻指向新地址，图片不用换"
  echo
  open "$HOT_PAGE" >/dev/null 2>&1 || dim "（自动打开失败，请手动访问：$HOT_PAGE）"
else
  dim "──── 让码永久不变：草料活码（只需建一次）────"
  dim "首次：打开 cli.im 注册 → 新建「活码」→ 类型选「网址」→ 粘贴上面地址 → 保存并下载图片"
  dim "以后：每次启动本脚本后，回到那个活码，把「网址」改成新地址并保存，图不用换"
  dim "想再省一步：把草料活码的编辑页地址粘进下面这个文件，本脚本以后会自动打开它"
  dim "  $HOT_FILE"
  echo
fi
dim "──── 出问题看这里 ────"
dim "  一直转圈打不开 → 先在这台电脑浏览器打开上面地址，能开说明是对方网络问题"
dim "  提示未授权     → 地址里的 ?token= 被截断，重新扫码"
dim "  用一会儿就断   → 免费隧道有时限，关掉重开"
echo
green "服务运行中。要停止，请按 Ctrl+C，或直接关掉这个窗口。"
echo

# ---------- 8. 保持运行 ----------
while kill -0 "$APP_PID" 2>/dev/null && kill -0 "$CF_PID" 2>/dev/null; do
  sleep 5
done

red "服务已意外退出。日志最后 10 行："
tail -10 "$ALOG" 2>/dev/null
echo
read -r "?按回车关闭…"
