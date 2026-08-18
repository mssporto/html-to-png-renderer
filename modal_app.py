"""
HTML rendering service.

Renders an HTML string to a PNG image or PDF using headless Chromium.

The input HTML is untrusted, so:
  - JavaScript execution is disabled in the render context.
  - Every resource request (images, stylesheets, fonts) is checked against
    a scheme allowlist and a private/loopback/link-local IP blocklist
    before being allowed through, so this service can't be used as an
    open SSRF proxy into the container's network (e.g. cloud metadata
    endpoints).
  - `/render_html_to_png` requires a Bearer token (see the `render-auth-token`
    Modal Secret).
"""

import ipaddress
import socket
import time
from typing import Literal
from urllib.parse import urlparse

import modal
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

app = modal.App("html-to-png-renderer")

# Playwright needs its Chromium download baked into the image.
# This image step adds ~30s to the first build, then cached forever.
image = (
    modal.Image.debian_slim()
    .pip_install("fastapi", "playwright", "pydantic")
    .run_commands("playwright install chromium")
    .run_commands("playwright install-deps chromium")
)

auth_scheme = HTTPBearer()

MAX_HTML_CHARS = 500_000  # generous for a single carousel slide
MIN_DIMENSION = 100
MAX_DIMENSION = 2160
ALLOWED_SCHEMES = {"http", "https", "data", "blob"}


class RenderRequest(BaseModel):
    html: str = Field(..., min_length=1, max_length=MAX_HTML_CHARS)
    width: int = Field(default=1080, ge=MIN_DIMENSION, le=MAX_DIMENSION)
    height: int = Field(default=1350, ge=MIN_DIMENSION, le=MAX_DIMENSION)
    wait_for_network: bool = True
    format: Literal["png", "pdf"] = "png"


def _is_blocked_host(hostname: str | None) -> bool:
    """True if hostname resolves to a private/loopback/link-local/reserved address."""
    if not hostname:
        return True
    try:
        addrs = {info[4][0] for info in socket.getaddrinfo(hostname, None)}
    except socket.gaierror:
        return False  # unresolvable host; Chromium's own request will just fail
    return any(
        ipaddress.ip_address(addr).is_private
        or ipaddress.ip_address(addr).is_loopback
        or ipaddress.ip_address(addr).is_link_local
        or ipaddress.ip_address(addr).is_reserved
        or ipaddress.ip_address(addr).is_multicast
        for addr in addrs
    )


def _make_route_handler(cache_bust: str):
    def handle_route(route):
        url = route.request.url
        parsed = urlparse(url)

        if parsed.scheme not in ALLOWED_SCHEMES:
            route.abort()
            return

        if parsed.scheme in ("http", "https") and _is_blocked_host(parsed.hostname):
            route.abort()
            return

        # Belt-and-braces: append a timestamp query string to every jsDelivr
        # request so Chromium cannot serve a cached version of the brand CSS.
        if "cdn.jsdelivr.net" in url:
            sep = "&" if "?" in url else "?"
            route.continue_(url=f"{url}{sep}_cb={cache_bust}")
        else:
            route.continue_()

    return handle_route


@app.function(
    image=image,
    timeout=120,        # 2 min ceiling; renders take ~3s typically
    memory=1024,         # 1GB; Chromium needs headroom
    # No min_containers, so this scales to zero between calls (billed only
    # while running). scaledown_window is Modal's own default (60s) rather
    # than a longer custom value: still lets back-to-back slides in one
    # carousel reuse a warm container, but doesn't linger idle-and-billed
    # once a workflow run is done.
    scaledown_window=60,
    secrets=[modal.Secret.from_name("render-auth-token")],
)
@modal.fastapi_endpoint(method="POST")
def render_html_to_png(
    item: RenderRequest,
    token: HTTPAuthorizationCredentials = Depends(auth_scheme),
):
    """
    Render an HTML string to PNG or PDF.

    Requires `Authorization: Bearer <token>` matching the `render-auth-token`
    Modal Secret's `AUTH_TOKEN` value.

    Request body:
      {
        "html": "<html>...</html>",   # required
        "width": 1080,                # optional, default 1080 (100-2160)
        "height": 1350,               # optional, default 1350 (100-2160)
        "wait_for_network": true,     # optional, default true
        "format": "png"               # optional, "png" (default) or "pdf"
      }

    Returns:
      {
        "file_base64": "<base64 file bytes>",
        "content_type": "image/png" or "application/pdf",
        "width": 1080,
        "height": 1350
      }
    """
    import base64
    import os
    from playwright.sync_api import sync_playwright

    if token.credentials != os.environ["AUTH_TOKEN"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    with sync_playwright() as p:
        # Modal containers run as root, and Chromium refuses to launch as
        # root without --no-sandbox. We accept that trade-off here because
        # Modal already runs each container inside the gVisor sandboxed
        # runtime, which is the isolation boundary doing the real work;
        # disabling JavaScript (below) and the network guard above remove
        # the practical exploit paths (SSRF, script-driven exfiltration)
        # that Chromium's own sandbox would otherwise be the last line of
        # defense against.
        browser = p.chromium.launch(
            args=["--no-sandbox", "--disable-cache", "--disk-cache-size=0"]
        )
        context = browser.new_context(
            viewport={"width": item.width, "height": item.height},
            device_scale_factor=2,   # retina-quality output
            java_script_enabled=False,
        )
        page = context.new_page()

        cache_bust = str(int(time.time()))
        page.route("**/*", _make_route_handler(cache_bust))

        wait_until = "networkidle" if item.wait_for_network else "load"
        page.set_content(item.html, wait_until=wait_until, timeout=30000)

        if item.format == "pdf":
            # Page size in px matches the requested canvas dimensions, so a
            # PDF request behaves like the PNG one: one canvas-sized page.
            file_bytes = page.pdf(
                width=f"{item.width}px",
                height=f"{item.height}px",
                print_background=True,
            )
            content_type = "application/pdf"
        else:
            # Screenshot the .fn-canvas element directly so the output
            # matches the canvas dimensions exactly. Falls back to a
            # viewport clip so non-canvas HTML still works.
            canvas_el = page.query_selector(".fn-canvas")
            if canvas_el:
                file_bytes = canvas_el.screenshot(type="png")
            else:
                file_bytes = page.screenshot(
                    type="png",
                    clip={"x": 0, "y": 0, "width": item.width, "height": item.height},
                )
            content_type = "image/png"

        browser.close()

    return {
        "file_base64": base64.b64encode(file_bytes).decode("utf-8"),
        "content_type": content_type,
        "width": item.width,
        "height": item.height,
    }


# Quick health check endpoint, so you can verify the deploy without rendering.
# Left unauthenticated: it returns no sensitive information and is useful
# for uptime monitoring.
@app.function(image=image, scaledown_window=60)
@modal.fastapi_endpoint(method="GET")
def health():
    return {"status": "ok", "service": "html-to-png-renderer"}
