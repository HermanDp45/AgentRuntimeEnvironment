"""Профили, которые можно выбрать при создании чата."""

from typing import Literal

ModelProfile = Literal["deepseek", "qwen"]
MODEL_IDS = {
    "deepseek": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
    "qwen": "Qwen/Qwen3-235B-A22B-Instruct-2507",
}
