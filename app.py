"""
Converts a whole Node.js/Express *project* into a working Python/Flask *project*
using a local Qwen3 model running via MLX — with a self-correction loop.

Unlike the original single-file version, this walks an Express project tree
(routers, middleware, support modules, EJS views, static assets) and emits a
multi-file Flask package:

    outputs/<project>_flask/
        app/__init__.py     # application factory (generated deterministically)
        app/<router>.py     # one Flask blueprint per Express router
        app/middleware.py    # global logger + auth decorator
        app/<module>.py     # support modules (e.g. the in-memory store)
        app/templates/...    # EJS views converted to Jinja2
        app/static/...       # CSS/JS/images copied verbatim
        wsgi.py              # entry point: create_app() + dev server
        requirements.txt

Because a local 8B model with a 4096-token budget cannot convert a whole
project in one shot, files are converted one at a time in dependency order:
support modules and middleware first, then routers (which receive the already
converted modules as context so imports line up), then templates. Static assets
are copied, never sent to the model. The application factory is generated
deterministically from the parsed router mounts, so blueprint wiring is always
consistent.

After assembly the whole app is booted in a subprocess, every GET route in the
url_map is hit, and any traceback is mapped back to the specific generated file
and fed to the model for a targeted repair. Repeats until it works or attempts
run out.

Usage:
    1. Set INPUT_DIR and OUTPUT_DIR below.
    2. python app.py
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Hardcoded config
# ---------------------------------------------------------------------------
MODEL_PATH = "/Volumes/SAMSUNG T20/models/qwen3-8b"
INPUT_DIR = "node_js_files"
OUTPUT_DIR = "outputs"

MAX_TOKENS = 4096
MAX_FIX_ATTEMPTS = 4  # smoke test + up to this many repair rounds

# File roles produced by classify()
SERVER, ROUTE, MIDDLEWARE, MODULE, TEMPLATE, STATIC, SKIP = (
    "server", "route", "middleware", "module", "template", "static", "skip"
)


SYSTEM_PROMPT = """You are an expert software engineer specializing in migrating \
Node.js/Express backends to Python/Flask. You convert code precisely and \
completely, preserving all routes, middleware behavior, request/response \
handling, and business logic. You do not omit any endpoints.

Important Flask-specific rules you must follow:
- Express `app.use(middlewareFn)` calls that apply globally (not tied to a \
  specific route) must become a plain function registered with \
  `app.before_request` — never wrapped, called manually, or passed the \
  `request` object as an argument.
- Per-route middleware (e.g. auth checks applied to specific routes) should \
  become a decorator using `functools.wraps`, applied directly above the \
  affected view functions.
- `request` is a proxy object; never call it like a function.
- Express routers become Flask Blueprints. Route paths on a blueprint stay \
  relative (the URL prefix is applied when the blueprint is registered), and \
  Express `:param` segments become Flask `<param>` / `<int:param>` converters.
- Output ONLY the requested file's contents in a single fenced code block with \
  no prose before or after."""


# ---------------------------------------------------------------------------
# Model plumbing (kept from the original, mlx imported lazily so the rest of
# this module — assembly, smoke testing — is importable without the model).
# ---------------------------------------------------------------------------
def load_model():
    from mlx_lm import load

    print(f"Loading model from {MODEL_PATH} ...")
    return load(MODEL_PATH)


def extract_code_block(text: str, lang: str = "python") -> str:
    """Pull the first fenced code block out of a model response."""
    fence = f"```{lang}"
    if fence in text:
        text = text.split(fence, 1)[1]
        text = text.split("```", 1)[0]
    elif "```" in text:
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1]
            # a leading bare language token on the first line, e.g. ```html
            first_nl = text.find("\n")
            if first_nl != -1 and text[:first_nl].strip().isalpha():
                text = text[first_nl + 1:]
    return text.strip()


def run_model(model, tokenizer, prompt, lang: str = "python") -> str:
    from mlx_lm import stream_generate

    chunks = []
    for response in stream_generate(model, tokenizer, prompt=prompt, max_tokens=MAX_TOKENS):
        print(response.text, end="", flush=True)
        chunks.append(response.text)
    print()
    return extract_code_block("".join(chunks), lang=lang)


def _chat(tokenizer, user_content: str) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    return tokenizer.apply_chat_template(
        messages, add_generation_prompt=True, enable_thinking=False
    )


# ---------------------------------------------------------------------------
# Project discovery / classification
# ---------------------------------------------------------------------------
def classify(rel: Path) -> str:
    parts = rel.parts
    name = rel.name
    if "node_modules" in parts:
        return SKIP
    if name in ("package.json", "package-lock.json") or name.endswith(".md"):
        return SKIP
    if name == "server.js" and len(parts) == 1:
        return SERVER
    if parts[0] == "routes" and name.endswith(".js"):
        return ROUTE
    if parts[0] == "middleware" and name.endswith(".js"):
        return MIDDLEWARE
    if parts[0] in ("data", "models", "lib", "services") and name.endswith(".js"):
        return MODULE
    if parts[0] == "views" and name.endswith(".ejs"):
        return TEMPLATE
    if parts[0] == "public":
        return STATIC
    if name.endswith(".js"):
        return MODULE  # stray top-level helper
    return SKIP


def build_manifest(input_dir: Path):
    """Walk the project and tag every file with a role."""
    entries = []
    for path in sorted(input_dir.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(input_dir)
        role = classify(rel)
        entries.append({"abs": path, "rel": rel, "role": role, "stem": rel.stem})
    return entries


def parse_route_mounts(server_src: str, route_stems):
    """
    Map each router file stem -> its mount prefix by reading server.js, e.g.

        const apiRoutes = require('./routes/api');
        app.use('/api', apiRoutes);            ->  {'api': '/api'}
    """
    var_to_stem = {}
    require_re = re.compile(
        r"""(?:const|let|var)\s+(\w+)\s*=\s*require\(\s*['"][./]*routes/(\w+)['"]\s*\)"""
    )
    for m in require_re.finditer(server_src):
        var_to_stem[m.group(1)] = m.group(2)

    mounts = {}
    use_re = re.compile(r"""app\.use\(\s*(?:['"]([^'"]*)['"]\s*,\s*)?(\w+)\s*\)""")
    for m in use_re.finditer(server_src):
        prefix, var = m.group(1), m.group(2)
        if var in var_to_stem:
            mounts[var_to_stem[var]] = prefix or "/"

    for stem in route_stems:
        mounts.setdefault(stem, "/")
    return mounts


# ---------------------------------------------------------------------------
# Prompt builders (one per kind of file to convert)
# ---------------------------------------------------------------------------
def build_module_prompt(tokenizer, name: str, js: str) -> str:
    user = f"""Convert this Node.js support module into an equivalent Python module. \
Preserve every exported function and its behavior. Keep the public function names \
the same (snake_case is fine only if the original already uses it — otherwise keep \
the original names so other modules can import them). Module-level state (arrays, \
counters) should stay module-level.

Output ONLY the Python file in a single ```python code block.

Source file: {name}
```javascript
{js}
```"""
    return _chat(tokenizer, user)


def build_middleware_prompt(tokenizer, combined_js: str) -> str:
    user = f"""Convert these Express middleware functions into a single Python module \
named `middleware.py` for a Flask app.

It MUST expose exactly these names:
- `log_request()` — a no-argument function suitable for `app.before_request`. It \
  logs the current request's method and path (use the Flask `request` proxy and \
  the `logging` module). Do NOT take `req`/`res`/`next` arguments.
- `require_api_key(f)` — a decorator (using `functools.wraps`) that reproduces the \
  Express API-key check: read the `x-api-key` header and return \
  `jsonify({{'error': 'Unauthorized'}}), 401` when it does not match; otherwise call \
  through to the wrapped view.

Preserve the exact header name and expected key value from the source.

Output ONLY the Python file in a single ```python code block.

--- SOURCE MIDDLEWARE ---
```javascript
{combined_js}
```"""
    return _chat(tokenizer, user)


def build_project_context(generated, mounts, template_names) -> str:
    """Shared context injected into every router-conversion prompt."""
    store_mods = [
        rel for rel, info in generated.items()
        if info["role"] == MODULE
    ]
    blocks = []
    for rel in store_mods:
        module_import = rel[len("app/"):].replace("/", ".")[: -len(".py")]
        blocks.append(
            f"# already converted: {rel}  (import as `from app import {module_import}`)\n"
            f"```python\n{generated[rel]['content']}\n```"
        )
    if "app/middleware.py" in generated:
        blocks.append(
            "# already converted: app/middleware.py "
            "(import as `from app.middleware import require_api_key`)\n"
            f"```python\n{generated['app/middleware.py']['content']}\n```"
        )

    mount_lines = "\n".join(
        f"  - blueprint `{stem}` is registered with url_prefix '{prefix}'"
        for stem, prefix in mounts.items()
    )
    tmpl_lines = ", ".join(sorted(template_names)) or "(none)"

    return f"""PROJECT CONTEXT — the target is a Flask package laid out as:
  app/__init__.py   (application factory, already written — do NOT create `app`)
  app/<router>.py   (Flask blueprints, one per Express router)
  app/middleware.py
  app/<module>.py   (support modules)
  app/templates/    (Jinja2 templates)
  app/static/       (css/js copied as-is)

Blueprint mounts:
{mount_lines}

Available Jinja2 templates for render_template(...): {tmpl_lines}

Already-converted modules you must import and reuse (do NOT re-implement them):
{chr(10).join(blocks)}"""


def build_route_prompt(tokenizer, stem: str, prefix: str, js: str, context: str) -> str:
    user = f"""{context}

Now convert this Express router file into a Flask blueprint module `app/{stem}.py`.

Rules:
- Define the blueprint exactly as `bp = Blueprint('{stem}', __name__)` (no url_prefix \
  here — it is applied when the blueprint is registered).
- This router is mounted at '{prefix}', but KEEP the route paths exactly as written in \
  the source (relative to the router). Do not prepend '{prefix}'.
- Import support modules from the app package (e.g. `from app import store`) and use \
  their functions; import `require_api_key` from `app.middleware` where the source \
  applies auth middleware to a route.
- For server-rendered routes use `render_template('<name>.html', ...)` with the \
  templates listed above. For JSON routes use `jsonify(...)` and preserve status codes.
- Convert `:param` to `<param>` / `<int:param>`; preserve query params, request body \
  handling (`request.json` / `request.form`), redirects, and validation.
- Do NOT create a Flask `app` object and do NOT call `app.run`.

Output ONLY `app/{stem}.py` in a single ```python code block.

Source file: routes/{stem}.js
```javascript
{js}
```"""
    return _chat(tokenizer, user)


def build_template_prompt(tokenizer, name: str, ejs: str) -> str:
    user = f"""Convert this EJS template into an equivalent Jinja2 (Flask) template. \
Keep the HTML identical; only translate the templating syntax:
- `<%= value %>`  ->  `{{{{ value }}}}`   (Jinja auto-escapes, like EJS `<%=`)
- `<%- include('partials/header') %>`  ->  `{{% include 'partials/header.html' %}}`
- `<% if (cond) {{ %> ... <% }} %>`  ->  `{{% if cond %}} ... {{% endif %}}`
- `<% arr.forEach(function (x) {{ %> ... <% }}); %>`  ->  `{{% for x in arr %}} ... {{% endfor %}}`
- `typeof foo !== 'undefined'` guards  ->  `foo is defined`
Convert JS expressions to Jinja equivalents (e.g. `note.pinned ? 'a' : 'b'` -> \
`'a' if note.pinned else 'b'`; `arr.length === 0` -> `arr|length == 0`).

Output ONLY the Jinja2 template in a single ```html code block.

Source template: {name}
```html
{ejs}
```"""
    return _chat(tokenizer, user)


def build_fix_prompt(tokenizer, rel_path: str, role: str, current: str, error_trace: str) -> str:
    lang = "html" if role == TEMPLATE else "python"
    user = f"""The generated Flask project fails when booted and exercised. The error \
points at the file `{rel_path}`. Fix THAT file.

Return the COMPLETE corrected contents of `{rel_path}` (not a diff, not a snippet) in \
a single ```{lang} code block, with no explanation before or after.

--- CURRENT `{rel_path}` ---
```{lang}
{current}
```

--- ERROR WHEN RUNNING THE APP ---
```
{error_trace}
```

Fix the root cause (a wrong import, a bad Jinja reference, a mis-converted route, \
etc.). Do not paper over it with a broad try/except."""
    return _chat(tokenizer, user)


# ---------------------------------------------------------------------------
# Deterministic scaffolding (factory / wsgi / requirements / smoke runner)
# ---------------------------------------------------------------------------
def generate_app_factory(mounts) -> str:
    imports = "\n".join(f"from app.{stem} import bp as {stem}_bp" for stem in mounts)
    regs = []
    for stem, prefix in mounts.items():
        if prefix and prefix != "/":
            regs.append(f"    app.register_blueprint({stem}_bp, url_prefix={prefix!r})")
        else:
            regs.append(f"    app.register_blueprint({stem}_bp)")
    reg_block = "\n".join(regs)

    return f'''"""Application factory (generated deterministically by the converter)."""
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

from app.middleware import log_request
{imports}


def create_app():
    # static_url_path="" serves app/static/* at the root (e.g. /css/style.css),
    # matching Express's `app.use(express.static('public'))`.
    app = Flask(__name__, static_url_path="")
    CORS(app)

    app.before_request(log_request)

{reg_block}

    @app.route("/health")
    def health():
        return jsonify({{"status": "ok"}})

    @app.errorhandler(404)
    def not_found(_e):
        if request.path.startswith("/api"):
            return jsonify({{"error": "Route not found"}}), 404
        return render_template("error.html", title="Not Found", message="Page not found"), 404

    @app.errorhandler(500)
    def server_error(_e):
        return jsonify({{"error": "Internal server error"}}), 500

    return app
'''


WSGI = '''"""Entry point: `python wsgi.py` runs the dev server."""
from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
'''

REQUIREMENTS = "flask>=3.0\nflask-cors>=4.0\n"

SMOKE_RUNNER = r'''import json, sys, traceback

result = {"ok": False, "phase": "boot", "url": None, "traceback": None}
try:
    from app import create_app
    app = create_app()
    app.testing = True
except Exception:
    tb = traceback.format_exc()
    result["traceback"] = tb
    if "ModuleNotFoundError" in tb and ("flask" in tb or "werkzeug" in tb):
        result["phase"] = "missing_deps"
    else:
        result["phase"] = "import"
    print(json.dumps(result)); sys.exit(1)

client = app.test_client()
adapter = app.url_map.bind("localhost")
seen = set()
for rule in app.url_map.iter_rules():
    if "GET" not in rule.methods or rule.endpoint == "static":
        continue
    values = {a: 1 for a in rule.arguments}
    try:
        url = adapter.build(rule.endpoint, values)
    except Exception:
        continue
    if url in seen:
        continue
    seen.add(url)
    result["url"] = url
    try:
        resp = client.get(url)
    except Exception:
        result["phase"] = "request"
        result["traceback"] = traceback.format_exc()
        print(json.dumps(result)); sys.exit(1)
    if resp.status_code >= 500:
        result["phase"] = "http500"
        result["traceback"] = "GET %s -> HTTP %d\n%s" % (
            url, resp.status_code, resp.get_data(as_text=True))
        print(json.dumps(result)); sys.exit(1)

result["ok"] = True
result["phase"] = "passed"
result["url"] = None
print(json.dumps(result)); sys.exit(0)
'''


# ---------------------------------------------------------------------------
# Assembly + smoke testing
# ---------------------------------------------------------------------------
def assemble_project(project_dir: Path, generated, static_copies, mounts):
    """Write the whole Flask project tree to disk."""
    if project_dir.exists():
        shutil.rmtree(project_dir)
    (project_dir / "app").mkdir(parents=True, exist_ok=True)

    # generated python/template files
    for rel, info in generated.items():
        dest = project_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(info["content"], encoding="utf-8")

    # copied static assets
    for src_abs, out_rel in static_copies:
        dest = project_dir / out_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src_abs, dest)

    # deterministic scaffolding
    (project_dir / "app" / "__init__.py").write_text(generate_app_factory(mounts), encoding="utf-8")
    (project_dir / "wsgi.py").write_text(WSGI, encoding="utf-8")
    (project_dir / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")


def smoke_test_project(project_dir: Path) -> dict:
    """Boot the assembled app in a subprocess and probe every GET route."""
    runner = project_dir / "_smoke.py"
    runner.write_text(SMOKE_RUNNER, encoding="utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, "_smoke.py"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        runner.unlink(missing_ok=True)

    # the runner prints a single JSON line as its last line of stdout
    for line in reversed(proc.stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                break
    return {
        "ok": False,
        "phase": "runner_error",
        "url": None,
        "traceback": (proc.stdout + "\n" + proc.stderr).strip(),
    }


def locate_failing_file(traceback_text: str, generated) -> str | None:
    """Map a traceback back to the generated file most likely at fault (deepest frame)."""
    match = None
    for m in re.finditer(r'File "([^"]+)"', traceback_text):
        path = m.group(1).replace("\\", "/")
        for rel in generated:
            if path.endswith("/" + rel) or path.endswith(rel):
                match = rel  # keep the last (deepest) match
    # Jinja errors reference the template path without a File "..." wrapper
    if match is None:
        for rel in generated:
            if generated[rel]["role"] == TEMPLATE and rel.split("app/templates/")[-1] in traceback_text:
                match = rel
    return match


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def template_out_rel(rel: Path) -> str:
    """views/index.ejs -> app/templates/index.html; views/partials/x.ejs -> app/templates/partials/x.html"""
    inside = Path(*rel.parts[1:])  # drop leading "views/"
    return f"app/templates/{inside.with_suffix('.html').as_posix()}"


def static_out_rel(rel: Path) -> str:
    """public/css/style.css -> app/static/css/style.css"""
    inside = Path(*rel.parts[1:])  # drop leading "public/"
    return f"app/static/{inside.as_posix()}"


def convert_project():
    input_dir = Path(INPUT_DIR)
    if not input_dir.exists():
        raise FileNotFoundError(f"Input project not found: {input_dir}")
    project_dir = Path(OUTPUT_DIR) / f"{input_dir.name}_flask"

    manifest = build_manifest(input_dir)
    server_entry = next((e for e in manifest if e["role"] == SERVER), None)
    server_src = server_entry["abs"].read_text(encoding="utf-8") if server_entry else ""
    route_stems = [e["stem"] for e in manifest if e["role"] == ROUTE]
    mounts = parse_route_mounts(server_src, route_stems)

    template_names = [
        template_out_rel(e["rel"])[len("app/templates/"):]
        for e in manifest if e["role"] == TEMPLATE
    ]

    model, tokenizer = load_model()

    generated = {}       # out-rel path -> {"content", "role"}
    static_copies = []   # (src_abs, out-rel)

    def convert(rel_out, role, prompt, lang="python"):
        print(f"\n>>> Converting -> {rel_out}\n")
        content = run_model(model, tokenizer, prompt, lang=lang)
        generated[rel_out] = {"content": content, "role": role}

    # 1. support modules first (routers depend on them)
    for e in [e for e in manifest if e["role"] == MODULE]:
        js = e["abs"].read_text(encoding="utf-8")
        convert(f"app/{e['stem']}.py", MODULE, build_module_prompt(tokenizer, e["rel"].as_posix(), js))

    # 2. middleware (combined into one module)
    mids = [e for e in manifest if e["role"] == MIDDLEWARE]
    if mids:
        combined = "\n\n".join(
            f"// --- {e['rel'].as_posix()} ---\n{e['abs'].read_text(encoding='utf-8')}"
            for e in mids
        )
        convert("app/middleware.py", MIDDLEWARE, build_middleware_prompt(tokenizer, combined))

    # 3. routers (with converted modules/middleware as shared context)
    context = build_project_context(generated, mounts, template_names)
    for e in [e for e in manifest if e["role"] == ROUTE]:
        js = e["abs"].read_text(encoding="utf-8")
        prefix = mounts.get(e["stem"], "/")
        convert(f"app/{e['stem']}.py", ROUTE,
                build_route_prompt(tokenizer, e["stem"], prefix, js, context))

    # 4. templates
    for e in [e for e in manifest if e["role"] == TEMPLATE]:
        ejs = e["abs"].read_text(encoding="utf-8")
        convert(template_out_rel(e["rel"]), TEMPLATE,
                build_template_prompt(tokenizer, e["rel"].as_posix(), ejs), lang="html")

    # 5. static assets — copied verbatim, never sent to the model
    for e in [e for e in manifest if e["role"] == STATIC]:
        static_copies.append((e["abs"], static_out_rel(e["rel"])))

    # 6. assemble + smoke-test/repair loop
    assemble_project(project_dir, generated, static_copies, mounts)

    ok = False
    for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
        print(f"\n--- Smoke-testing the assembled app (attempt {attempt}) ---")
        result = smoke_test_project(project_dir)

        if result["ok"]:
            ok = True
            print("Smoke test passed — every GET route booted and responded.")
            break

        if result["phase"] == "missing_deps":
            print("Smoke test could not run: Flask isn't installed in this interpreter.\n"
                  f"Install the project's deps and retest:\n"
                  f"    pip install -r {project_dir / 'requirements.txt'}")
            break

        print(f"Smoke test failed [{result['phase']}] on {result.get('url')}:\n"
              f"{result['traceback']}\n")

        if attempt == MAX_FIX_ATTEMPTS:
            print("Max fix attempts reached. Saving the last version for manual review.")
            break

        target = locate_failing_file(result["traceback"] or "", generated)
        if target is None:
            print("Could not pinpoint a generated file from the traceback; stopping.")
            break

        print(f"Asking the model to fix `{target}` (fix round {attempt}) ...\n")
        info = generated[target]
        fix_prompt = build_fix_prompt(tokenizer, target, info["role"], info["content"],
                                      result["traceback"])
        lang = "html" if info["role"] == TEMPLATE else "python"
        info["content"] = run_model(model, tokenizer, fix_prompt, lang=lang)
        assemble_project(project_dir, generated, static_copies, mounts)

    print(f"\nSaved converted Flask project to: {project_dir}")
    print("Run it with:")
    print(f"    cd {project_dir} && pip install -r requirements.txt && python wsgi.py")
    print("ok" if ok else "NOTE: this version still failed its smoke test — review manually.")


if __name__ == "__main__":
    convert_project()
