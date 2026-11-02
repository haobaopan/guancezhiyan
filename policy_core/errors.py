"""Shared model failure types."""


class ModelUnavailable(RuntimeError):
    """模型接口明确拒绝（密钥无效 / 余额不足 / 无权限）。

    与超时、限流不同：这类错误重试没有意义。正确做法是立刻降级为证据模式，
    而不是把错误抛给用户。2026-10-28 真实事故：账号余额用尽后，`/api/ask`
    直接返回 HTTP 502「模型接口调用失败：HTTP 402」，用户既拿不到结论，
    也拿不到证据，更看不出是欠费。
    """
