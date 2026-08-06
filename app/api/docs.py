from fastapi import Depends, FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute


API_VERSION = "2.0.0"
PUBLIC_ROUTE_PATHS = frozenset({
    "/api/model-readiness",
    "/api/stats",
    "/api/status",
})


def _api_routes(app: FastAPI):
    for route in app.routes:
        if isinstance(route, APIRoute):
            yield route
        else:
            yield from getattr(getattr(route, "original_router", None), "routes", ())


def _schema(app: FastAPI, paths: frozenset[str], title: str):
    routes = [
        route
        for route in _api_routes(app)
        if isinstance(route, APIRoute) and route.path in paths
    ]
    return get_openapi(title=title, version=API_VERSION, routes=routes)


def public_schema(app: FastAPI):
    return _schema(app, PUBLIC_ROUTE_PATHS, "Tunisia Outage Tracker Public API")


def internal_schema(app: FastAPI):
    paths = frozenset(
        route.path
        for route in _api_routes(app)
        if isinstance(route, APIRoute) and route.path.startswith("/api/internal/")
    )
    return _schema(app, paths, "Tunisia Outage Tracker Internal API")


def install(app: FastAPI, verify_ops_secret):
    @app.get("/openapi.json", include_in_schema=False)
    def public_openapi():
        return JSONResponse(public_schema(app))

    @app.get("/docs", include_in_schema=False)
    def public_docs():
        return get_swagger_ui_html(
            openapi_url="/openapi.json",
            title="Tunisia Outage Tracker Public API",
        )

    @app.get(
        "/api/internal/openapi.json",
        dependencies=[Depends(verify_ops_secret)],
        include_in_schema=False,
    )
    def protected_internal_openapi():
        return JSONResponse(internal_schema(app))
