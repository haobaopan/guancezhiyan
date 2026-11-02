#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""兼容入口；实现已按职责迁入 policy_core/。

直接运行方式与旧版一致。完整拆分前源码保存在
backups/source_split_20261102_v1/应用源码_示例知识库版.py。
模块定位、依赖及修改指南见 docs/源码导航与模块拆分_V1.md。
"""
from policy_core import PolicyEngine
from policy_core.config import first_dir, load_env_file
from policy_core.http import make_handler, parse_multipart as _parse_multipart

_engine = PolicyEngine(load_env=True)
Handler = make_handler(_engine)


def __getattr__(name):
    """保留评测脚本使用的 db/retrieve/导出函数等旧名称。"""
    return getattr(_engine, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_engine)))


def parse_multipart(handler):
    return _parse_multipart(_engine, handler)


def main():
    _engine.main()


if __name__ == '__main__':
    main()
