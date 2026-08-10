"""Lexical retrieval over the SOP: send only the relevant sections per reply.

Why lexical and not embeddings
------------------------------
The corpus is 17 short sections (~7k chars). BM25 plus hand-written aliases
covers that comfortably, with no new dependencies, no cold start, and scores
that can be read straight out of the logs when tuning. Every retrieval
decision is logged so the real miss rate can be measured before paying for
embeddings.

Safety properties
-----------------
- Sections 6 and 8 are always co-retrieved: the SOP's ATURAN KHUSUS rules
  disambiguate between them *by number*, so splitting them breaks the rule.
- Below MIN_SCORE we fall back to the full SOP. Retrieval can therefore only
  save tokens, never turn a previously-correct answer into a wrong one.
"""

import math
import re

# BM25
K1 = 1.5
B = 0.75

TOP_K = 3
# Calibrated on real mitra messages from bot.log: the weakest true match
# scored 4.3, the strongest off-topic message 2.4. 3.5 sits in that gap.
MIN_SCORE = 3.5            # top section below this -> full-SOP fallback
# A section also has to clear a floor relative to the winner, so a strong
# match doesn't drag two irrelevant sections along with it.
SECTION_MIN_ABS = 2.5
SECTION_REL = 0.3
ALIAS_PHRASE_BOOST = 4.0   # multi-word alias hit
ALIAS_WORD_BOOST = 2.5     # single-word alias hit

CORRECTION_TOP_K = 2
CORRECTION_MIN_SCORE = 1.0

# Politeness, pronouns and greetings carry no topic signal. Greetings matter:
# section 1 literally contains "Pagi/Siang/Malam", so leaving them in would
# pull section 1 for every message that opens with a greeting.
STOPWORDS = {
    "mba", "mas", "kak", "ya", "yg", "yang", "di", "ke", "dari", "untuk", "utk",
    "dengan", "dan", "atau", "saya", "aku", "kita", "kami", "anda", "nya",
    "mohon", "tolong", "apakah", "apa", "kok", "nih", "sih", "dong", "deh",
    "gimana", "bagaimana", "itu", "ini", "adalah", "jika", "kalau", "klo",
    "bisa", "dapat", "akan", "juga", "saja", "aja", "buat", "pada", "oleh",
    "dalam", "tersebut", "silakan", "terima", "kasih", "makasih", "thanks",
    "halo", "hai", "pagi", "siang", "sore", "malam", "permisi", "maaf",
    "test", "coba", "oke", "ok", "iya", "tidak", "gak", "ga", "engga",
}

# Hand-written triggers. Multi-word entries are matched as substrings on the
# normalised text; single words as tokens. These carry most of the precision.
ALIASES = {
    1: ["konfirmasi", "job masuk", "keberangkatan", "berangkat", "sudah jalan",
        "tiba lokasi", "klik tiba", "mulai layanan"],
    2: ["pembayaran client", "client bayar", "client belum bayar",
        "belum bayar", "reminder pembayaran", "ingatkan client",
        "konfirmasi pembayaran", "client transfer"],
    3: ["tagihan", "transfer", "rekening", "bca", "nominal", "bukti transfer",
        "potong tabungan", "bayar tagihan"],
    4: ["detail layanan", "massage", "cream", "scrub", "paket layanan",
        "body head"],
    5: ["izin off", "ajukan off", "jam standby", "libur", "offkan"],
    6: ["aplikasi error", "tidak bisa buka", "ga bisa buka", "gabisa buka",
        "tidak bisa klik", "refresh", "restart", "uninstall", "force close",
        "error", "aplikasi", "lemot", "hang", "bug", "blank"],
    7: ["belum ada job", "gaada job", "ga ada job", "gada job", "tidak ada job",
        "sepi", "nunggu job", "orderan", "job terus"],
    8: ["sudah tiba", "belum ketemu", "belum mulai", "15 menit", "nunggu lama",
        "chat client", "sudah di lokasi", "sudah sampai", "belum dibalas"],
    9: ["buatkan booking", "bikin booking", "buat booking", "client baru",
        "client lama", "buatin booking"],
    10: ["alamat", "patokan", "nyasar", "cari alamat", "susah cari", "maps"],
    11: ["feedback", "masukan", "saran", "evaluasi"],
    12: ["beli produk", "pembelian produk", "ambil produk", "toko mitra",
         "produk", "kantor"],
    13: ["ubah jam", "ganti jam", "reschedule", "tambah layanan",
         "perubahan booking", "perubahan jam"],
    14: ["tabungan", "saldo", "cairkan", "pencairan", "tarik tabungan",
         "finance"],
    15: ["cek sk", "jadwal sk"],
    16: ["dibatalkan", "batal", "cancel", "pengganti"],
    17: ["kode booking", "kode bookingan"],
}

# Sections whose meaning depends on being seen together.
BINDINGS = {6: (8,), 8: (6,)}


def _tokens(text: str) -> list[str]:
    return [
        t for t in re.findall(r"[a-z0-9]+", text.lower())
        if t not in STOPWORDS and len(t) > 1
    ]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower())


def parse_sop(system_prompt: str) -> tuple[str, dict[int, str], str]:
    """Split the prompt into (intro, {number: section_text}, footer).

    Uses the existing `---` fences, so the SOP text stays the single source of
    truth in llm.py and never has to be duplicated here.
    """
    parts = re.split(r"\n-{3,}\n", system_prompt)
    if len(parts) < 3:
        return system_prompt, {}, ""
    intro, body, footer = parts[0], parts[1], "\n---\n".join(parts[2:])

    sections: dict[int, str] = {}
    for chunk in re.split(r"\n(?=\d+\.\s)", body.strip()):
        chunk = chunk.strip()
        m = re.match(r"(\d+)\.\s", chunk)
        if m:
            sections[int(m.group(1))] = chunk
    return intro.strip(), sections, footer.strip()


class SopIndex:
    """BM25 index over the SOP sections."""

    def __init__(self, sections: dict[int, str]):
        self.sections = sections
        self.docs = {n: _tokens(t) for n, t in sections.items()}
        self.lengths = {n: len(d) for n, d in self.docs.items()}
        self.avg_len = (sum(self.lengths.values()) / len(self.lengths)) if self.lengths else 0.0
        n_docs = len(self.docs) or 1
        df: dict[str, int] = {}
        for toks in self.docs.values():
            for t in set(toks):
                df[t] = df.get(t, 0) + 1
        self.idf = {
            t: math.log(1 + (n_docs - c + 0.5) / (c + 0.5)) for t, c in df.items()
        }

    def score(self, query: str) -> dict[int, float]:
        q_tokens = _tokens(query)
        q_set = set(q_tokens)
        scores: dict[int, float] = {}
        for num, toks in self.docs.items():
            if not toks:
                continue
            tf: dict[str, int] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            s = 0.0
            for qt in q_tokens:
                if qt not in tf:
                    continue
                idf = self.idf.get(qt, 0.0)
                f = tf[qt]
                denom = f + K1 * (1 - B + B * self.lengths[num] / (self.avg_len or 1))
                s += idf * (f * (K1 + 1)) / (denom or 1)
            for alias in ALIASES.get(num, []):
                # Match alias tokens rather than the raw substring: mitra insert
                # filler words the SOP doesn't have. "client belum bayar" has to
                # match "client nya belum bayar nih kak", which substring
                # matching missed -- that gap sent §2 questions to §10 in prod.
                a_tokens = _tokens(alias)
                if not a_tokens:
                    continue
                if len(a_tokens) > 1:
                    if all(t in q_set for t in a_tokens):
                        s += ALIAS_PHRASE_BOOST
                elif a_tokens[0] in q_set:
                    s += ALIAS_WORD_BOOST
            scores[num] = s
        return scores

    def select(self, query: str, top_k: int = TOP_K) -> tuple[list[int], dict[int, float], bool]:
        """Return (section numbers, scores, confident)."""
        scores = self.score(query)
        if not scores:
            return [], {}, False
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        top = ranked[0][1]
        if top < MIN_SCORE:
            return [], scores, False
        floor = max(SECTION_MIN_ABS, top * SECTION_REL)
        chosen = [n for n, s in ranked[:top_k] if s >= floor]
        if not chosen:
            chosen = [ranked[0][0]]
        for n in list(chosen):
            for bound in BINDINGS.get(n, ()):
                if bound not in chosen and bound in self.sections:
                    chosen.append(bound)
        return sorted(chosen), scores, True


def select_corrections(query: str, corrections: list, top_k: int = CORRECTION_TOP_K) -> list:
    """Pick the corrections that actually relate to this message.

    Replaces the previous `corrections[-5:]`, which injected whatever was most
    recent regardless of topic -- wasting tokens and crowding out relevant ones.
    """
    if not corrections:
        return []
    q_tokens = set(_tokens(query))
    if not q_tokens:
        return []
    scored = []
    for c in corrections:
        cq = set(_tokens(c.get("user_question", "")))
        if not cq:
            continue
        overlap = q_tokens & cq
        if not overlap:
            continue
        # Jaccard-ish: reward overlap, penalise very broad stored questions.
        score = len(overlap) / math.sqrt(len(cq)) * 2.0
        if score >= CORRECTION_MIN_SCORE:
            scored.append((score, c))
    scored.sort(key=lambda kv: kv[0], reverse=True)
    return [c for _, c in scored[:top_k]]


def build_sop(system_prompt: str, query: str) -> tuple[str, dict]:
    """Return (sop_text, meta). Falls back to the full prompt when unsure."""
    intro, sections, footer = parse_sop(system_prompt)
    if not sections:
        return system_prompt, {"mode": "full", "reason": "parse_failed", "sections": []}

    index = _get_index(system_prompt, sections)
    chosen, scores, confident = index.select(query or "")
    if not confident or not chosen:
        top = max(scores.values()) if scores else 0.0
        return system_prompt, {
            "mode": "full",
            "reason": "low_confidence",
            "sections": [],
            "top_score": round(top, 2),
        }

    body = "\n\n".join(sections[n] for n in chosen)
    text = f"{intro}\n\n---\n{body}\n---\n{footer}"
    return text, {
        "mode": "rag",
        "reason": "match",
        "sections": chosen,
        "top_score": round(max(scores[n] for n in chosen), 2),
        "scores": {n: round(scores[n], 2) for n in chosen},
    }


_INDEX_CACHE: dict[int, SopIndex] = {}


def _get_index(system_prompt: str, sections: dict[int, str]) -> SopIndex:
    key = hash(system_prompt)
    if key not in _INDEX_CACHE:
        _INDEX_CACHE[key] = SopIndex(sections)
    return _INDEX_CACHE[key]
