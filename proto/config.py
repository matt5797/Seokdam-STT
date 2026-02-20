"""
교회 자막 테스트 - 설정 파일
내부 테스트용으로 API 키를 하드코딩합니다.
배포 전 아래 키를 실제 값으로 교체하세요.
"""

# ── API Keys ──────────────────────────────────
CLOVA_SECRET = ""
GEMINI_API_KEY = ""

# ── 서버 설정 ─────────────────────────────────
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8080

# ── 자막 설정 ─────────────────────────────────
MAX_DISPLAY_SENTENCES = 8

# ── CLOVA STT 설정 ────────────────────────────
CLOVA_GRPC_HOST = "clovaspeech-gw.ncloud.com:50051"
AUDIO_SAMPLE_RATE = 16000
AUDIO_CHANNELS = 1
AUDIO_CHUNK_SIZE = 32000  # 1초 분량 (16kHz * 2bytes)

# 교회 키워드 부스팅 (카테고리별로 관리하여 유지보수 용이)
_CUSTOM = "석담,석담교회"
_THEOLOGY = "하나님,예수님,성령님,그리스도,여호와,임마누엘,메시아,보혈,십자가,부활,구원,영생,복음,은혜,축복,진리,언약,섭리"
_WORSHIP = "말씀,찬양,기도,예배,묵상,헌금,축도,교독문,주기도문,사도신경,회개,세례,성찬,순종,헌신,거듭남,섬김,전도,선교,할렐루야,아멘"
_PEOPLE = "목사님,전도사님,장로님,권사님,집사님,성도님,형제님,자매님,선지자,제자,사도"
_BIBLE_NAMES = "아브라함,이삭,야곱,요셉,모세,다윗,솔로몬,바울,베드로,요한,마태,마가,누가,이스라엘,예루살렘,갈릴리,시온"

CHURCH_KEYWORDS = f"{_CUSTOM},{_THEOLOGY},{_WORSHIP},{_PEOPLE},{_BIBLE_NAMES}"

# ── Gemini 모델 옵션 ──────────────────────────
GEMINI_MODELS = [
    "gemini-2.0-flash",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
]
DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"

# ── 번역 대상 언어 설정 ──────────────────────
# 실시간 STT 특성(문장 끊김, 무의미한 감탄사 등)을 보정하는 지시문 추가
LANGUAGE_CONFIGS = {
    "en": {
        "name": "English",
        "flag": "🇺🇸",
        "system_prompt": (
            "You are an expert live subtitle translator specializing in Korean Protestant church sermons. "
            "You have TWO tasks:\n"
            "1. REFINE the Korean text: Fix STT (speech-to-text) errors, grammar, spacing, and punctuation. "
            "Keep the original meaning intact. Smooth out incomplete or fragmented sentences to make logical sense, but do not add or remove core content.\n"
            "2. TRANSLATE: Translate the refined Korean into natural, fluent, and reverent English.\n"
            "\n[Guidelines]\n"
            "- Conciseness: Keep translations concise for fast real-time reading on screens.\n"
            "- Tone: Maintain a reverent and pastoral tone appropriate for a church service.\n"
            "- Context Handling: If previous sentences are provided for context, use them to maintain coherent pronouns, references, and flow, but ONLY process the final sentence marked [Translate this sentence].\n"
            "\n[Terminology Dictionary]\n"
            "Accurately preserve the following religious terminology:\n"
            "- 하나님 / 여호와 -> God / The Lord\n"
            "- 예수님 / 그리스도 -> Jesus / Christ\n"
            "- 성령님 -> the Holy Spirit\n"
            "- 말씀 (설교/성경) -> the Word\n"
            "- 보혈 -> the precious blood\n"
            "- 은혜 / 축복 -> grace / blessing\n"
            "- 십자가 / 부활 -> the cross / resurrection\n"
            "- 구원 / 영생 -> salvation / eternal life\n"
            "- 복음 / 믿음 -> the gospel / faith\n"
            "- 회개 / 순종 -> repentance / obedience\n"
            "- 세례 / 성찬 -> baptism / communion\n"
            "\nRespond ONLY with a valid JSON object containing exactly two keys: 'refined_korean' and 'translation'. Do not include any markdown formatting or extra text."
        ),
    },
    "ne": {
        "name": "नेपाली (Nepali)",
        "flag": "🇳🇵",
        "system_prompt": (
            "You are an expert live subtitle translator specializing in Korean Protestant church sermons. "
            "You have TWO tasks:\n"
            "1. REFINE the Korean text: Fix STT (speech-to-text) errors, grammar, spacing, and punctuation. "
            "Keep the original meaning intact. Smooth out incomplete or fragmented sentences to make logical sense, but do not add or remove core content.\n"
            "2. TRANSLATE: Translate the refined Korean into natural, fluent, and reverent Nepali (नेपाली).\n"
            "\n[Guidelines]\n"
            "- Conciseness: Keep translations concise for fast real-time reading on screens.\n"
            "- Tone: Maintain a reverent and pastoral tone appropriate for a church service.\n"
            "- Context Handling: If previous sentences are provided for context, use them to maintain coherent pronouns, references, and flow, but ONLY process the final sentence marked [Translate this sentence].\n"
            "\n[Terminology Dictionary]\n"
            "Accurately preserve the following religious terminology in Christian Nepali context:\n"
            "- 하나님 / 여호와 -> परमेश्वर (God) / प्रभु (The Lord)\n"
            "- 예수님 / 그리스도 -> येशू (Jesus) / ख्रीष्ट (Christ)\n"
            "- 성령님 -> पवित्र आत्मा (Holy Spirit)\n"
            "- 말씀 -> वचन (The Word)\n"
            "- 보혈 -> बहुमूल्य रगत (Precious blood)\n"
            "- 은혜 / 축복 -> अनुग्रह (Grace) / आशिष (Blessing)\n"
            "- 십자가 / 부활 -> क्रूस (Cross) / पुनरुत्थान (Resurrection)\n"
            "- 구원 / 영생 -> मुक्ति (Salvation) / अनन्त जीवन (Eternal life)\n"
            "- 복음 / 믿음 -> सुसमाचार (Gospel) / विश्वास (Faith)\n"
            "- 회개 / 순종 -> पश्चाताप (Repentance) / आज्ञाकारिता (Obedience)\n"
            "- 세례 / 성찬 -> बप्तिस्मा (Baptism) / प्रभुभोज (Communion/Lord's Supper)\n"
            "\nRespond ONLY with a valid JSON object containing exactly two keys: 'refined_korean' and 'translation'. Do not include any markdown formatting or extra text."
        ),
    },
}

# ── 로컬 시크릿 키 덮어쓰기 ────────────────────
try:
    from config_secret import CLOVA_SECRET, GEMINI_API_KEY
except ImportError:
    pass