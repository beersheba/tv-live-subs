"""Prompts shared by the LLM translators (local Gemma, Gemini)."""
from __future__ import annotations

LANG_NAMES = {
    "he": "Hebrew", "ru": "Russian", "en": "English", "ar": "Arabic", "uk": "Ukrainian", "fr": "French",
    "de": "German", "es": "Spanish", "it": "Italian", "pt": "Portuguese", "tr": "Turkish", "pl": "Polish",
    "fa": "Persian", "am": "Amharic",
}


def lang_name(code: str) -> str:
    return LANG_NAMES.get(code, code)


def system_prompt(src: str, dst: str, glossary: dict[str, str]) -> str:
    """Static per (src, dst): stays byte-identical between requests so its KV cache can be reused."""
    s, d = lang_name(src), lang_name(dst)
    terms = "\n".join(f"- {k} = {v}" for k, v in glossary.items())
    return (
        f"You translate live TV subtitles from {s} to {d}.\n"
        "The input is raw automatic speech recognition of live, informal speech: it can contain "
        "recognition errors, filler words, false starts, repetitions and cut-off fragments.\n"
        "Rules:\n"
        f"- Translate ONLY the last line, into natural, concise spoken {d}, as a subtitle "
        "(short; about 12 words at most when possible).\n"
        "- Use the previous lines as context to fix obvious recognition errors and to resolve what is meant.\n"
        "- Drop fillers, stutters and repeated words. Render slang, abbreviations and idioms by their "
        "meaning, not word for word.\n"
        "- If the line is a cut-off fragment, translate it as a fragment and keep a trailing '...'.\n"
        "- Keep names and terms consistent with the glossary and the known names given with the line.\n"
        "- Output only the translation: no quotes, no notes.\n"
        + (f"Glossary (always use these renderings):\n{terms}" if terms else "")
    )


def user_prompt(text: str, context: tuple[str, ...], terms: dict[str, str]) -> str:
    parts = []
    if terms:
        parts.append("Known names and terms (use these renderings):\n" + "\n".join(f"- {k} = {v}" for k, v in terms.items()))
    if context:
        parts.append("Previous lines (context only):\n" + "\n".join(context))
    parts.append(f"Last line to translate:\n{text}")
    return "\n\n".join(parts)


def extract_system(src: str, dst: str) -> str:
    return (f"You maintain a glossary for a live TV subtitle translator ({lang_name(src)} to {lang_name(dst)}). "
            "Be strict: only list items you are sure about.")


def extract_user(sources: list[str], translations: list[str], src: str, dst: str) -> str:
    rows = "\n".join(f"{a} | {b}" for a, b in zip(sources, translations))
    return (f"Recent subtitle lines (source | current translation):\n{rows}\n\n"
            f"List proper names (people, parties, places, organizations) and any slang, abbreviations or "
            f"special terms that appear in the SOURCE lines, each with its correct {lang_name(dst)} rendering, "
            "one per line as `source = target`. Copy the source form exactly as written above. "
            "At most 8 lines, no explanations. If there is nothing worth listing, output NONE.")
