from __future__ import annotations

import os
from functools import lru_cache

import outlines
from transformers import AutoModelForCausalLM, AutoTokenizer

DEFAULT_MODEL_NAME = os.getenv("OUTLINES_MODEL_NAME", "microsoft/Phi-3-mini-4k-instruct")


@lru_cache(maxsize=1)
def get_outlines_model(model_name: str | None = None):
    name = model_name or DEFAULT_MODEL_NAME
    hf_model = AutoModelForCausalLM.from_pretrained(name, device_map="auto")
    tokenizer = AutoTokenizer.from_pretrained(name)
    return outlines.from_transformers(hf_model, tokenizer)
