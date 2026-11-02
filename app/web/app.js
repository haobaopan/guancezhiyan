'use strict';

const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
let state = {}, pid = '', cid = '', csrf = '', configs = {}, vaultKey = null, salt = null;
let authPurpose = 'login', authBusy = false;
let pendingQuestion = '', refreshVersion = 0, sending = false;

function note(message) {
  $('notice').textContent = message || '';
  $('notice').hidden = !message;
}
function feedback(message) { $('authFeedback').textContent = message || ''; }
function navigate(path, replace = false) {
  if (replace) { history.replaceState(null, '', '#' + path); route(); }
  else if (location.hash === '#' + path) route();
  else location.hash = path;
}
function resetAuth(purpose) {
  authPurpose = purpose;
  $('password').value = '';
  $('confirmPassword').value = '';
  feedback('');
  const register = purpose === 'register';
  $('authEyebrow').textContent = register ? '开启你的研究空间' : '欢迎回来';
  $('authTitle').textContent = register ? '创建你的账户' : '登录管策智研';
  $('authDescription').textContent = register ? '设置密码，创建独立的政策研究账户。' : '使用邮箱和密码进入你的研究空间。';
  $('password').autocomplete = register ? 'new-password' : 'current-password';
  $('confirmPassword').hidden = !register;
  $('confirmPasswordLabel').hidden = !register;
  $('confirmPassword').required = register;
  $('authSubmit').textContent = register ? '注册并进入' : '登录';
  $('authSwitchLabel').textContent = register ? '已经有账户？' : '还没有账户？';
  $('authSwitch').textContent = register ? '前往登录' : '立即注册';
  $('authSwitch').href = register ? '#/login' : '#/register';
}
function route() {
  let path = location.hash.slice(1) || (state.user ? '/workspace' : '/login');
  if (!state.user && path === '/home') { navigate('/login', true); return; }
  const protectedPage = path === '/workspace' || path.startsWith('/settings');
  if (protectedPage && !state.user) { navigate('/login', true); return; }
  if (state.user && (path === '/login' || path === '/register')) { navigate('/workspace', true); return; }
  const known = ['/home', '/login', '/register', '/workspace', '/settings/account', '/settings/models', '/settings/memory'];
  if (!known.includes(path)) { navigate(state.user ? '/workspace' : '/login', true); return; }
  const isAuth = path === '/login' || path === '/register';
  if (isAuth && (authPurpose !== path.slice(1) || $('login').hidden)) resetAuth(path.slice(1));
  $('home').hidden = path !== '/home';
  $('login').hidden = !isAuth;
  $('workspace').hidden = path !== '/workspace';
  $('settingsPage').hidden = !path.startsWith('/settings/');
  $('guestActions').hidden = !!state.user;
  $('memberActions').hidden = !state.user;
  const panels = {account: 'accountPanel', models: 'settings', memory: 'memoriesDialog'};
  for (const [tab, id] of Object.entries(panels)) $(id).hidden = path !== '/settings/' + tab;
  for (const link of document.querySelectorAll('[data-settings]')) {
    const active = path === '/settings/' + link.dataset.settings;
    link.classList.toggle('selected', active);
    if (active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
  }
  const titles = {'/home': '规划文本智能体', '/login': '登录', '/register': '注册', '/workspace': '研究工作台', '/settings/account': '账户设置', '/settings/models': '模型配置', '/settings/memory': '记忆管理'};
  document.title = '管策智研 · ' + titles[path];
}
function endSession() {
  refreshVersion++;
  lock(false);
  state = {};
  pid = cid = csrf = '';
  pendingQuestion = '';
  for (const id of ['messages', 'projects', 'conversations', 'privateFiles', 'projectFiles', 'memories', 'sourceChunks']) $(id).replaceChildren();
  for (const id of ['question', 'homeQuestion', 'memoryContent', 'memoryCategory', 'displayName', 'accountEmail', 'email', 'password', 'confirmPassword', 'file', 'fileArea', 'fileAdmin', 'fileFyp']) $(id).value = '';
  $('fileCategory').value = '其他';
  $('userLabel').textContent = '';
  if ($('sourceDialog').open) $('sourceDialog').close();
  navigate('/login', true);
}
async function api(action, body = {}) {
  const userId = state.user?.user_id;
  const response = await fetch('/api/action', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, credentials: 'same-origin', body: JSON.stringify({action, ...body})});
  const data = await response.json();
  if (userId && userId !== state.user?.user_id) throw Error('登录状态已变更，请重新操作');
  if (response.status === 401 && state.user) endSession();
  if (!response.ok) throw Error(data.error || '请求失败');
  if (data.csrf) csrf = data.csrf;
  return data;
}
async function refresh() {
  const version = ++refreshVersion;
  const query = new URLSearchParams({project_id: pid, conversation_id: cid});
  const response = await fetch('/api/state?' + query, {credentials: 'same-origin'});
  if (version !== refreshVersion) return;
  if (response.status === 401) {
    if (state.user) endSession(); else { state = {}; route(); }
    return;
  }
  const data = await response.json();
  if (version !== refreshVersion) return;
  if (!response.ok) throw Error(data.error || '加载失败');
  if (state.user?.user_id && state.user.user_id !== data.user?.user_id) { endSession(); await refresh(); return; }
  state = data;
  csrf = data.csrf || csrf;
  render();
  route();
}
function emptyList(id, message) {
  const paragraph = document.createElement('p');
  paragraph.className = 'empty-list';
  paragraph.textContent = message;
  $(id).append(paragraph);
}
function itemButtons(list, target, idKey, onClick) {
  $(target).replaceChildren();
  for (const item of list || []) {
    const button = document.createElement('button');
    button.textContent = item.name || item.title || '未命名';
    const selected = item[idKey] === (target === 'projects' ? pid : cid);
    button.classList.toggle('selected', selected);
    button.setAttribute('aria-pressed', String(selected));
    button.onclick = () => Promise.resolve(onClick(item)).catch(error => note(error.message));
    $(target).append(button);
  }
  if (!list?.length) emptyList(target, target === 'projects' ? '还没有项目，创建你的第一个研究项目。' : pid ? '点击 ＋ 开始一段新对话。' : '选择项目后查看对话。');
}
function render() {
  itemButtons(state.projects, 'projects', 'project_id', async project => { pid = project.project_id; cid = ''; await refresh(); });
  itemButtons(state.conversations, 'conversations', 'conversation_id', async conversation => { cid = conversation.conversation_id; await refresh(); });
  const project = (state.projects || []).find(project => project.project_id === pid);
  $('projectCount').textContent = (state.projects || []).length;
  $('projectTitle').textContent = project?.name || '选择或创建项目';
  $('projectDescription').textContent = project?.description || '';
  $('projectSummary').textContent = typeof state.project_summary === 'object' ? state.project_summary?.summary || '暂无摘要' : state.project_summary || '暂无摘要';
  $('projectActions').hidden = !project;
  $('summaryDetails').hidden = !project;
  $('conversationActions').hidden = !cid;
  $('newConversation').disabled = !pid;
  $('sendBtn').disabled = !cid || sending;
  $('question').placeholder = cid ? '输入问题或当前项目任务…' : pid ? '点击左侧 ＋ 新建对话后，开始提问' : '先创建或选择一个研究项目';
  $('workspaceEmpty').hidden = !!state.messages?.length;
  $('workspaceHint').textContent = cid ? '写下你的问题，选择研究模式，开始一段有依据的对话。' : project ? '项目已准备就绪。点击左侧 ＋ 创建对话，开始你的研究。' : '先创建或选择一个项目，归集资料，让每一次研究都有迹可循。';
  $('settingsProject').textContent = project ? '当前项目：' + project.name : '尚未选择项目，可管理全局偏好。';
  $('extractMemory').disabled = !cid;
  $('memoryScope').querySelector('[value="project"]').disabled = !pid;
  if (!pid) $('memoryScope').value = 'global';
  $('messages').replaceChildren();
  for (const message of state.messages || []) {
    const element = document.createElement('div');
    element.className = 'message ' + message.role;
    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = (message.role === 'user' ? '你' : '管策智研') + ' · ' + (message.created_at || '');
    const body = document.createElement('div');
    body.textContent = message.content;
    element.append(meta, body);
    if (message.result?.sources?.length) {
      const sources = document.createElement('details');
      const title = document.createElement('summary');
      title.textContent = '引用原文（' + message.result.sources.length + '）';
      sources.append(title);
      for (const source of message.result.sources) {
        const section = document.createElement('div');
        section.className = 'file';
        const link = document.createElement('a');
        link.textContent = '[' + source.id + '] ' + (source.filename || source.title || '原文') + ' · ' + (source.heading || '');
        link.href = '/api/source?' + new URLSearchParams({scope: source.scope || 'public', planid: source.planid || source.document_id, project_id: source.scope === 'project' ? pid : '', frag: source.fragment || ''});
        link.onclick = event => { event.preventDefault(); showSource(link.href, source.fragment).catch(error => note(error.message)); };
        const text = document.createElement('p');
        text.textContent = source.text;
        section.append(link, text);
        sources.append(section);
      }
      element.append(sources);
    }
    if (message.role === 'assistant') for (const [format, label] of [['docx', 'Word'], ['xlsx', 'Excel'], ['csv', 'CSV'], ['pptx', 'PPT']]) {
      const button = document.createElement('button');
      button.textContent = '导出 ' + label;
      button.onclick = () => download(format, message.message_id);
      element.append(button);
    }
    if (message.result?.warnings?.length) {
      const warning = document.createElement('p');
      warning.className = 'small';
      warning.textContent = message.result.warnings.join('；');
      element.append(warning);
    }
    if (message.result?.status) {
      const status = document.createElement('p');
      status.className = 'small';
      status.textContent = '结果状态：' + message.result.status;
      element.append(status);
    }
    $('messages').append(element);
  }
  renderFiles(state.private_files || [], 'privateFiles', 'private');
  renderFiles(state.project_files || state.files || [], 'projectFiles', 'project');
  renderMemories();
  $('displayName').value = state.user?.display_name || '';
  $('accountEmail').value = state.user?.email || '';
  $('userLabel').textContent = state.user?.display_name || state.user?.email || '';
  $('autoMemory').checked = state.user?.memory_extraction_enabled !== 0;
  if (pendingQuestion) { $('question').value = pendingQuestion; pendingQuestion = ''; }
}
async function showSource(url, fragment) {
  const userId = state.user?.user_id;
  const response = await fetch(url, {credentials: 'same-origin'});
  if (response.status === 401) { endSession(); throw Error('登录已过期，请重新登录'); }
  const data = await response.json();
  if (!userId || userId !== state.user?.user_id) return;
  if (!response.ok) throw Error(data.error || '读取原文失败');
  $('sourceTitle').textContent = data.title || '原文';
  $('sourceNote').textContent = data.note || '';
  $('sourceChunks').replaceChildren();
  for (const chunk of data.chunks || []) {
    const element = document.createElement('div');
    element.className = 'message';
    element.textContent = (chunk.heading || '正文') + ' · 片段 ' + chunk.fragment + '\n' + chunk.text;
    if (String(chunk.fragment) === String(fragment)) { element.classList.add('highlight'); element.id = 'sourceSelected'; }
    $('sourceChunks').append(element);
  }
  $('sourceDialog').showModal();
  requestAnimationFrame(() => $('sourceSelected')?.scrollIntoView({block: 'center'}));
}
function renderFiles(files, id, scope) {
  $(id).replaceChildren();
  for (const file of files) {
    const element = document.createElement('div');
    element.className = 'file';
    element.innerHTML = '<b>' + esc(file.source_name || file.title) + '</b><div class="small">' + esc(file.status || '') + ' · ' + esc(file.top_category || '') + ' · 切片 ' + esc(file.chunks || 0) + ' / 向量 ' + esc(file.embedded || 0) + ' · ' + esc(file.created_at || '') + '</div>';
    for (const action of ['view_file', 'reindex', 'delete_file']) {
      const button = document.createElement('button');
      button.textContent = {view_file: '查看原件', reindex: '重新索引', delete_file: '删除'}[action];
      button.onclick = async () => {
        if (action === 'delete_file' && !confirm('删除此文档及关联索引？')) return;
        try {
          if (action === 'view_file') {
            const link = document.createElement('a');
            link.href = '/api/file?' + new URLSearchParams({scope, project_id: scope === 'project' ? pid : '', file_id: file.file_id || file.document_id});
            link.download = file.source_name || '文件';
            link.click();
            return;
          }
          button.disabled = true;
          await api(action, {scope, project_id: pid, file_id: file.file_id || file.document_id, embedding_config: configs.embedding});
          await refresh();
        } catch (error) { note(error.message); } finally { button.disabled = false; }
      };
      element.append(button);
    }
    $(id).append(element);
  }
  if (!files.length) emptyList(id, scope === 'project' ? '选定项目后，在此归集研究材料。' : '还没有上传私人资料。');
}
function renderMemories() {
  $('memories').replaceChildren();
  for (const memory of state.memories || []) {
    const element = document.createElement('div');
    element.className = 'memory';
    element.innerHTML = '<b>' + esc(memory.project_id ? '项目记忆' : '全局记忆') + ' · ' + esc(memory.category) + '</b><p>' + esc(memory.content) + '</p><div class="small">重要程度 ' + esc(memory.importance) + ' / 置信度 ' + esc(memory.confidence) + ' · ' + esc(memory.updated_at) + ' · 来源 ' + esc(memory.source_conversation_id || '手动') + '</div>';
    const edit = document.createElement('button');
    edit.textContent = '编辑';
    edit.onclick = async () => {
      try {
        const content = prompt('编辑记忆', memory.content);
        if (content !== null) { await api('memory_update', {memory_id: memory.memory_id, project_id: memory.project_id, content, category: memory.category}); await refresh(); }
      } catch (error) { note(error.message); }
    };
    const remove = document.createElement('button');
    remove.textContent = '删除';
    remove.onclick = async () => {
      try {
        if (confirm('删除此记忆？')) { await api('memory_delete', {memory_id: memory.memory_id, project_id: memory.project_id}); await refresh(); }
      } catch (error) { note(error.message); }
    };
    element.append(edit, remove);
    $('memories').append(element);
  }
  if (!state.memories?.length) emptyList('memories', '暂无记忆。你可以手动添加稳定偏好或项目决策。');
}
function bind(id, handler) {
  $(id).onclick = async event => {
    event.preventDefault();
    const button = $(id);
    if (button.disabled) return;
    button.disabled = true;
    try { await handler(); } catch (error) { note(error.message); }
    finally { button.disabled = false; }
  };
}
$('authForm').onsubmit = async event => {
  event.preventDefault();
  if (authBusy) return;
  if (!$('authForm').reportValidity()) return;
  if (authPurpose === 'register' && $('password').value !== $('confirmPassword').value) {
    feedback('两次输入的密码不一致'); return;
  }
  authBusy = true;
  feedback('');
  $('authSubmit').disabled = true;
  $('authSubmit').textContent = authPurpose === 'register' ? '正在注册…' : '正在登录…';
  try {
    await api(authPurpose, {email: $('email').value.trim(), password: $('password').value});
    $('password').value = '';
    $('confirmPassword').value = '';
    await refresh();
    if (!state.user) throw Error('登录状态未能加载，请重新登录');
    navigate('/workspace', true);
    note('');
  } catch (error) { feedback(error.message); }
  finally { authBusy = false; $('authSubmit').disabled = false; $('authSubmit').textContent = authPurpose === 'register' ? '注册并进入' : '登录'; }
};
function beginResearch(mode) {
  if (mode) { $('mode').value = mode; $('scope').value = 'public'; }
  pendingQuestion = $('homeQuestion').value.trim();
  if (state.user) { if (pendingQuestion) { $('question').value = pendingQuestion; pendingQuestion = ''; } navigate('/workspace'); }
  else navigate('/login');
}
$('homeForm').onsubmit = event => { event.preventDefault(); beginResearch('ask'); };
for (const link of document.querySelectorAll('[data-entry-mode]')) link.onclick = event => { event.preventDefault(); beginResearch(link.dataset.entryMode); };
for (const button of document.querySelectorAll('[data-example]')) button.onclick = () => { $('homeQuestion').value = button.dataset.example; $('homeQuestion').focus(); };

bind('logoutBtn', async () => { await api('logout'); endSession(); note('已安全退出。'); });
bind('newProject', async () => {
  const name = prompt('项目名称');
  if (name?.trim()) { const result = await api('project_create', {name: name.trim()}); pid = result.project_id || result.project?.project_id || ''; cid = ''; await refresh(); }
});
bind('editProject', async () => {
  if (!pid) return;
  const project = state.projects.find(project => project.project_id === pid);
  const name = prompt('项目名称', project.name);
  if (name === null) return;
  const description = prompt('项目描述/目标', project.description || '');
  if (description === null) return;
  const status = prompt('状态 active / archived', project.status || 'active');
  if (status === null) return;
  await api('project_update', {project_id: pid, name, description, status});
  await refresh();
});
bind('deleteProject', async () => {
  if (pid && confirm('删除项目后，关联对话、文件和项目记忆将停止访问与检索，磁盘保留供备份恢复。确认删除？')) { await api('project_delete', {project_id: pid}); pid = cid = ''; await refresh(); }
});
bind('newConversation', async () => {
  if (!pid) throw Error('请先选择项目');
  const result = await api('conversation_create', {project_id: pid, title: '新对话'});
  cid = result.conversation_id || result.conversation?.conversation_id || '';
  await refresh();
  $('question').focus();
});
bind('renameConversation', async () => {
  if (!cid) return;
  const title = prompt('对话名称');
  if (title) { await api('conversation_update', {conversation_id: cid, title}); await refresh(); }
});
bind('deleteConversation', async () => {
  if (cid && confirm('删除当前对话和相关历史记录？')) { await api('conversation_delete', {conversation_id: cid}); cid = ''; await refresh(); }
});
$('mode').onchange = () => {
  if ($('mode').value === 'chat' && $('scope').value === 'public') $('scope').value = 'project';
  else if ($('mode').value !== 'chat' && $('scope').value === 'project') $('scope').value = 'public';
};
$('chatForm').onsubmit = async event => {
  event.preventDefault();
  if (sending) return;
  try {
    if (!cid) throw Error('请先选择项目并创建对话');
    if ($('mode').value === 'chat' && !configs.chat) { navigate('/settings/models', true); throw Error('请在模型配置中解锁并配置聊天模型'); }
    sending = true;
    $('sendBtn').disabled = true;
    $('sendBtn').textContent = '研究中…';
    const content = $('question').value;
    await api('send_message', {conversation_id: cid, content, mode: $('mode').value, scope: $('scope').value, model_config: configs.chat, embedding_config: configs.embedding, public_embedding_config: configs.public_embedding});
    if ($('question').value === content) $('question').value = '';
    await refresh();
    note('');
  } catch (error) { note(error.message); }
  finally { sending = false; $('sendBtn').disabled = !cid; $('sendBtn').textContent = '发送 ↗'; }
};
$('question').onkeydown = event => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey) && !event.isComposing) { event.preventDefault(); if (!sending) $('chatForm').requestSubmit(); } };
bind('uploadBtn', async () => {
  const file = $('file').files[0];
  if (!file) throw Error('请选择文件');
  if (file.size > 20 * 1024 * 1024) throw Error('文件超过 20MB');
  const scope = $('uploadScope').value;
  if (scope === 'project' && !pid) throw Error('请先选择项目');
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = '';
  for (let i = 0; i < bytes.length; i += 32768) binary += String.fromCharCode(...bytes.subarray(i, i + 32768));
  await api('upload_file', {scope, project_id: scope === 'project' ? pid : null, filename: file.name, metadata: {top_category: $('fileCategory').value, area_name: $('fileArea').value, admin_level: $('fileAdmin').value, fyp_name: $('fileFyp').value}, content_base64: btoa(binary), embedding_config: configs.embedding});
  $('file').value = '';
  await refresh();
  note('上传完成。');
});
async function download(format, messageId) {
  const userId = state.user?.user_id;
  try {
    const response = await fetch('/api/action', {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: JSON.stringify({action: 'export', format, conversation_id: cid, message_id: messageId})});
    if (response.status === 401) { endSession(); throw Error('登录已过期，请重新登录'); }
    if (!response.ok) { const data = await response.json(); throw Error(data.error || '导出失败'); }
    const blob = await response.blob();
    if (!userId || userId !== state.user?.user_id) return;
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = '管策智研.' + format;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) { note(error.message); }
}
bind('saveAccount', async () => { await api('update_account', {display_name: $('displayName').value}); await refresh(); note('账户信息已保存。'); });
bind('editSummary', async () => {
  if (!pid) throw Error('请先选择项目');
  const summary = prompt('编辑项目摘要', $('projectSummary').textContent);
  if (summary !== null) { await api('summary_edit', {project_id: pid, summary}); await refresh(); }
});
$('memoryForm').onsubmit = async event => {
  event.preventDefault();
  $('addMemory').disabled = true;
  try {
    if ($('memoryScope').value === 'project' && !pid) throw Error('请先在工作台选择项目');
    await api('memory_save', {project_id: $('memoryScope').value === 'project' ? pid : null, category: $('memoryCategory').value || 'note', content: $('memoryContent').value});
    $('memoryContent').value = '';
    await refresh();
    note('记忆已保存。');
  } catch (error) { note(error.message); } finally { $('addMemory').disabled = false; }
};
bind('extractMemory', async () => {
  if (!pid || !cid) throw Error('请先在工作台选择项目与对话');
  if (!configs.chat) throw Error('请先解锁并配置聊天模型');
  await api('memory_extract', {conversation_id: cid, project_id: pid, model_config: configs.chat});
  await refresh();
  note('记忆整理完成。');
});
$('autoMemory').onchange = async () => {
  const previous = state.user?.memory_extraction_enabled !== 0;
  $('autoMemory').disabled = true;
  try { await api('memory_toggle', {memory_extraction_enabled: $('autoMemory').checked}); await refresh(); }
  catch (error) { $('autoMemory').checked = previous; note(error.message); }
  finally { $('autoMemory').disabled = false; }
};

// Browser-only encrypted vault. Passwords and CryptoKeys are never persisted.
function vaultId() { if (!state.user?.user_id) throw Error('请先登录再使用模型配置'); return 'models:' + state.user.user_id; }
function openVault() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open('gczy-model-vault', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('settings');
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(Error('无法打开本地加密存储'));
  });
}
async function vaultTransaction(mode, operation) {
  const id = vaultId();
  const db = await openVault();
  return new Promise((resolve, reject) => {
    const transaction = db.transaction('settings', mode);
    const request = operation(transaction.objectStore('settings'), id);
    transaction.oncomplete = () => { db.close(); resolve(request.result); };
    transaction.onerror = transaction.onabort = () => { db.close(); reject(Error('读写本地模型配置失败')); };
  });
}
async function derive(password, vaultSalt) {
  if (!crypto.subtle) throw Error('加密功能需要 HTTPS 或 localhost');
  if (password.length < 10) throw Error('解锁口令至少 10 个字符');
  const material = await crypto.subtle.importKey('raw', new TextEncoder().encode(password), 'PBKDF2', false, ['deriveKey']);
  return crypto.subtle.deriveKey({name: 'PBKDF2', salt: vaultSalt, iterations: 600000, hash: 'SHA-256'}, material, {name: 'AES-GCM', length: 256}, false, ['encrypt', 'decrypt']);
}
function vaultStatus() {
  $('modelFields').disabled = !vaultKey;
  $('vaultStatus').textContent = vaultKey ? '本地配置已解锁 · 仅当前会话可用' : '本地配置已锁定';
}
async function unlock() {
  const userId = state.user?.user_id;
  const saved = await vaultTransaction('readonly', (store, id) => store.get(id));
  const vaultSalt = saved ? new Uint8Array(saved.salt) : crypto.getRandomValues(new Uint8Array(16));
  const key = await derive($('vaultPassword').value, vaultSalt);
  let decrypted = {};
  if (saved) {
    try {
      const raw = await crypto.subtle.decrypt({name: 'AES-GCM', iv: new Uint8Array(saved.iv)}, key, new Uint8Array(saved.cipher));
      decrypted = JSON.parse(new TextDecoder().decode(raw));
    } catch (error) { throw Error('解锁口令不正确或本地配置损坏'); }
  }
  if (!userId || userId !== state.user?.user_id) throw Error('登录状态已变更，请重新解锁');
  vaultKey = key;
  salt = vaultSalt;
  configs = decrypted;
  $('vaultPassword').value = '';
  loadConfig();
  vaultStatus();
  note('本地配置已解锁；退出或锁定将清除运行时密钥。');
}
function lock(showNotice = true) {
  vaultKey = null;
  salt = null;
  configs = {};
  $('apiKey').value = '';
  $('vaultPassword').value = '';
  loadConfig();
  vaultStatus();
  if (showNotice) note('本地配置已锁定。');
}
function currentConfig() {
  for (const field of $('modelFields').elements) {
    if (field.willValidate && !field.reportValidity()) throw Error('请检查模型参数的取值范围');
  }
  const parameters = JSON.parse($('extraParameters').value || '{}');
  if (!parameters || Array.isArray(parameters) || typeof parameters !== 'object') throw Error('附加参数必须是 JSON 对象');
  return {provider: $('provider').value, base_url: $('baseUrl').value.trim(), model: $('modelName').value.trim(), api_key: $('apiKey').value.trim(), temperature: Number($('temperature').value), max_tokens: Number($('maxTokens').value), dimensions: Number($('dimensions').value), context_budget: Number($('contextBudget').value), parameters};
}
function loadConfig() {
  const config = configs[$('configType').value] || {};
  $('provider').value = config.provider || 'siliconflow';
  $('baseUrl').value = config.base_url || 'https://api.siliconflow.cn/v1';
  $('modelName').value = config.model || ($('configType').value === 'public_embedding' ? 'BAAI/bge-m3' : '');
  $('apiKey').value = config.api_key || '';
  $('temperature').value = config.temperature ?? 0;
  $('maxTokens').value = config.max_tokens || 2048;
  $('dimensions').value = config.dimensions || 1024;
  $('contextBudget').value = config.context_budget || 12000;
  $('extraParameters').value = JSON.stringify(config.parameters || {});
}
bind('unlockBtn', unlock);
bind('lockBtn', () => lock());
$('configType').onchange = loadConfig;
$('provider').onchange = () => {
  const urls = {siliconflow: 'https://api.siliconflow.cn/v1', deepseek: 'https://api.deepseek.com/v1', bailian: 'https://dashscope.aliyuncs.com/compatible-mode/v1'};
  if (urls[$('provider').value]) $('baseUrl').value = urls[$('provider').value];
};
bind('saveConfig', async () => {
  if (!vaultKey) throw Error('请先解锁本地配置，再填写并保存');
  const config = currentConfig();
  if (!config.api_key || !config.model) throw Error('请填写 API Key 和模型名');
  const key = vaultKey, id = vaultId();
  const updated = {...configs, [$('configType').value]: config};
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const cipher = await crypto.subtle.encrypt({name: 'AES-GCM', iv}, key, new TextEncoder().encode(JSON.stringify(updated)));
  if (key !== vaultKey || id !== vaultId()) throw Error('配置已锁定，请重新解锁后保存');
  await vaultTransaction('readwrite', (store, vault) => store.put({version: 1, salt: Array.from(salt), iv: Array.from(iv), cipher: Array.from(new Uint8Array(cipher))}, vault));
  if (key === vaultKey) configs = updated;
  note('配置已在本浏览器加密保存。');
});
bind('deleteConfig', async () => {
  if (!confirm('删除本浏览器为当前账户保存的全部模型配置？')) return;
  await vaultTransaction('readwrite', (store, id) => store.delete(id));
  lock(false);
  note('本地模型配置已删除。');
});
bind('testConfig', async () => {
  await api('test_model', {purpose: $('configType').value === 'chat' ? 'chat' : 'embedding', model_config: currentConfig()});
  note('API 测试成功。');
});
window.addEventListener('hashchange', () => {
  note('');
  route();
  window.scrollTo(0, 0);
  if (!$('login').hidden) $('authTitle').focus({preventScroll: true});
});
// Resolve the HttpOnly session before exposing protected navigation or panels.
refresh().catch(error => { route(); note(error.message); });
