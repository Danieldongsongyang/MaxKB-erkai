# coding=utf-8
"""
@project: MaxKB
@file: dpin_actual_usage.py
@desc: 读取本次模型响应的真实用量，不使用估算或模型实例上的共享状态
"""

from threading import RLock

MAX_TOKENS = 2**31 - 1


class ActualUsageUnavailable(ValueError):
    def __init__(self):
        # 错误可记录，但不能附带候选人、输入、回复或上游凭据。
        super().__init__("MAXKB_ACTUAL_USAGE_UNAVAILABLE")


def actual_token_usage(message):
    candidates = []
    metadata = getattr(message, "usage_metadata", None)
    if metadata is not None:
        if not isinstance(metadata, dict) or not metadata:
            raise ActualUsageUnavailable()
        candidates.append(metadata)
    response = getattr(message, "response_metadata", None)
    if response is not None and not isinstance(response, dict):
        raise ActualUsageUnavailable()
    if isinstance(response, dict):
        usage = response.get("token_usage")
        if "token_usage" in response:
            if not isinstance(usage, dict) or not usage:
                raise ActualUsageUnavailable()
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


def actual_cached_tokens(message, prompt_tokens):
    """缓存命中必须有本次响应的显式事实；缺字段返回未知，绝不补零。"""
    candidates = []
    metadata = getattr(message, "usage_metadata", None)
    response = getattr(message, "response_metadata", None)
    sources = []
    if isinstance(metadata, dict):
        sources.append((metadata, "input_token_details", "cache_read"))
    if isinstance(response, dict) and isinstance(response.get("token_usage"), dict):
        raw = response["token_usage"]
        sources.append((raw, "prompt_tokens_details", "cached_tokens"))
        if "prompt_cache_hit_tokens" in raw:
            candidates.append(raw["prompt_cache_hit_tokens"])
        if "prompt_cache_miss_tokens" in raw:
            miss = raw["prompt_cache_miss_tokens"]
            hit = raw.get("prompt_cache_hit_tokens")
            if type(miss) is not int or type(hit) is not int or miss < 0 or hit + miss != prompt_tokens:
                raise ActualUsageUnavailable()
    for usage, details_key, value_key in sources:
        details = usage.get(details_key)
        if details is None:
            continue
        if not isinstance(details, dict):
            raise ActualUsageUnavailable()
        if value_key in details:
            candidates.append(details[value_key])
    if not candidates:
        return None
    if any(type(value) is not int or value < 0 or value > prompt_tokens for value in candidates):
        raise ActualUsageUnavailable()
    if any(value != candidates[0] for value in candidates):
        raise ActualUsageUnavailable()
    return candidates[0]


class RequestActualUsage:
    """每次请求新建；登记实际调用、完成校验，再冻结总量，不能挂到缓存模型上。

    begin 必须在调用前执行。调用失败需 fail；未完成、重复完成或任何校验失败均不可恢复为成功。
    只保存调用标识、用途和数量，不保存消息内容、配置正文或凭据。
    """

    def __init__(self):
        self._lock = RLock()
        self._calls = {}
        self._response_ids = set()
        self._failed = False
        self._sealed = False

    def _reject(self):
        self._failed = True
        raise ActualUsageUnavailable()

    def begin(self, purpose):
        with self._lock:
            if self._failed or self._sealed or purpose not in {"reply", "problem_optimization", "long_term_memory"}:
                self._reject()
            call_id = str(len(self._calls) + 1)
            self._calls[call_id] = {"call_id": call_id, "purpose": purpose, "tokens": None}
            return call_id

    def complete(self, call_id, message):
        with self._lock:
            call = self._calls.get(call_id)
            if self._failed or self._sealed or call is None or call["tokens"] is not None:
                self._reject()
            try:
                tokens = actual_token_usage(message)
                cached_tokens = actual_cached_tokens(message, tokens[0])
            except ActualUsageUnavailable:
                self._reject()
            # 同一上游响应不能当作两个实际调用计费。无响应 ID 时仍执行调用票据的单次完成校验。
            response_id = getattr(message, "id", None)
            if response_id:
                if not isinstance(response_id, str) or response_id in self._response_ids:
                    self._reject()
                self._response_ids.add(response_id)
            prompt = sum(c["tokens"][0] for c in self._calls.values() if c["tokens"] is not None) + tokens[0]
            completion = sum(c["tokens"][1] for c in self._calls.values() if c["tokens"] is not None) + tokens[1]
            if prompt + completion > MAX_TOKENS:
                self._reject()
            call["tokens"] = tokens
            call["cached_tokens"] = cached_tokens
            return tokens

    def fail(self, call_id):
        with self._lock:
            self._failed = True

    def invoke(self, purpose, model, messages):
        call_id = self.begin(purpose)
        try:
            result = model.invoke(messages)
            self.complete(call_id, result)
            return result
        except Exception:
            self.fail(call_id)
            raise ActualUsageUnavailable() from None

    def finalize(self):
        with self._lock:
            if self._failed or not self._calls or any(c["tokens"] is None for c in self._calls.values()):
                self._reject()
            self._sealed = True
            return (
                sum(c["tokens"][0] for c in self._calls.values()),
                sum(c["tokens"][1] for c in self._calls.values()),
            )

    def cached_tokens(self):
        with self._lock:
            self.finalize()
            values = [c["cached_tokens"] for c in self._calls.values()]
            return None if any(v is None for v in values) else sum(values)

    def details(self):
        with self._lock:
            self.finalize()
            return [
                {
                    "call_id": c["call_id"],
                    "purpose": c["purpose"],
                    "prompt_tokens": c["tokens"][0],
                    "completion_tokens": c["tokens"][1],
                    **(
                        {"cached_tokens": c["cached_tokens"], "cache_usage_source": "upstream"}
                        if c["cached_tokens"] is not None
                        else {}
                    ),
                }
                for c in self._calls.values()
            ]
