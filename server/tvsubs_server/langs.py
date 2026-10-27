"""ISO 639-1 -> NLLB-200 language codes (subset; extend as needed)."""

NLLB = {
    "he": "heb_Hebn", "ru": "rus_Cyrl", "en": "eng_Latn", "ar": "arb_Arab",
    "uk": "ukr_Cyrl", "fr": "fra_Latn", "de": "deu_Latn", "es": "spa_Latn",
    "it": "ita_Latn", "pt": "por_Latn", "pl": "pol_Latn", "tr": "tur_Latn",
    "fa": "pes_Arab", "hi": "hin_Deva", "zh": "zho_Hans", "ja": "jpn_Jpan",
    "ko": "kor_Hang", "nl": "nld_Latn", "sv": "swe_Latn", "cs": "ces_Latn",
    "ro": "ron_Latn", "hu": "hun_Latn", "el": "ell_Grek", "bg": "bul_Cyrl",
    "sr": "srp_Cyrl", "hr": "hrv_Latn", "ka": "kat_Geor", "hy": "hye_Armn",
    "az": "azj_Latn", "kk": "kaz_Cyrl", "uz": "uzn_Latn",
}


def nllb(code: str) -> str:
    try:
        return NLLB[code.lower().split("-")[0]]
    except KeyError:
        raise ValueError(f"unsupported language: {code!r}") from None
