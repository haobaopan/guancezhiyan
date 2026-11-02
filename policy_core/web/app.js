
(function(){
  "use strict";

  var $ = function(sel){ return document.querySelector(sel); };
  var ESC = {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"};
  function esc(s){ return String(s==null?"":s).replace(/[&<>"']/g, function(c){ return ESC[c]; }); }

  var MODES = {
    ask:{ label:"政策问答", ph:"用一句话提问，例如：沧州市十五规划产业结构调整的主要方向",
          ex:["沧州市十五规划产业结构调整的主要方向","衡水市十三五规划确定的发展目标","甘肃省十四五时期生态环境保护的重点任务"] },
    extract:{ label:"四要素提取", ph:"例如：沧州市十五规划的四要素（量化目标、重点产业、政策工具、责任主体）",
          ex:["沧州市十五规划的四要素","衡水市十三五规划的政策工具与责任主体","甘肃省十四五规划量化目标有哪些"] },
    compare:{ label:"多文件比较", ph:"例如：比较沧州市和衡水市在产业结构调整上的部署",
          ex:["比较沧州市和衡水市在产业结构调整上的部署","比较两市十三五规划的发展目标","对比沧州与衡水在生态治理上的举措"] },
    evolution:{ label:"跨期演变", ph:"例如：沧州市产业结构调整在十五、十一五、十二五期间的演变",
          ex:["沧州市产业结构调整的跨期演变","衡水市发展目标的历次变化","甘肃省生态保护思路的演变"] }
  };

  var STATUS = {
    verified:["结论已通过逐条证据校验","s-ok"],
    partial:["部分主张未通过校验，已省略","s-warn"],
    insufficient:["证据不足或覆盖不全","s-warn"],
    validation_failed:["校验未完成，已降级为原文证据","s-warn"],
    evidence_only:["未启用生成模型，仅返回检索证据","s-info"],
    no_evidence:["未检索到相关证据","s-bad"],
    existence_found:["已找到直接证据","s-ok"],
    not_found_in_complete_scope:["完整检索范围内未发现","s-mute"],
    inconclusive:["证据不足，无法判断","s-mute"],
    elements_verified:["四要素已按引文核对","s-ok"],
    elements_rule:["未配置模型，已按原文整句抽取","s-info"],
    evolution_verified:["跨期要点已按引文核对","s-ok"],
    evolution_evidence:["未配置模型，按原文摘录呈现","s-info"],
    no_evolution:["未找到可跨期对比的材料","s-warn"]
  };

  var state = { mode:"ask", sid:null, latest:null, busy:false };

  /* ---------- 访问令牌（集中式部署时启用） ----------
     管理员发的地址形如 http://内网IP:8765/?token=XXXX，页面加载时读入并记住，
     之后所有请求走 X-Access-Token 头；原文链接则把令牌拼在 query 上。
     本机自用没有令牌时，TOKEN 为空，所有逻辑退化成原来的样子。 */
  var TOKEN = (function(){
    var m = /[?&]token=([^&]*)/.exec(location.search);
    if(m && m[1]){
      var t = decodeURIComponent(m[1]);
      try{ localStorage.setItem("kgsz_token", t); }catch(e){}
      return t;
    }
    try{ var ls = localStorage.getItem("kgsz_token"); if(ls) return ls; }catch(e){}
    /* 扫码进来后服务端会 302 到干净地址，location.search 里没有 token，
       此时从服务端种的副 cookie 里取，保证「复制引用」的链接仍然可用。 */
    var mc = /(?:^|;\s*)gczy_js=([^;]+)/.exec(document.cookie || "");
    return mc ? decodeURIComponent(mc[1]) : "";
  })();

  function apiFetch(url, opt){
    opt = opt || {};
    var h = Object.assign({}, opt.headers || {});
    if(TOKEN) h["X-Access-Token"] = TOKEN;
    opt.headers = h;
    return fetch(url, opt);
  }

  function sourceUrl(planid, frag){
    var u = "/source/" + encodeURIComponent(planid) + "?frag=" + frag;
    return TOKEN ? u + "&token=" + encodeURIComponent(TOKEN) : u;
  }

  function newSid(){
    return (window.crypto && crypto.randomUUID) ? crypto.randomUUID()
      : String(Date.now()) + Math.random().toString(16).slice(2);
  }
  state.sid = localStorage.getItem("kgsz_sid");
  if(!state.sid){ state.sid = newSid(); localStorage.setItem("kgsz_sid", state.sid); }

  function notice(msg, kind){
    var el = $("#notice");
    if(!msg){ el.className = "notice hidden"; el.textContent = ""; return; }
    el.className = "notice" + (kind ? " " + kind : "");
    el.textContent = msg;
  }

  function setBusy(on){
    state.busy = on;
    var b = $("#btn-run");
    b.disabled = on;
    b.textContent = on ? "检索中…" : "检索并分析";
  }

  /* ---------- 模式切换 ---------- */
  function setMode(m){
    state.mode = m;
    Array.prototype.forEach.call($("#tabs").children, function(b){
      b.classList.toggle("on", b.getAttribute("data-mode") === m);
    });
    $("#q").placeholder = MODES[m].ph;
    $("#examples").innerHTML = MODES[m].ex.map(function(x){
      return '<span class="chip" data-q="' + esc(x) + '">' + esc(x) + '</span>';
    }).join("");
  }
  $("#tabs").addEventListener("click", function(e){
    var b = e.target.closest("button[data-mode]");
    if(b) setMode(b.getAttribute("data-mode"));
  });
  $("#examples").addEventListener("click", function(e){
    var c = e.target.closest(".chip");
    if(!c) return;
    $("#q").value = c.getAttribute("data-q");
    $("#q").focus();
  });

  /* ---------- 检索 ---------- */
  async function run(){
    if(state.busy) return;
    var q = $("#q").value.trim();
    if(!q){ notice("请先输入问题。", "warn"); $("#q").focus(); return; }

    setBusy(true);
    notice("正在检索并逐条核对原文…");
    $("#empty").classList.add("hidden");
    $("#result").classList.remove("hidden");
    $("#meta").innerHTML = '<span class="tag-status s-mute">检索中</span>';
    $("#answer").innerHTML = '<div class="skel skel-line" style="width:72%"></div>'
      + '<div class="skel skel-line" style="width:94%"></div>'
      + '<div class="skel skel-line" style="width:83%"></div>';
    ["#table","#periods","#sources"].forEach(function(s){ $(s).innerHTML = ""; });
    $("#ev-head").classList.add("hidden");
    $("#exports").classList.add("hidden");

    var t0 = Date.now();
    try{
      var r = await apiFetch("/api/" + state.mode, {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({ question:q, session_id:state.sid })
      });
      var d = await r.json();
      if(!r.ok){
        if(r.status === 401) throw new Error("需要访问令牌：请在地址后加 ?token=你的令牌再进入，令牌可向管理员索取。");
        throw new Error(d.error || "请求失败");
      }
      var ms = Date.now() - t0;
      state.latest = Object.assign({}, d, { question:q });
      render(d, ms);
      notice("");
    }catch(err){
      $("#answer").innerHTML = "";
      $("#meta").innerHTML = '<span class="tag-status s-bad">请求失败</span>';
      notice("失败：" + err.message, "bad");
    }finally{
      setBusy(false);
    }
  }

  function render(d, ms){
    var st = STATUS[d.status] || [d.status || "已返回结果", "s-mute"];
    var bits = ['<span class="tag-status ' + st[1] + '">' + esc(st[0]) + "</span>"];
    bits.push('<span>耗时 ' + (ms/1000).toFixed(2) + " 秒</span>");
    if(d.model) bits.push('<span class="dot"></span><span>模型 ' + esc(d.model) + "</span>");
    if(d.rewritten_query) bits.push('<span class="dot"></span><span>已按上文补全检索范围</span>');
    if(d.area) bits.push('<span class="dot"></span><span>地区 ' + esc(d.area) + "</span>");
    $("#meta").innerHTML = bits.join("");

    $("#answer").textContent = d.answer || "";
    if(!d.answer) $("#answer").classList.add("hidden");
    else $("#answer").classList.remove("hidden");

    renderTable(d.table);
    renderPeriods(d.periods);
    renderSources(d.sources);

    $("#exports").classList.remove("hidden");
  }

  function renderTable(rows){
    var box = $("#table");
    if(!rows || !rows.length){ box.innerHTML = ""; return; }
    var h = ['<h2 class="sec">四要素对照 <small>' + rows.length + ' 项</small></h2>',
             '<div class="tablewrap"><table class="rep"><thead><tr>',
             "<th>维度</th><th>要素表述</th><th>出处</th><th>原文引文</th>",
             "</tr></thead><tbody>"];
    rows.forEach(function(r){
      var href = sourceUrl(r.planid, r.fragment);
      h.push('<tr><td class="dim">' + esc(r.dim) + "</td>");
      h.push("<td>" + esc(r.text) + "</td>");
      h.push('<td><a href="' + href + '" target="_blank" rel="noopener">' + esc(r.filename) + "</a>"
        + (r.heading ? '<div class="score">' + esc(r.heading) + " · 片段 " + esc(r.fragment) + "</div>" : "") + "</td>");
      h.push('<td class="quote">' + esc(r.quote || "") + "</td></tr>");
    });
    h.push("</tbody></table></div>");
    box.innerHTML = h.join("");
  }

  function renderPeriods(ps){
    var box = $("#periods");
    if(!ps || !ps.length){ box.innerHTML = ""; return; }
    var h = ['<h2 class="sec">按规划期对照 <small>' + ps.length + ' 期</small></h2>', '<div class="tl">'];
    ps.forEach(function(p){
      var yr = (p.start_year && p.start_year < 9998) ? "（" + p.start_year + " 年起）" : "";
      h.push('<div class="tl-item"><div class="tl-card">');
      h.push("<h4>" + esc(p.label) + " <span>" + yr + " · 覆盖 " + esc(p.doc_count) + " 份文件</span></h4>");
      if(p.points && p.points.length){
        h.push("<ul>");
        p.points.forEach(function(x){
          h.push("<li>" + esc(x.text) + (x.source_id != null ? "<i>[" + esc(x.source_id) + "]</i>" : "") + "</li>");
        });
        h.push("</ul>");
      }else{
        h.push('<p class="tl-empty">本期在检索范围内没有直接对应表述。</p>');
      }
      h.push("</div></div>");
    });
    h.push("</div>");
    box.innerHTML = h.join("");
  }

  function renderSources(src){
    var box = $("#sources");
    if(!src || !src.length){
      box.innerHTML = "";
      $("#ev-head").classList.add("hidden");
      return;
    }
    $("#ev-head").classList.remove("hidden");
    $("#ev-count").textContent = src.length + " 条 · 逐条可回溯到原文";

    var h = [];
    src.forEach(function(s){
      var href = sourceUrl(s.planid, s.fragment);
      var chips = [];
      if(s.heading) chips.push("<span>" + esc(s.heading) + "</span>");
      if(s.ordinal != null) chips.push("<span>段落 " + esc(s.ordinal) + "</span>");
      chips.push('<span class="frag">片段 ' + esc(s.fragment) + "</span>");
      if(s.area_name) chips.push("<span>" + esc(s.area_name) + "</span>");
      if(s.fyp_name) chips.push("<span>" + esc(s.fyp_name) + "</span>");

      var score = (typeof s.score === "number" && s.score > 0 && s.score <= 1)
        ? '<span class="score">相关度 ' + s.score.toFixed(2) + "</span>" : "";

      h.push('<article class="src" data-i="' + esc(s.id) + '">');
      h.push('<div class="src-no">' + esc(s.id) + "</div>");
      h.push('<div class="src-body">');
      h.push('<div class="src-top">');
      h.push('<a class="src-title" href="' + href + '" target="_blank" rel="noopener" title="点击回看原文并高亮该片段">' + esc(s.filename) + "</a>");
      h.push(score);
      h.push('<span class="src-actions">');
      h.push('<button class="mini" data-act="more">展开</button>');
      h.push('<button class="mini" data-act="copy" data-href="' + esc(href) + '">复制引用</button>');
      h.push("</span></div>");
      h.push('<div class="chips">' + chips.join("") + "</div>");
      h.push('<p class="src-text">' + esc(s.text) + "</p>");
      h.push("</div></article>");
    });
    box.innerHTML = h.join("");
  }

  $("#sources").addEventListener("click", function(e){
    var btn = e.target.closest("button[data-act]");
    if(!btn) return;
    var card = btn.closest(".src");
    if(btn.getAttribute("data-act") === "more"){
      card.classList.toggle("open");
      btn.textContent = card.classList.contains("open") ? "收起" : "展开";
      return;
    }
    if(btn.getAttribute("data-act") === "copy"){
      var href = btn.getAttribute("data-href");
      var no = card.getAttribute("data-i");
      var title = card.querySelector(".src-title").textContent;
      var chips = Array.prototype.map.call(card.querySelectorAll(".chips > span"), function(x){ return x.textContent; }).join("｜");
      var text = "[" + no + "] " + title + "（" + chips + "）" + location.origin + href;
      var done = function(){ btn.textContent = "已复制"; setTimeout(function(){ btn.textContent = "复制引用"; }, 1400); };
      if(navigator.clipboard && navigator.clipboard.writeText){
        navigator.clipboard.writeText(text).then(done, function(){ fallbackCopy(text, done); });
      }else{ fallbackCopy(text, done); }
    }
  });

  function fallbackCopy(text, cb){
    var ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try{ document.execCommand("copy"); cb(); }catch(e){}
    document.body.removeChild(ta);
  }

  /* ---------- 导出 ---------- */
  $("#exports").addEventListener("click", async function(e){
    var b = e.target.closest("button[data-fmt]");
    if(!b || !state.latest) return;
    var fmt = b.getAttribute("data-fmt");
    var label = b.textContent;
    b.disabled = true; b.textContent = "生成中…";
    try{
      var r = await apiFetch("/api/export/" + fmt, {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({ export_id: state.latest.export_id })
      });
      if(!r.ok){
        var d = await r.json().catch(function(){ return { error:"导出失败" }; });
        throw new Error(d.error || "导出失败");
      }
      var blob = await r.blob();
      var a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "管策智研分析结果." + fmt;
      document.body.appendChild(a); a.click();
      URL.revokeObjectURL(a.href); document.body.removeChild(a);
      notice("已生成 " + fmt.toUpperCase() + " 文件，请在浏览器下载里查看。");
    }catch(err){
      notice("导出失败：" + err.message, "bad");
    }finally{
      b.disabled = false; b.textContent = label;
    }
  });

  /* ---------- 上传 / 文件列表 ---------- */
  $("#btn-upload").addEventListener("click", async function(){
    var input = $("#files");
    if(!input.files || !input.files.length){ notice("请先选择文件。", "warn"); return; }
    var btn = this; btn.disabled = true; btn.textContent = "上传中…";
    notice("正在上传并建立索引…");
    try{
      var fd = new FormData();
      for(var i = 0; i < input.files.length; i++) fd.append("files", input.files[i]);
      var r = await apiFetch("/api/upload", { method:"POST", body:fd });
      var d = await r.json();
      if(!r.ok) throw new Error(d.error || "上传失败");
      notice(d.message || "上传完成。");
      input.value = "";
      refreshFiles(); health();
    }catch(err){
      notice("上传失败：" + err.message, "bad");
    }finally{
      btn.disabled = false; btn.textContent = "上传并建索引";
    }
  });

  async function refreshFiles(){
    try{
      var r = await apiFetch("/api/files");
      var d = await r.json();
      var files = d.files || [];
      $("#list").innerHTML = files.length ? files.map(function(f){
        var meta = [f.top_category, f.region, f.kind, f.chunks + " 段"].filter(Boolean).map(esc).join(" · ");
        return "<li><b>" + esc(f.filename) + "</b><em>" + meta + "</em></li>";
      }).join("") : '<li class="none">尚未导入文件。</li>';
      $("#db-count").textContent = files.length ? "共 " + files.length + " 份可检索文件" : "暂无可检索文件";
    }catch(e){
      $("#list").innerHTML = '<li class="none">读取失败</li>';
      $("#db-count").textContent = "—";
    }
  }

  function setPill(sel, text, cls){
    // 顶部状态标签已从界面移除（属于运维信息，不该占用户视线）。
    // 这里保留空实现，避免 health() 在元素不存在时抛错。
    var el = $(sel);
    if(!el) return;
    el.className = "pill" + (cls ? " " + cls : "");
    var sp = el.querySelector("span");
    if(sp) sp.textContent = text;
  }

  async function health(){
    try{
      var r = await fetch("/healthz");
      var d = await r.json();
      setPill("#pill-db", (d.status === "ok" ? "知识库就绪" : "索引待修复") + " · " + (d.chunks || 0).toLocaleString() + " 段",
              d.status === "ok" ? "" : "off");
      var sem = !!d.semantic_active;
      setPill("#pill-vec", sem ? "语义检索已启用" : "关键词检索", sem ? "" : "off");
      setPill("#pill-llm", d.llm ? "生成模型已配置" : "未配置生成模型", d.llm ? "" : "off");
      $("#db-count").textContent = "共 " + (d.documents || 0) + " 篇 · " + (d.chunks || 0).toLocaleString() + " 段";
    }catch(e){
      setPill("#pill-db", "服务未就绪", "err");
      setPill("#pill-vec", "语义检索", "off");
      setPill("#pill-llm", "生成模型", "off");
    }
  }

  /* ---------- 交互绑定 ---------- */
  $("#btn-run").addEventListener("click", run);
  $("#q").addEventListener("keydown", function(e){
    if((e.metaKey || e.ctrlKey) && e.key === "Enter"){ e.preventDefault(); run(); }
  });
  $("#btn-new").addEventListener("click", function(){
    state.sid = newSid();
    localStorage.setItem("kgsz_sid", state.sid);
    notice("已开启新会话，上一轮的追问上下文已清除。");
  });

  setMode("ask");
  refreshFiles();
  health();
})();
