"""The Arabic version of the app is complete.

Every word the client says goes through t("..."), tn(n, "one", "other") or a
table entry marked N_("..."), keyed by its English wording. These tests read
those keys out of the source and hold static/js/ar.js to them: a new screen
cannot ship with English left in the Arabic version, and a removed one cannot
leave dead translations behind.

What is deliberately not here: factory data (stage, station, area and phase
names, codes, descriptions) is shown as typed, and the server's own error
messages arrive in English.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"
STRING = r'"((?:[^"\\]|\\.)*)"'
PLURAL_FORMS = {"zero", "one", "two", "few", "many", "other"}


def _unquote(raw: str) -> str:
    return json.loads(f'"{raw}"')


def _source() -> str:
    src = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
    # Block comments start a line here; a "/*" inside a string ("image/*")
    # must not be taken for one.
    return re.sub(r"(?m)^[ \t]*/\*.*?\*/", "", src, flags=re.S)


def _keys() -> tuple[set[str], set[str]]:
    src = _source()
    plain = {
        _unquote(m.group(1))
        for m in re.finditer(r"(?<![\w.$])(?:t|N_)\(\s*" + STRING, src)
    }
    plural = {
        _unquote(m.group(2))
        for m in re.finditer(r"(?<![\w.$])tn\([^,]+,\s*" + STRING + r",\s*" + STRING, src, flags=re.S)
    }
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    plain |= set(re.findall(r'data-i18n(?:-title)?="([^"]+)"', html))
    return plain, plural


def _arabic() -> dict:
    src = (STATIC / "js" / "ar.js").read_text(encoding="utf-8")
    body = src[src.index("const LSF_AR = {") + len("const LSF_AR = ") : src.rindex("};") + 1]
    # Entries are written one per line as JSON key/value pairs; a trailing
    # comma is the only thing standing between the object and JSON.
    return json.loads(re.sub(r",\s*}$", "}", body.strip()))


def _placeholders(text: str) -> set[str]:
    return set(re.findall(r"\{(\w+)\}", text))


def test_every_key_used_has_arabic():
    plain, plural = _keys()
    arabic = _arabic()
    missing = sorted((plain | plural) - set(arabic))
    assert not missing, f"no Arabic for: {missing}"


def test_no_arabic_left_over():
    plain, plural = _keys()
    unused = sorted(set(_arabic()) - plain - plural)
    assert not unused, f"Arabic for text the app no longer says: {unused}"


def test_counted_text_has_every_arabic_form():
    _, plural = _keys()
    arabic = _arabic()
    for key in plural:
        entry = arabic[key]
        if isinstance(entry, dict):
            assert set(entry) == PLURAL_FORMS, f"{key!r}: forms {sorted(entry)}"


def test_placeholders_survive_translation():
    """{n}, {code}... are filled by name: one lost in translation prints as
    nothing, one invented prints literally. The singular forms may spell the
    number out ("قطعة واحدة") and so drop {n}, nothing else."""
    for key, entry in _arabic().items():
        expected = _placeholders(key)
        forms = entry.items() if isinstance(entry, dict) else [("other", entry)]
        for form, text in forms:
            got = _placeholders(text)
            allowed = expected - {"n"} if form in {"zero", "one", "two"} else expected
            assert allowed <= got <= expected, f"{key!r} [{form}]: {sorted(got)} vs {sorted(expected)}"


def test_translation_runtime():
    """t() and tn() as the browser runs them: fallback to English, the
    plural rules, and the switch itself. Skipped where Node is not installed."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    script = """
      globalThis.localStorage = { store: {}, getItem(k) { return this.store[k] ?? null; }, setItem(k, v) { this.store[k] = v; } };
      globalThis.navigator = { language: "en-GB" };
      const fs = require("fs");
      const vm = require("vm");
      const load = (f) => vm.runInThisContext(fs.readFileSync(f, "utf8"));
      load(process.argv[1]); load(process.argv[2]);
      const out = { en: [t("Done"), tn(1, "{n} pc", "{n} pcs"), tn(4, "{n} pc", "{n} pcs"), t("not a key {x}", { x: 1 })] };
      LSF_I18N.setLang("ar");
      out.ar = [t("Done"), tn(1, "{n} pc", "{n} pcs"), tn(2, "{n} pc", "{n} pcs"), tn(4, "{n} pc", "{n} pcs"),
                tn(40, "{n} pc", "{n} pcs"), t("{n} pending", { n: 3 }), t("not a key {x}", { x: 1 }), LSF_I18N.lang()];
      out.stored = localStorage.getItem("lang");
      console.log(JSON.stringify(out));
    """
    result = subprocess.run(
        [node, "-e", script, str(STATIC / "js" / "ar.js"), str(STATIC / "js" / "i18n.js")],
        capture_output=True, text=True, check=True,
    )
    out = json.loads(result.stdout)
    assert out["en"] == ["Done", "1 pc", "4 pcs", "not a key 1"]
    assert out["ar"] == [
        "منتهي", "قطعة واحدة", "قطعتان", "4 قطع", "40 قطعة", "3 في الانتظار", "not a key 1", "ar",
    ]
    assert out["stored"] == "ar"
