"""EU-235: the /models cockpit pages — CRUD views over the model registry (EU-233) + the
secrets store (EU-234). EU-237 adds the form's Test-connection button, wired to POST
/models/test (``backends.test_backend_connection``) with an inline pass/fail result.

String-builder views in the house style (``html.escape`` + f-string fragments rendered through
``cockpit_views._wrap``): this repo has NO ``templates/`` directory and the cockpit deliberately
renders from Python strings (see cockpit_views.py's module docstring), so the ticket's original
"templates/models.html" plan is corrected here to match the real filesystem.

Lives in its OWN module rather than growing cockpit_views.py: the EU-235 land runs alongside
concurrent per-file-clustered cockpit work, and a fresh module keeps the whole view slice
merge-clean. server.py owns the routes; these builders take ``cfg`` (+ optional record/values/
errors) and return HTML — they never touch ``request``/``redirect``, same contract as
cockpit_views.

Secrets NEVER render (the EU-234 boundary): the list and the edit form show only
``Secrets.presence_display()`` — a fixed 8-bullet mask; an existing key is represented by a BLANK
password input whose *placeholder* is the mask, and submitting it blank means "keep the stored
key". There is no code path here that interpolates a raw credential into HTML
(tests/cockpit_models_test.py pins that on every rendered page).
"""
from __future__ import annotations

import html

from .cockpit_views import _wrap
from .model_registry import PROVIDERS
from .secrets import Secrets

# Human labels for the registry's provider enum (model_registry.PROVIDERS is the single source of
# truth for the VALUES — an id outside it never reaches the store, _validate rejects it).
PROVIDER_LABELS = {"anthropic": "Anthropic-compatible", "openai": "OpenAI-compatible"}

# Shared look for both pages — consumes the EU-39 design tokens _wrap injects.
_STYLE = (
    "<style>"
    ".mdl{width:100%;border-collapse:collapse;margin:14px 0;font-size:13px}"
    ".mdl th{color:var(--dim);text-align:left;font-weight:600;padding:7px 10px;"
    "border-bottom:1px solid var(--line2);font-size:11px;text-transform:uppercase;"
    "letter-spacing:.06em}"
    ".mdl td{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:middle}"
    ".mdl .mono{font-family:var(--mono);font-size:12px}"
    ".mdl .murl{color:var(--dim);max-width:260px;overflow:hidden;text-overflow:ellipsis;"
    "white-space:nowrap}"
    ".mdl .mname{font-weight:650}"
    ".mdl .mempty{color:var(--dim);text-align:center;padding:22px}"
    ".mdl .mact{white-space:nowrap;text-align:right}"
    ".mdl .mact form{display:inline-block;margin:0 0 0 6px}"
    ".mbtn{display:inline-block;background:var(--panel2);border:1px solid var(--line2);"
    "color:var(--ink);border-radius:var(--r-md);padding:6px 12px;font:inherit;font-size:12px;"
    "font-weight:600;cursor:pointer;text-decoration:none}"
    ".mbtn:hover{background:var(--line)}"
    ".mbtn.add{background:var(--accent);border-color:var(--accent);color:#fff;"
    "padding:9px 16px;font-size:13px}"
    ".mbtn.danger{background:var(--badbg);border-color:var(--badline);color:var(--bad)}"
    ".mform{max-width:560px;display:flex;flex-direction:column;gap:13px;margin-top:14px}"
    ".mform label{display:flex;flex-direction:column;gap:5px;font-size:12px;font-weight:600;"
    "color:var(--dim)}"
    ".mform input,.mform select{width:100%}"
    ".mform .mopt{font-weight:400;color:var(--faint)}"
    ".mform .mhint{font-size:11px;font-weight:400;color:var(--faint)}"
    ".mactions{display:flex;gap:12px;align-items:center;margin-top:6px}"
    ".mcancel{color:var(--dim);font-size:13px}"
    ".mdlerr{background:var(--badbg);border:1px solid var(--badline);color:var(--bad);"
    "border-radius:var(--r-md);padding:10px 14px;margin:0 0 14px;font-size:13px}"
    ".mdlerr ul{margin:6px 0 0 18px;padding:0}"
    # EU-237: the inline Test-connection result — hidden until a test runs, then busy/ok/err.
    ".mtest{display:none;border-radius:var(--r-md);padding:9px 12px;margin-top:4px;font-size:13px}"
    ".mtest.busy{display:block;background:var(--panel2);border:1px solid var(--line2);"
    "color:var(--dim)}"
    ".mtest.ok{display:block;background:var(--okbg);border:1px solid var(--okline);"
    "color:var(--ok)}"
    ".mtest.err{display:block;background:var(--badbg);border:1px solid var(--badline);"
    "color:var(--bad)}"
    "</style>")

# EU-237: the Test-connection wiring appended after the form. Posts the form's CURRENT values
# (FormData — same fields, same encoding as a submit, incl. the edit form's hidden record_id so a
# blank key means "retest the stored one") to /models/test and paints the {success, message} JSON
# into the inline result box. Same-origin fetch, so the EU-254 origin guard passes it; the reply
# never contains the key (see server.models_test_api), so nothing sensitive can land in the DOM.
_TEST_SCRIPT = (
    "<script>(function(){"
    "var btn=document.getElementById('mtest'),out=document.getElementById('mtestout');"
    "if(!btn||!out)return;"
    "btn.addEventListener('click',function(){"
    "var form=btn.closest('form');"
    "out.className='mtest busy';out.textContent='Testing connection\\u2026';btn.disabled=true;"
    "fetch('/models/test',{method:'POST',body:new FormData(form)})"
    ".then(function(r){return r.json();})"
    ".then(function(d){out.className='mtest '+(d.success?'ok':'err');"
    "out.textContent=(d.success?'\\u2713 ':'\\u2717 ')+(d.message||'');})"
    ".catch(function(){out.className='mtest err';"
    "out.textContent='\\u2717 test request failed \\u2014 is the cockpit still running?';})"
    ".finally(function(){btn.disabled=false;});"
    "});})();</script>")


def render_models_list(cfg) -> str:
    """The GET /models page: every registry backend with display name, provider label, model id,
    base URL, and a presence-only key cell (the fixed mask when a credential is stored, 'not set'
    otherwise) — plus Edit/Delete per row and the add button. Reads live like
    ``cockpit_views.backend_control`` does, so a just-added backend shows without a restart."""
    from .model_registry import ModelRegistry
    reg = ModelRegistry(cfg)
    store = Secrets(cfg)   # same cfg-anchored state dir set_credential writes (secrets.py:59-64)
    rows = ""
    for r in reg.list():
        rid = html.escape(str(r.get("id") or ""))
        name = html.escape(str(r.get("display_name") or ""))
        provider = html.escape(PROVIDER_LABELS.get(r.get("provider"), str(r.get("provider") or "?")))
        model_id = html.escape(str(r.get("model_id") or ""))
        base_url = html.escape(str(r.get("base_url") or ""))
        ref = r.get("credential_ref") or ""
        # Presence CHECK only — the value itself is read solely for `is not None` and the cell
        # renders the fixed mask, never anything derived from the secret.
        has_key = bool(ref) and store.get(ref) is not None
        key_cell = (Secrets.presence_display() if has_key
                    else '<span style="color:var(--warn)">not set</span>')
        rows += (
            f'<tr><td class=mname>{name}</td><td>{provider}</td>'
            f'<td class=mono>{model_id}</td><td class="mono murl" title="{base_url}">{base_url}</td>'
            f'<td>{key_cell}</td>'
            f'<td class=mact><a class=mbtn href="/models/edit/{rid}">Edit</a>'
            f'<form method=post action="/models/delete/{rid}" '
            f"onsubmit=\"return confirm('Delete {name}? Its stored API key is deleted with it.')\">"
            f'<button class="mbtn danger">Delete</button></form></td></tr>')
    if not rows:
        rows = ('<tr><td colspan=6 class=mempty>No model backends yet — add the first one to '
                'offer it in the cockpit Model selector.</td></tr>')
    inner = (
        _STYLE
        + '<p style="color:var(--dim);max-width:72ch;margin:4px 0 14px">User-defined model '
          'backends (custom Anthropic/OpenAI-compatible endpoints). Entries appear in the '
          'cockpit Model selector (EU-236); API keys live in the gitignored secrets store and '
          'only ever render as a mask.</p>'
        + '<p><a class="mbtn add" href="/models/add">+ Add model backend</a></p>'
        + '<table class=mdl><tr><th>Display name</th><th>Provider</th><th>Model ID</th>'
          '<th>Base URL</th><th>API key</th><th></th></tr>'
        + rows + '</table>')
    return _wrap("Model backends", inner)


def render_model_form(cfg, record: dict | None = None, values: dict | None = None,
                      errors: list | None = None) -> str:
    """The add/edit form (GET /models/add, GET /models/edit/<id>, and both POSTs' error
    re-render). ``record`` = the existing registry record (edit mode); ``values`` = the submitted
    form fields to re-fill after a validation failure (they win over ``record``, so the operator
    never loses input); ``errors`` = messages to show — the registry's own ``_validate`` wording,
    translated by the route, not a duplicated schema here. Carries the EU-237 Test-connection
    button + inline result box (see ``_TEST_SCRIPT``) so a config can be probed before saving."""
    base = dict(record or {})
    if values:
        base.update(values)
    editing = record is not None
    action = (f"/models/edit/{html.escape(str(record.get('id') or ''))}" if editing
              else "/models/add")
    title = "Edit model backend" if editing else "Add model backend"

    def _val(name: str) -> str:
        return html.escape(str(base.get(name) or ""))

    err_html = ""
    if errors:
        items = "".join(f"<li>{html.escape(str(e))}</li>" for e in errors)
        err_html = (f'<div role=alert class=mdlerr><b>Not saved — fix and resubmit:</b>'
                    f'<ul>{items}</ul></div>')

    sel = str(base.get("provider") or "")
    popts = (f'<option value="" disabled {"selected" if sel not in PROVIDERS else ""}>'
             'Choose provider…</option>')
    for p in PROVIDERS:
        popts += (f'<option value="{p}" {"selected" if sel == p else ""}>'
                  f'{html.escape(PROVIDER_LABELS.get(p, p))}</option>')

    if editing:
        # An existing key is a blank password input whose PLACEHOLDER is the mask — blank submit
        # means "keep the stored key"; the raw value never leaves the Secrets store.
        key_input = (f'<input type=password name=api_key value="" '
                     f'placeholder="{Secrets.presence_display()}" autocomplete=new-password>'
                     '<span class=mhint>A key is stored — leave blank to keep it, or paste a new '
                     'one to replace it.</span>')
    else:
        key_input = ('<input type=password name=api_key value="" placeholder="sk-…" required '
                     'autocomplete=new-password>'
                     '<span class=mhint>Stored in the gitignored secrets store (EU-234) — only a '
                     'masked reference ever renders.</span>')

    # EU-237: the edit form carries its record id so a Test-connection click with a BLANK key
    # field can mean "retest the stored credential" — resolved server-side, never sent to the
    # browser. The add form has no record yet, so it carries nothing.
    record_id_input = (f'<input type=hidden name=record_id '
                       f'value="{html.escape(str(record.get("id") or ""))}">' if editing else "")
    inner = (
        _STYLE + err_html
        + f'<form method=post action="{action}" class=mform>'
        + record_id_input
        + ('<label>Display name<input type=text name=display_name '
           f'value="{_val("display_name")}" required></label>')
        + f'<label>Provider<select name=provider required>{popts}</select></label>'
        + ('<label>Base URL<input type=text name=base_url '
           f'value="{_val("base_url")}" placeholder="https://api.anthropic.com" required></label>')
        + ('<label>Model ID<input type=text name=model_id '
           f'value="{_val("model_id")}" placeholder="claude-opus-4-6" required></label>')
        + ('<label>Small/fast model ID <span class=mopt>(optional)</span>'
           '<input type=text name=small_fast_model_id '
           f'value="{_val("small_fast_model_id")}"></label>')
        + f'<label>API key{key_input}</label>'
        + '<div class=mactions>'
        + f'<button type=submit>{"Save changes" if editing else "Add backend"}</button>'
        # type=button: the test must never submit/persist the form (EU-237's no-state-change rule).
        + '<button type=button id=mtest class=mbtn>Test connection</button>'
        + '<a href="/models" class=mcancel>Cancel</a></div>'
        + '<div id=mtestout class=mtest role=status></div>'
        + '</form>' + _TEST_SCRIPT)
    return _wrap(title, inner)


# The Roster button in _control_bar's nav cluster — a stable, distinctive literal since the
# EU-299 nav-cluster work. tests/cockpit_models_test.py pins the link's presence on GET /, so a
# rename of this anchor fails the suite instead of silently dropping the link.
_NAV_ANCHOR = '<a class="btn" href="/roster-doc"'


def add_models_nav_link(bar_html: str) -> str:
    """Splice the '/models' nav button into an already-rendered control bar (server.index).

    Injection-by-anchor instead of editing ``cockpit_views._control_bar`` directly: EU-235's
    files are clustered apart from the concurrent cockpit_views work, and server.py already
    post-processes rendered pages this way (see tasks_page's ``page.replace("</header>", …)``).
    Idempotent, and a missing anchor degrades to an unchanged bar — the link is lost but the
    cockpit never breaks."""
    link = ('<a class="btn" href="/models" '
            'title="Model backends — view, add, edit or delete custom model endpoints">'
            '&#129504; Models</a>')
    if _NAV_ANCHOR not in bar_html or 'href="/models"' in bar_html:
        return bar_html
    return bar_html.replace(_NAV_ANCHOR, link + _NAV_ANCHOR, 1)
