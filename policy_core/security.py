"""Security operations for one policy engine instance."""
import json
import sys


class SecurityMixin:
    def audit(self, event):
        """请求留痕：谁、什么时候、问了什么、返回什么状态，逐行 JSONL 可事后核查。"""
        try:
            with self._AUDIT_LOCK:
                if self.AUDIT_LOG.exists() and self.AUDIT_LOG.stat().st_size > self.AUDIT_MAX_BYTES:
                    self.AUDIT_LOG.replace(self.AUDIT_LOG.with_suffix('.1.jsonl'))
                with self.AUDIT_LOG.open('a', encoding='utf-8') as handle:
                    handle.write(json.dumps(event, ensure_ascii=False)+'\n')
        except OSError as exc:
            self.log.warning('审计日志写入失败：%s', exc)


    def is_loopback(self, host):
        host = (host or '').strip().strip('[]').lower()
        return host in ('127.0.0.1', 'localhost', '::1') or host.startswith('127.')


    def enforce_access_control(self):
        """对外监听必须带访问控制，缺一项就拒绝启动（不提供旁路开关）。

    回环地址不需要令牌；一旦绑定到非回环地址，令牌和限流都必须显式配置，
    否则等于把政策原文库和上传入口直接暴露在网络上。
    """
        if self.is_loopback(self.HOST):
            return
        problems = []
        if len(self.ACCESS_TOKEN) < 16:
            problems.append('ACCESS_TOKEN 未设置或过短（至少 16 位，建议 secrets.token_urlsafe(24)）')
        if self.RATE_LIMIT_PER_MIN <= 0:
            problems.append('RATE_LIMIT_PER_MIN=0 表示不限流，对外监听时不允许')
        if problems:
            print(f'拒绝启动：监听地址 {self.HOST}:{self.PORT} 不是本机回环，但访问控制不完整。', file=sys.stderr)
            for item in problems:
                print(f'  - {item}', file=sys.stderr)
            print('本机自用请设 POLICY_HOST=127.0.0.1；确需对外提供服务，请同时设置令牌与限流，'
                  '并前置反向代理做 HTTPS。示例：', file=sys.stderr)
            print('  POLICY_HOST=0.0.0.0 ACCESS_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))") '
                  'RATE_LIMIT_PER_MIN=60 python3 应用源码_示例知识库版.py', file=sys.stderr)
            raise SystemExit(2)


