"""Settings loaded from config/app.toml."""
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "app.toml"

# Wildcard defaults (the [wildcards] section of app.toml documents and can override every one of them).
# A wildcard folder's role says what kind of ingredient its lists are. "Slot" roles may be offered whenever a job leaves
# that aspect open; every other role is "match": offered only when the request matches the list by keyword.
WILDCARD_ROLES = {"background": "setting", "camera": "shot", "color": "palette", "clothing": "outfit", "pose": "pose",
                  "style": "style", "artist-group": "artist", "material": "material", "concept": "concept", "state": "state",
                  "meta": "meta", "random": "random", "body-parts": "body", "nsfw": "nsfw", "gaze": "gaze"}
WILDCARD_SLOT_ROLES = ["setting", "shot", "palette", "outfit", "pose", "gaze"]
# Words that say a slot is already decided in the brief ("in a city" fixes the setting), so no ideas are offered for it.
WILDCARD_GIVEN_WORDS = {
    "setting": ["background", "setting", "scene", "location", "indoors", "outdoors", "street", "city", "town", "village", "forest", "beach",
                "mountain", "room", "kitchen", "bedroom", "cafe", "office", "park", "garden", "desert", "castle", "ruins", "library",
                "temple", "rooftop", "lake", "river", "ocean", "sea", "field", "meadow", "space", "cabin", "school", "classroom"],
    "shot": ["close-up", "closeup", "close up", "portrait", "wide shot", "full body", "cowboy shot", "upper body", "from above", "from below",
             "from behind", "dutch angle", "low angle", "high angle", "bird's eye", "aerial", "macro", "selfie", "side view", "front view",
             "back view", "pov", "camera", "angle", "shot", "framing", "from the side", "from the front", "from the back", "bird's-eye", "worm's-eye",
             "overhead", "top-down", "over the shoulder"],
    "gaze": ["gaze", "gazing", "stare", "staring", "glance", "glancing", "eye contact", "looking at", "looking back", "looking away", "looking up",
             "looking down", "looking over", "looking aside", "looking sideways", "looks at", "facing the viewer", "facing viewer", "facing away"],
    "palette": ["color", "colour", "colors", "colours", "palette", "monochrome", "black and white", "sepia", "pastel", "neon", "vibrant",
                "muted", "red", "orange", "yellow", "green", "blue", "purple", "pink", "cyan", "teal", "white", "black", "gold", "golden",
                "silver", "grey", "gray", "brown"],
    "outfit": ["wearing", "wears", "dress", "outfit", "clothes", "clothing", "suit", "uniform", "armor", "armour", "raincoat", "coat",
               "jacket", "shirt", "skirt", "kimono", "hoodie", "costume", "gown", "hat", "sweater", "jeans"],
    "pose": ["pose", "posing", "standing", "sitting", "sits", "lying", "kneeling", "running", "jumping", "walking", "crouching", "leaning",
             "dancing", "holding", "looking at", "reaching", "waving"]}
# Which scene slot a list's role fills when the LLM did not say, and what each slot is about (the meaning search ranks a
# list's entries against this text, so an entry that is not a viewing angle or framing sinks in the camera slot).
WILDCARD_ROLE_SLOTS = {"shot": "camera", "gaze": "gaze", "pose": "pose", "outfit": "outfit", "setting": "setting", "palette": "lighting", "state": "mood",
                       "body": "body", "nsfw": "pose"}
# The slots the scene plan wants filled for every picture (unless the request already decides them): where the camera is, and where the person looks.
# Entries for them come from whichever lists hold fitting ones, in any folder (planning/serving.py); the LLM chooses among the entries offered, and when
# no list covers a slot it writes that slot itself.
WILDCARD_ALWAYS_SLOTS = ["camera", "gaze"]
WILDCARD_SLOT_ABOUT = {
    "camera": "from which side or height the scene is seen, and how close or wide the framing is, such as from above, from behind or a close-up",
    "gaze": "where the person looks, such as at the viewer, away, back over the shoulder, up or down",
    "pose": "the body position or pose of a person",
    "outfit": "clothes or an outfit that a person wears",
    "setting": "the place or background where a picture is set",
    "lighting": "the lighting and colour scheme of a picture",
    "expression": "the facial expression of a person",
    "body": "the build, hair, eyes or other physical features of a person",
    "mood": "the mood or atmosphere of a picture"}
# What each scene slot looks like as entries of a wildcard list: serving.py scores every entry against these. Concrete examples are what let the
# embeddings model tell slots apart; the plain sentences above could not separate camera from pose.
WILDCARD_SLOT_EXAMPLES = {
    "camera": "camera angle, view and framing: wide shot, close-up, portrait framing, point of view, from above, from below, from behind, from the side, dutch angle",
    "gaze": "where the person looks: at the viewer, looking away, looking back over the shoulder, looking up, looking down, looking to the side",
    "pose": "body posture and pose: standing, sitting, kneeling, lying down, walking, dancing, arms crossed, squatting",
    "outfit": "clothing and outfits: dress, suit, kimono, jacket, skirt, uniform, costume, shirt, coat",
    "setting": "the place and background: city street, forest, beach, room interior, garden, castle, outdoors, indoors",
    "lighting": "light and colour scheme: soft lighting, neon light, warm sunlight, shadows, vibrant colours, muted tones",
    "expression": "facial expression: smile, frown, angry stare, surprised face, neutral expression, grin",
    "body": "body and build: tall, muscular, slim build, hair, eyes",
    "mood": "mood and atmosphere: calm, dark, cheerful, mysterious, romantic, epic atmosphere"}
# An instruction to Tweak containing one of these asks for something new, so wildcard ideas are offered with it.
WILDCARD_TWEAK_WORDS = ["different", "random", "surprise", "another", "something else", "something new", "vary", "variety", "switch",
                        "swap", "mix it up", "anything", "new background", "new setting", "new pose", "new outfit"]


@dataclass
class Config:
    comfy_url: str = "http://127.0.0.1:8188"
    comfy_install_dir: str = ""
    comfy_cli: str = "comfy"
    host: str = "127.0.0.1"
    port: int = 8765
    data_dir: Path = ROOT / "data"
    max_images_per_job: int = 16
    max_images_per_request: int = 200              # most images one planned request may add up to
    hires_default: bool = True
    max_loras: int = 3
    safety_negative: list[str] = field(default_factory=list)
    strip_from_default_negative: list[str] = field(default_factory=list)
    # llama.cpp server (see [llm] in app.toml)
    llm_binary: Path = ROOT / "tools" / "llama.cpp" / "b11960" / "llama-server.exe"
    llm_model: str = "gemma"                       # key of llm_models
    llm_models: dict[str, Path] = field(default_factory=dict)
    llm_host: str = "127.0.0.1"
    llm_port: int = 8090
    llm_ctx_size: int = 8192
    llm_gpu_layers: int = 99
    llm_enable_thinking: bool = False
    llm_temperature: float = 0.2
    llm_max_tokens: int = 3000
    llm_start_timeout: float = 180.0
    llm_stop_timeout: float = 15.0
    llm_extra_args: list[str] = field(default_factory=list)
    allow_draft_cards: bool = False                # the planner may use cards you haven't approved
    # search by meaning (see [embeddings] in app.toml). A small model on the CPU, never on the GPU.
    embeddings_enabled: bool = True                # off: keyword search (BM25) only, exactly as before
    embeddings_model: Path = ROOT / "models" / "embeddings" / "embeddinggemma-300m" / "embeddinggemma-300M-Q8_0.gguf"
    embeddings_port: int = 8099
    embeddings_query_prefix: str = "task: search result | query: "      # the model's own prefixes (see its model card)
    embeddings_doc_prefix: str = "title: none | text: "
    embeddings_idle_stop: float = 600.0            # seconds without use before the embedding server is stopped (it restarts when needed)
    embeddings_floor: float = 0.25                 # least meaning score for a wildcard list to be offered
    embeddings_margin: float = 0.10                # ... and at most this far below the best list's score
    embeddings_lora_floor: float = 0.35            # least meaning score for a LoRA card to count as pointed at by the brief
    embeddings_threads: int = 0                    # CPU threads of the embedding server (0 = llama.cpp decides)
    embeddings_start_timeout: float = 60.0
    # reranking (see [reranker] in app.toml): a small model on the CPU puts the best matches of the meaning search in better order.
    # The file is not shipped; with it missing, off, or failing its start-up check, the planner uses the meaning search alone.
    reranker_enabled: bool = False      # off by default: in the comparison it tied the search by meaning alone and cost about 2 s per search
    reranker_model: Path = ROOT / "models" / "reranker" / "Qwen3-Reranker-0.6B-Q4_K_M.gguf"
    reranker_port: int = 8101
    reranker_pool: int = 24                        # the best matches of the meaning search that are reranked (the rest keep their order after them)
    reranker_weight: float = 0.7                   # share of the rerank score in the final one (the rest is the meaning search's cosine), both scaled 0..1 per query
    reranker_timeout: float = 30.0                 # seconds one rerank request may take
    reranker_threads: int = 0                      # CPU threads of the reranker server (0 = llama.cpp decides)
    # wildcards (see [wildcards] in app.toml)
    wildcards_enabled: bool = True                 # offer wildcard ideas to the prompt writer
    wildcards_ideas_per_prompt: int = 4            # lists the ideas for one prompt come from
    wildcards_entries_per_list: int = 5            # entries drawn from each list, for each prompt
    wildcards_max_idea_entries: int = 120          # most idea entries in one prompt-writing call (it shrinks the draw to fit)
    wildcards_min_match_score: float = 1.0         # BM25 score a list needs to count as matching the brief
    wildcards_llm_picks_lists: bool = True         # the LLM chooses which lists to use (one short call per job); off: keywords only
    wildcards_catalog_chars: int = 9000            # size cap of the list catalog shown to the LLM (about 3 characters per token)
    wildcards_roles: dict[str, str] = field(default_factory=lambda: dict(WILDCARD_ROLES))
    wildcards_slot_roles: list[str] = field(default_factory=lambda: list(WILDCARD_SLOT_ROLES))
    wildcards_given_words: dict[str, list[str]] = field(default_factory=lambda: {k: list(v) for k, v in WILDCARD_GIVEN_WORDS.items()})
    wildcards_tweak_words: list[str] = field(default_factory=lambda: list(WILDCARD_TWEAK_WORDS))
    wildcards_role_slots: dict[str, str] = field(default_factory=lambda: dict(WILDCARD_ROLE_SLOTS))      # list role -> the scene slot it fills by default
    wildcards_slot_about: dict[str, str] = field(default_factory=lambda: dict(WILDCARD_SLOT_ABOUT))      # scene slot -> what it is about (for ranking entries)
    wildcards_always_slots: list[str] = field(default_factory=lambda: list(WILDCARD_ALWAYS_SLOTS))      # scene slots offered for every picture the request leaves open
    wildcards_offer_size: int = 5                  # entries offered to the scene plan for each slot it may choose from
    wildcards_fit_floor: float = 0.40              # least closeness in meaning for an entry to count as fitting a slot (planning/serving.py)
    wildcards_fit_lead: float = 0.02               # ... and how far it must be ahead of the next slot, so an entry between two slots feeds neither

    @property
    def llm_url(self) -> str:
        return f"http://{self.llm_host}:{self.llm_port}"

    @property
    def llm_model_path(self) -> Path:
        if self.llm_model not in self.llm_models:
            raise KeyError(f"No LLM model called '{self.llm_model}' in [llm.models] ({', '.join(self.llm_models) or 'none'})")
        return self.llm_models[self.llm_model]

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "comfy-planner.db"

    @property
    def workflows_dir(self) -> Path:
        return self.data_dir / "workflows"

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def wildcards_dir(self) -> Path:
        return self.data_dir / "wildcards"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"



def slot_entry_texts(cfg) -> list[str]:
    """The text each entry is scored against, one per slot in `wildcards_slot_about` order. A slot description the user changed is used as it stands."""
    return [WILDCARD_SLOT_EXAMPLES.get(slot, text) if text == WILDCARD_SLOT_ABOUT.get(slot) else text for slot, text in cfg.wildcards_slot_about.items()]

def load_config(path: Path = CONFIG_PATH) -> Config:
    """Read the toml file; missing keys keep their defaults."""
    raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    comfy, app, jobs = raw.get("comfy", {}), raw.get("app", {}), raw.get("jobs", {})
    cfg = Config()
    cfg.comfy_url = comfy.get("url", cfg.comfy_url).rstrip("/")
    cfg.comfy_install_dir = comfy.get("install_dir", cfg.comfy_install_dir)
    cfg.comfy_cli = comfy.get("cli", cfg.comfy_cli)
    cfg.host = app.get("host", cfg.host)
    cfg.port = app.get("port", cfg.port)
    data = Path(app.get("data_dir", "data"))
    cfg.data_dir = data if data.is_absolute() else ROOT / data
    cfg.max_images_per_job = jobs.get("max_images_per_job", cfg.max_images_per_job)
    cfg.max_images_per_request = jobs.get("max_images_per_request", cfg.max_images_per_request)
    cfg.hires_default = jobs.get("hires_default", cfg.hires_default)
    cfg.max_loras = jobs.get("max_loras", cfg.max_loras)
    cfg.safety_negative = list(jobs.get("safety_negative", []))
    cfg.strip_from_default_negative = list(jobs.get("strip_from_default_negative", []))
    llm, cards = raw.get("llm", {}), raw.get("cards", {})
    cfg.llm_binary = _path(llm.get("binary"), cfg.llm_binary)
    cfg.llm_model = llm.get("model", cfg.llm_model)
    cfg.llm_models = {name: _path(p, Path(p)) for name, p in llm.get("models", {}).items()}
    cfg.llm_host = llm.get("host", cfg.llm_host)
    cfg.llm_port = llm.get("port", cfg.llm_port)
    cfg.llm_ctx_size = llm.get("ctx_size", cfg.llm_ctx_size)
    cfg.llm_gpu_layers = llm.get("gpu_layers", cfg.llm_gpu_layers)
    cfg.llm_enable_thinking = llm.get("enable_thinking", cfg.llm_enable_thinking)
    cfg.llm_temperature = llm.get("temperature", cfg.llm_temperature)
    cfg.llm_max_tokens = llm.get("max_tokens", cfg.llm_max_tokens)
    cfg.llm_start_timeout = llm.get("start_timeout", cfg.llm_start_timeout)
    cfg.llm_stop_timeout = llm.get("stop_timeout", cfg.llm_stop_timeout)
    cfg.llm_extra_args = list(llm.get("extra_args", []))
    cfg.allow_draft_cards = cards.get("allow_draft_cards", cfg.allow_draft_cards)
    emb = raw.get("embeddings", {})
    cfg.embeddings_enabled = emb.get("enabled", cfg.embeddings_enabled)
    cfg.embeddings_model = _path(emb.get("model"), cfg.embeddings_model)
    cfg.embeddings_port = emb.get("port", cfg.embeddings_port)
    cfg.embeddings_query_prefix = emb.get("query_prefix", cfg.embeddings_query_prefix)
    cfg.embeddings_doc_prefix = emb.get("doc_prefix", cfg.embeddings_doc_prefix)
    cfg.embeddings_threads = emb.get("threads", cfg.embeddings_threads)
    cfg.embeddings_start_timeout = emb.get("start_timeout", cfg.embeddings_start_timeout)
    cfg.embeddings_idle_stop = emb.get("idle_stop", cfg.embeddings_idle_stop)
    cfg.embeddings_floor = emb.get("floor", cfg.embeddings_floor)
    cfg.embeddings_margin = emb.get("margin", cfg.embeddings_margin)
    cfg.embeddings_lora_floor = emb.get("lora_floor", cfg.embeddings_lora_floor)
    rr = raw.get("reranker", {})
    cfg.reranker_enabled = rr.get("enabled", cfg.reranker_enabled)
    cfg.reranker_model = _path(rr.get("model"), cfg.reranker_model)
    cfg.reranker_port = rr.get("port", cfg.reranker_port)
    cfg.reranker_pool = int(rr.get("pool", cfg.reranker_pool))
    cfg.reranker_weight = min(1.0, max(0.0, float(rr.get("weight", cfg.reranker_weight))))
    cfg.reranker_timeout = rr.get("timeout", cfg.reranker_timeout)
    cfg.reranker_threads = rr.get("threads", cfg.reranker_threads)
    wc = raw.get("wildcards", {})
    cfg.wildcards_enabled = wc.get("enabled", cfg.wildcards_enabled)
    cfg.wildcards_ideas_per_prompt = wc.get("ideas_per_prompt", cfg.wildcards_ideas_per_prompt)
    cfg.wildcards_entries_per_list = wc.get("entries_per_list", cfg.wildcards_entries_per_list)
    cfg.wildcards_max_idea_entries = wc.get("max_idea_entries", cfg.wildcards_max_idea_entries)
    cfg.wildcards_min_match_score = wc.get("min_match_score", cfg.wildcards_min_match_score)
    cfg.wildcards_llm_picks_lists = wc.get("llm_picks_lists", cfg.wildcards_llm_picks_lists)
    cfg.wildcards_catalog_chars = wc.get("catalog_chars", cfg.wildcards_catalog_chars)
    cfg.wildcards_roles = {k.lower(): str(v) for k, v in wc.get("roles", cfg.wildcards_roles).items()}
    cfg.wildcards_slot_roles = list(wc.get("slot_roles", cfg.wildcards_slot_roles))
    cfg.wildcards_given_words = {k: list(v) for k, v in wc.get("given_words", cfg.wildcards_given_words).items()}
    cfg.wildcards_tweak_words = list(wc.get("tweak_words", cfg.wildcards_tweak_words))
    cfg.wildcards_role_slots = {k.lower(): str(v) for k, v in wc.get("role_slots", cfg.wildcards_role_slots).items()}
    cfg.wildcards_slot_about = {k: str(v) for k, v in wc.get("slot_about", cfg.wildcards_slot_about).items()}
    cfg.wildcards_always_slots = [str(x) for x in wc.get("always_slots", cfg.wildcards_always_slots)]
    cfg.wildcards_offer_size = int(wc.get("offer_size", cfg.wildcards_offer_size))
    cfg.wildcards_fit_floor = float(wc.get("fit_floor", cfg.wildcards_fit_floor))
    cfg.wildcards_fit_lead = float(wc.get("fit_lead", cfg.wildcards_fit_lead))
    return cfg


def _path(value: str | None, default: Path) -> Path:
    """A path from the toml file; relative ones are relative to the project folder."""
    if not value:
        return default
    p = Path(value)
    return p if p.is_absolute() else ROOT / p
