# html-to-png-renderer

Modal service that renders an HTML string to a PNG image or PDF using headless Chromium.

---

## Endpoint

```
POST /render_html_to_png
Authorization: Bearer <token>
```

The token must match the `AUTH_TOKEN` value in the `render-auth-token` Modal Secret for the environment you're calling.

**Request body:**

```json
{
  "html": "<div class=\"fn-canvas\">...</div>",
  "width": 1080,
  "height": 1080,
  "format": "png"
}
```

`format` is `"png"` (default) or `"pdf"`. `width`/`height` must be between 100 and 2160 (default 1080x1350) and set the PDF page size in pixels the same way they size the PNG canvas. `html` is capped at 500,000 characters. Requests outside these bounds get a `422`.

**Response:**

```json
{
  "file_base64": "<base64-encoded PNG or PDF>",
  "content_type": "image/png",
  "width": 1080,
  "height": 1080
}
```

For a PDF request, `content_type` is `"application/pdf"` instead.

```
GET /health
```

Unauthenticated status check, returns `{"status": "ok", "service": "html-to-png-renderer"}`.

---

## How it works

1. Playwright opens a headless Chromium browser with JavaScript execution disabled
2. Loads the HTML with a cache-busting query string (prevents stale asset issues)
3. Every resource request (images, stylesheets, fonts) is checked against a scheme allowlist and blocked if it targets a private/loopback/link-local/reserved IP, so untrusted input HTML can't be used to make the server reach internal network targets (SSRF)
4. For PNG: waits for the `.fn-canvas` element and takes a retina screenshot (`deviceScaleFactor: 2`) of that element only, falling back to a viewport clip if `.fn-canvas` isn't present
5. For PDF: renders the whole page to a single PDF page sized to `width`x`height` (in px), with backgrounds printed
6. Returns the file as a base64 string

---

## Security notes

- The render endpoint requires a Bearer token; `/health` does not.
- JavaScript is disabled in the render context — submitted HTML must be static markup/CSS only.
- Outbound resource requests are blocked for private/loopback/link-local/reserved IPs and non-http(s)/data/blob URL schemes.
- Chromium runs with `--no-sandbox` because Modal containers run as root and Chromium refuses to launch as root otherwise. The isolation boundary here is Modal's own gVisor-sandboxed container runtime, not Chromium's internal sandbox.
- `main` and `dev` environments each have their own `render-auth-token` secret; rotate with `modal secret create render-auth-token AUTH_TOKEN=<new-token> --env=<main|dev> --force`. **Rotating a secret does not restart already-warm containers** — run `modal app stop <app-id>` (or wait out `scaledown_window`) after rotating, or a running container can keep serving the old value.
- No `min_containers` is set on either function, so the app scales to zero between calls and isn't billed while idle. `scaledown_window` is left at Modal's default (60s) rather than a longer custom value, so a warm container is reused across slides within one carousel run but doesn't linger idle afterward.

---

## Design constraints

Inherited from the original brand constraints this service was built for; adjust as needed:

- No `border-radius`, no gradients, no `box-shadow`
- Target element must have the class `fn-canvas`
- Font: Archivo 800 (via Google Fonts or bundled)
- Palette: Charcoal `#1C1C1E` · Navy `#081B3E` · Snow `#F5F5F0` · Pink `#FF7D9B`

---

## Deploy

```bash
cd html-to-png-renderer
modal deploy --env=main modal_app.py   # or --env=dev
```

Requires a `render-auth-token` Modal Secret in the target environment (see Security notes). The service is otherwise stateless — no volumes or persistent storage.

---

## Environment

- Python 3.11, Debian Slim
- Playwright ≥ 1.44 + Chromium (installed in image)
- FastAPI ≥ 0.110
- Modal 1.5+
