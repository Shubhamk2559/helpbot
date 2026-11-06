"""Minimal in-memory stand-in for motor, covering ONLY the operators the ad engine uses.
It is NOT MongoDB: it proves our logic, not MongoDB's atomicity guarantees (see README 'Testing')."""
import copy

from ads.store import DuplicateKeyError

_MISSING = object()


def _get(doc, path):
    cur = doc
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return _MISSING
        cur = cur[p]
    return cur


def _match_val(v, cond):
    if isinstance(cond, dict) and cond and all(k.startswith("$") for k in cond):
        for op, arg in cond.items():
            if op == "$exists":
                if (v is not _MISSING) != bool(arg):
                    return False
            elif op == "$in":
                if v is _MISSING or v not in arg:
                    return False
            elif op == "$ne":
                if v is not _MISSING and v == arg:
                    return False
            else:
                if v is _MISSING or v is None:
                    return False
                if op == "$lt" and not v < arg: return False
                if op == "$lte" and not v <= arg: return False
                if op == "$gt" and not v > arg: return False
                if op == "$gte" and not v >= arg: return False
        return True
    if cond is None:
        return v is _MISSING or v is None
    return v is not _MISSING and v == cond


def match(doc, flt):
    for k, cond in flt.items():
        if k == "$or":
            if not any(match(doc, sub) for sub in cond):
                return False
        elif k == "$and":
            if not all(match(doc, sub) for sub in cond):
                return False
        elif not _match_val(_get(doc, k), cond):
            return False
    return True


def _set(doc, path, val):
    parts = path.split(".")
    for p in parts[:-1]:
        doc = doc.setdefault(p, {})
    doc[parts[-1]] = val


class Cursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, key, direction=1):
        if isinstance(key, list):
            key, direction = key[0]
        self.docs.sort(key=lambda d: (d.get(key) is None, d.get(key)), reverse=direction < 0)
        return self

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    async def to_list(self, length=None):
        return [copy.deepcopy(d) for d in self.docs[:length]]


class Collection:
    def __init__(self):
        self.docs = {}
        self._n = 0

    async def create_index(self, *a, **k):
        return "idx"

    def _all(self):
        return list(self.docs.values())

    async def insert_one(self, doc):
        doc = copy.deepcopy(doc)
        if "_id" not in doc:
            self._n += 1
            doc["_id"] = f"auto{self._n}"
        if doc["_id"] in self.docs:
            raise DuplicateKeyError("dup")
        self.docs[doc["_id"]] = doc

    async def find_one(self, flt=None, projection=None):
        for d in self._all():
            if match(d, flt or {}):
                return copy.deepcopy(d)
        return None

    def find(self, flt=None, projection=None):
        return Cursor([d for d in self._all() if match(d, flt or {})])

    def _apply(self, doc, update, inserting=False):
        for k, v in update.get("$set", {}).items():
            _set(doc, k, copy.deepcopy(v))
        for k, v in update.get("$inc", {}).items():
            cur = _get(doc, k)
            _set(doc, k, (0 if cur is _MISSING else cur) + v)
        if inserting:
            for k, v in update.get("$setOnInsert", {}).items():
                _set(doc, k, copy.deepcopy(v))

    async def update_one(self, flt, update, upsert=False):
        for d in self._all():
            if match(d, flt):
                self._apply(d, update)
                return
        if upsert:
            doc = {k: v for k, v in flt.items() if not k.startswith("$") and not isinstance(v, dict)}
            self._apply(doc, update, inserting=True)
            self._n += 1
            doc.setdefault("_id", f"auto{self._n}")
            self.docs[doc["_id"]] = doc

    async def find_one_and_update(self, flt, update, sort=None, return_document="before", upsert=False):
        cands = [d for d in self._all() if match(d, flt)]
        if sort:
            k, dirn = sort[0]
            cands.sort(key=lambda d: d.get(k), reverse=dirn < 0)
        if not cands:
            return None
        d = cands[0]
        before = copy.deepcopy(d)
        self._apply(d, update)
        return copy.deepcopy(d) if return_document == "after" else before


class FakeDB:
    def __init__(self):
        self.cols = {}

    def __getitem__(self, name):
        return self.cols.setdefault(name, Collection())

    def __call__(self, name):
        return self[name]
