"""
Mock LLM client — deterministic, free, fast.

Used when:
  - config/llm.toml provider="mock"
  - API key absent (factory falls back to mock)
  - dev / CI / smoke tests
"""

from __future__ import annotations

import asyncio
import random
import time

from .client_base import ChatMessage, ChatResult, LLMClient


_CALM_TEMPLATES = [
    "状态很稳定,继续保持就好",
    "做得不错,跟着节奏来",
    "动作很标准,放轻松",
    "保持这个状态,慢慢来",
]
_FRUSTRATION_TEMPLATES = [
    "慢一些没关系,深呼吸",
    "不舒服就告诉我,可以休息一下",
    "放松一点,跟着节奏就好",
    "你做得很好,休息片刻再继续",
]
_PLEASURE_TEMPLATES = [
    "状态非常好,再坚持一下就完成了",
    "保持这个节奏,做得很棒",
    "今天的表现很出色",
    "继续保持,你很棒",
]


_ASSESSMENT_TEMPLATE = """## 训练概况

本次康复训练完成度 {completion}%。患者在 {duration} 分钟的训练中,情绪表现稳定。

## 情绪表现分析

情绪占比:平静 {calm}% / 愉悦 {pleasure}% / 沮丧 {frustration}%。
{emotion_analysis}

## 计划调整解读

{adjustments}

## 给医生的建议

根据本次表现,患者整体状态良好。建议下次训练保持当前参数。可考虑在保持阶段微调,
以加强股四头肌等长收缩效果。

以上建议仅供参考,具体方案请医师据临床情况判断。

## 给患者的鼓励

今天的训练表现稳定,情绪管理也做得不错。康复之路需要耐心,你做得很好,继续保持。
"""


class MockClient(LLMClient):
    """Returns plausible-looking responses based on prompt content."""

    async def chat(self, messages, *, model=None, temperature=None,
                   max_tokens=None, timeout_s=None) -> ChatResult:
        await asyncio.sleep(0.05)   # tiny simulated latency

        # Concatenate so we can pattern-match content
        all_text = "\n".join(m.content for m in messages)

        if "评估" in all_text or "康复医师助理" in all_text or "训练概况" in all_text:
            content = self._mock_assessment(all_text)
        elif "鼓励" in all_text or "陪伴" in all_text or "TTS" in all_text:
            content = self._mock_encourage(all_text)
        else:
            content = "(mock response — set provider=deepseek in config/llm.toml for real LLM)"

        prompt_chars = sum(len(m.content) for m in messages)
        return ChatResult(
            content=content,
            model="mock-v1",
            prompt_tokens=prompt_chars // 4,
            completion_tokens=len(content) // 4,
            latency_ms=50.0,
            cache_hit=False,
        )

    def _mock_assessment(self, prompt_text: str) -> str:
        # Try to extract real-ish numbers from the prompt to make output feel grounded
        def find_num(needle, default):
            import re
            m = re.search(rf"{needle}[:\s]*([\d.]+)", prompt_text)
            if m:
                try:
                    v = float(m.group(1))
                    return f"{v:.0f}" if v.is_integer() else f"{v}"
                except ValueError:
                    pass
            return str(default)

        calm = int(find_num("calm", 60))
        plea = int(find_num("pleasure", 20))
        frus = max(0, 100 - calm - plea)
        completion = find_num("completion_pct", 100)
        duration = find_num("duration_min", 10)

        if frus >= 30:
            analysis = "训练中出现一定比例的沮丧情绪,可能与动作难度或疲劳有关。"
        elif plea >= 25:
            analysis = "训练中愉悦情绪占比较高,患者投入度良好。"
        else:
            analysis = "整体情绪稳定,符合保守康复阶段预期。"

        return _ASSESSMENT_TEMPLATE.format(
            completion=completion, duration=duration,
            calm=calm, pleasure=plea, frustration=frus,
            emotion_analysis=analysis,
            adjustments="本次训练系统未触发明显的计划调整事件。",
        )

    def _mock_encourage(self, prompt_text: str) -> str:
        if "frustration" in prompt_text.lower() or "沮丧" in prompt_text:
            pool = _FRUSTRATION_TEMPLATES
        elif "pleasure" in prompt_text.lower() or "愉悦" in prompt_text:
            pool = _PLEASURE_TEMPLATES
        else:
            pool = _CALM_TEMPLATES
        return random.choice(pool)
