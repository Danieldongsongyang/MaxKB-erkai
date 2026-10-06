# coding=utf-8
"""
@project: MaxKB
@file: dpin_actual_usage.py
@desc: 读取本次模型响应的真实用量，不使用估算或模型实例上的共享状态
"""

MAX_TOKENS = 2**31 - 1


class ActualUsageUnavailable(ValueError):
    def __init__(self):
        # 错误可记录，但不能附带候选人、输入、回复或上游凭据。
        super().__init__("MAXKB_ACTUAL_USAGE_UNAVAILABLE")


def actual_token_usage(message):
    candidates = []
    metadata = getattr(message, "usage_metadata", None)
    if isinstance(metadata, dict) and metadata:
        candidates.append(metadata)
    response = getattr(message, "response_metadata", None)
    if isinstance(response, dict):
        usage = response.get("token_usage")
        if isinstance(usage, dict) and usage:
            candidates.append(usage)

    selected = None
    for usage in candidates:
        # 若两组字段同时存在，必须分别合法且一致。缺少字段不能默认成零。
        pairs = []
        for prompt_key, completion_key in [
            ("input_tokens", "output_tokens"),
            ("prompt_tokens", "completion_tokens"),
        ]:
            if prompt_key not in usage and completion_key not in usage:
                continue
            prompt = usage.get(prompt_key)
            completion = usage.get(completion_key)
            if (
                type(prompt) is not int
                or type(completion) is not int
                or prompt < 0
                or completion < 0
                or prompt > MAX_TOKENS
                or completion > MAX_TOKENS - prompt
            ):
                raise ActualUsageUnavailable()
            pairs.append((prompt, completion))
        if not pairs or any(pair != pairs[0] for pair in pairs):
            raise ActualUsageUnavailable()
        if "total_tokens" in usage:
            total = usage["total_tokens"]
            if type(total) is not int or total != sum(pairs[0]):
                raise ActualUsageUnavailable()
        if selected is not None and selected != pairs[0]:
            raise ActualUsageUnavailable()
        selected = pairs[0]

    if selected is None:
        raise ActualUsageUnavailable()
    return selected
