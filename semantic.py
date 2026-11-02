"""Optional local embedding search. Keyword recall stays available without it."""
from __future__ import annotations
from array import array
import hashlib
import math

_encoders = {}


def model_name(reg):
    """The registry opts in with "semantic_model"; no key keeps recall keyword-only."""
    name = reg.data.get('semantic_model')
    return name if isinstance(name, str) and name else None


def encoder(name):
    if name not in _encoders:
        from fastembed import TextEmbedding  # Optional dependency; ImportError means keyword-only.
        _encoders[name] = TextEmbedding(model_name=name)
    return _encoders[name]


def pack(vector):
    values = [float(x) for x in vector]
    norm = math.sqrt(sum(x * x for x in values)) or 1.0
    return array('f', (x / norm for x in values)).tobytes()


def unpack(blob):
    values = array('f')
    values.frombytes(blob)
    return values


def text_hash(name, text):
    return hashlib.sha256((name + '\0' + text).encode('utf-8')).hexdigest()


def sync_vectors(db, name):
    """Caller owns the transaction. Embed only cards whose text or model changed."""
    db.execute('CREATE TABLE IF NOT EXISTS vectors (rowid INTEGER PRIMARY KEY, text_hash TEXT, vec BLOB)')
    cards = dict(db.execute('SELECT rowid, body FROM cards'))
    known = dict(db.execute('SELECT rowid, text_hash FROM vectors'))
    stale = [r for r in known if r not in cards]
    db.executemany('DELETE FROM vectors WHERE rowid=?', [(r,) for r in stale])
    missing = [(r, text) for r, text in cards.items() if known.get(r) != text_hash(name, text)]
    if missing:
        vectors = encoder(name).embed([text for _, text in missing])
        db.executemany('INSERT OR REPLACE INTO vectors VALUES(?,?,?)',
                       [(r, text_hash(name, text), pack(v)) for (r, text), v in zip(missing, vectors)])
    return {'model': name, 'embedded': len(missing), 'removed': len(stale)}


def search(db, name, query, pid, limit=40):
    """Rank in-scope cards by cosine similarity. Vectors are stored normalized."""
    q = unpack(pack(next(iter(encoder(name).query_embed(query)))))
    rows = db.execute("SELECT c.rowid, v.vec, v.text_hash, c.body FROM cards c JOIN vectors v ON v.rowid=c.rowid "
                      "WHERE c.project=? OR c.shared='1'", (pid,)).fetchall()
    scored = [(sum(a * b for a, b in zip(q, unpack(vec))), r) for r, vec, h, body in rows if h == text_hash(name, body)]
    scored.sort(reverse=True)
    return [r for _, r in scored[:limit]]
