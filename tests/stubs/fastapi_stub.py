"""Records route registrations so register() can be exercised without FastAPI installed (NOT a FastAPI test)."""
import sys
import types


class HTTPException(Exception):
    def __init__(self, status_code, detail=None):
        self.status_code, self.detail = status_code, detail


class _Resp:
    def __init__(self, content=None, status_code=200, media_type=None, headers=None):
        self.content, self.status_code, self.media_type, self.headers = content, status_code, media_type, headers or {}


def Header(default=None):
    return default


class FastAPI:
    def __init__(self):
        self.routes = {}

    def _reg(self, method):
        def deco(path, **kw):
            def wrap(fn):
                self.routes[(method, path)] = fn
                return fn
            return wrap
        return deco

    def __getattr__(self, name):
        if name in ("get", "post"):
            return self._reg(name.upper())
        raise AttributeError(name)


def install():
    fa = types.ModuleType("fastapi")
    fa.FastAPI, fa.Header, fa.HTTPException, fa.Request = FastAPI, Header, HTTPException, object
    rs = types.ModuleType("fastapi.responses")
    rs.HTMLResponse = rs.JSONResponse = rs.PlainTextResponse = rs.Response = _Resp
    sys.modules["fastapi"], sys.modules["fastapi.responses"] = fa, rs
    return fa
