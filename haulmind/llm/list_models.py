"""CLI: list the models reachable with the configured key.  ``make models``

Works for Gemini (``GEMINI_API_KEY``) and for any OpenAI-compatible provider
(``LLM_API_KEY`` + ``LLM_API_BASE``), and warns when the configured role models are missing.
"""

from __future__ import annotations

import asyncio

from haulmind.config import Settings, get_settings


def _print_names(s: Settings, names: list[str]) -> None:
    for n in names:
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


async def _main() -> int:
    s = get_settings()
    provider = s.effective_llm_provider
    if provider == "stub":
        print("No API key set (GEMINI_API_KEY or LLM_API_KEY); running on the offline stub.")
        print(f"Configured defaults: triage={s.llm_model_triage} planner={s.llm_model_planner}")
        return 0
    if provider == "openai":
        from haulmind.llm.openai_compat import OpenAICompatibleClient

        compat = OpenAICompatibleClient(s)
        try:
            names = await compat.list_models()
        finally:
            await compat.aclose()
        print(f"Models available at {s.llm_api_base} ({compat.provider}):")
        _print_names(s, names)
        return 0
    from haulmind.llm.gemini import GeminiClient

    client = GeminiClient(s)
    names = await client.list_models()
    flash = [
        n for n in names if "flash" in n and "image" not in n and "tts" not in n and "live" not in n
    ]
    print("Flash-class text models available to this key:")
    _print_names(s, flash)
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
