"""Engine-owned transcription language choices and detection declarations."""

from frisket.contracts.actions.schemas._engines import (
    EngineDeclaration,
    TRANSCRIBE_ENGINE_TABLE,
    transcription_engine_capabilities,
)
from frisket.contracts.actions.schemas._language import (
    LanguageDeclaration,
    choices_from_codes,
)


WHISPER_LANGUAGES: dict[str, str] = {
    "af": "Afrikaans",
    "am": "Amharic",
    "ar": "Arabic",
    "as": "Assamese",
    "az": "Azerbaijani",
    "ba": "Bashkir",
    "be": "Belarusian",
    "bg": "Bulgarian",
    "bn": "Bengali",
    "bo": "Tibetan",
    "br": "Breton",
    "bs": "Bosnian",
    "ca": "Catalan",
    "cs": "Czech",
    "cy": "Welsh",
    "da": "Danish",
    "de": "German",
    "el": "Greek",
    "en": "English",
    "es": "Spanish",
    "et": "Estonian",
    "eu": "Basque",
    "fa": "Persian",
    "fi": "Finnish",
    "fo": "Faroese",
    "fr": "French",
    "gl": "Galician",
    "gu": "Gujarati",
    "ha": "Hausa",
    "haw": "Hawaiian",
    "he": "Hebrew",
    "hi": "Hindi",
    "hr": "Croatian",
    "ht": "Haitian Creole",
    "hu": "Hungarian",
    "hy": "Armenian",
    "id": "Indonesian",
    "is": "Icelandic",
    "it": "Italian",
    "ja": "Japanese",
    "jw": "Javanese",
    "ka": "Georgian",
    "kk": "Kazakh",
    "km": "Khmer",
    "kn": "Kannada",
    "ko": "Korean",
    "la": "Latin",
    "lb": "Luxembourgish",
    "ln": "Lingala",
    "lo": "Lao",
    "lt": "Lithuanian",
    "lv": "Latvian",
    "mg": "Malagasy",
    "mi": "Maori",
    "mk": "Macedonian",
    "ml": "Malayalam",
    "mn": "Mongolian",
    "mr": "Marathi",
    "ms": "Malay",
    "mt": "Maltese",
    "my": "Myanmar",
    "ne": "Nepali",
    "nl": "Dutch",
    "nn": "Nynorsk",
    "no": "Norwegian",
    "oc": "Occitan",
    "pa": "Punjabi",
    "pl": "Polish",
    "ps": "Pashto",
    "pt": "Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "sa": "Sanskrit",
    "sd": "Sindhi",
    "si": "Sinhala",
    "sk": "Slovak",
    "sl": "Slovenian",
    "sn": "Shona",
    "so": "Somali",
    "sq": "Albanian",
    "sr": "Serbian",
    "su": "Sundanese",
    "sv": "Swedish",
    "sw": "Swahili",
    "ta": "Tamil",
    "te": "Telugu",
    "tg": "Tajik",
    "th": "Thai",
    "tk": "Turkmen",
    "tl": "Tagalog",
    "tr": "Turkish",
    "tt": "Tatar",
    "uk": "Ukrainian",
    "ur": "Urdu",
    "uz": "Uzbek",
    "vi": "Vietnamese",
    "yi": "Yiddish",
    "yo": "Yoruba",
    "yue": "Cantonese",
    "zh": "Chinese",
}

_WHISPER_SINGLE = LanguageDeclaration(
    mode="single",
    default="auto",
    choices=choices_from_codes(WHISPER_LANGUAGES),
    detects=True,
)
# Only Whisper-class remote models report detected language.
_REMOTE_NONWHISPER_SINGLE = LanguageDeclaration(
    mode="single",
    default="auto",
    choices=choices_from_codes(WHISPER_LANGUAGES),
    detects=False,
)


def transcribe_language_declaration(
    engine: str, *, table: tuple[EngineDeclaration, ...] = TRANSCRIBE_ENGINE_TABLE
) -> LanguageDeclaration:
    """The language capability for a transcribe engine id (symbolic, alias, or
    ``<provider>/<model>``). A ``<provider>/<model>`` remote id detects a
    language ONLY when the model is whisper-class (mirrors the remote adapter's
    verbose_json branch); other remote models accept a hint but report no
    detected language."""
    capabilities = transcription_engine_capabilities(table, engine)
    if capabilities.language_choices is not None:
        return LanguageDeclaration(
            mode="single",
            default="auto",
            choices=choices_from_codes(capabilities.language_choices),
            detects=capabilities.detects_language,
        )
    if capabilities.language_mode == "fixed":
        return LanguageDeclaration(
            mode="fixed",
            default=capabilities.fixed_language,
            fixed_language=capabilities.fixed_language,
            choices=None,
            detects=False,
        )
    if capabilities.language_mode == "auto_only":
        return LanguageDeclaration(
            mode="auto_only",
            default="auto",
            choices=None,
            detects=capabilities.detects_language,
        )
    if capabilities.language_mode == "multi":
        return LanguageDeclaration(
            mode="multi",
            default="auto",
            choices=choices_from_codes(WHISPER_LANGUAGES),
            detects=capabilities.detects_language,
        )
    if capabilities.detects_language:
        return _WHISPER_SINGLE
    return _REMOTE_NONWHISPER_SINGLE
