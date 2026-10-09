"""CLI: list the Gemini models reachable with the configured key.  ``make models``"""

from __future__ import annotations

import asyncio

from haulmind.config import get_settings
from haulmind.llm.gemini import GeminiClient


async def _main() -> int:
    s = get_settings()
    if not s.gemini_api_key:
        print("GEMINI_API_KEY is not set; running on the offline stub provider.")
        print(f"Configured defaults: triage={s.llm_model_triage} planner={s.llm_model_planner}")
        return 0
    client = GeminiClient(s)
    names = await client.list_models()
    flash = [
        n for n in names if "flash" in n and "image" not in n and "tts" not in n and "live" not in n
    ]
    print("Flash-class text models available to this key:")
    for n in flash:
        marker = ""
        if n == s.llm_model_triage:
            marker = "   <- LLM_MODEL_TRIAGE"
        elif n == s.llm_model_planner:
            marker = "   <- LLM_MODEL_PLANNER"
        print(f"  {n}{marker}")
    missing = [m for m in (s.llm_model_triage, s.llm_model_planner) if m not in names]
    if missing:
        print(f"\nWARNING: configured model(s) not listed for this key: {', '.join(missing)}")
        print("Set LLM_MODEL_TRIAGE / LLM_MODEL_PLANNER in .env to one of the names above.")
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
