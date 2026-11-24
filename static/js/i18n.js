/* The app's two languages: English and Arabic.

   Every word the app itself says goes through t() (or tn() when it counts
   something), keyed by its English wording. English is the fallback, so a
   string with no Arabic yet still reads; tests/test_i18n.py fails if one is
   missing, so that never ships.

   What is NOT translated: anything the factory typed -- stage, station, area
   and phase names, item codes and descriptions, reason codes, event type
   names. Those are data (CLAUDE.md rule 2), and appear as they were entered.

   The language belongs to the device, not the account: floor phones are
   shared, and whoever holds one reads it in the language it is set to. The
   first time, it follows the phone's own language. */

"use strict";

const LSF_I18N = (() => {
  const LANGS = ["en", "ar"];

  function initial() {
    let stored = null;
    try { stored = localStorage.getItem("lang"); } catch { /* storage blocked */ }
    if (LANGS.includes(stored)) return stored;
    return (navigator.language || "").toLowerCase().startsWith("ar") ? "ar" : "en";
  }
  let lang = initial();

  /* "{n} pcs" with {n: 4} -> "4 pcs". A placeholder with no value is left as
     it is, so a slip shows up on screen instead of reading "undefined". */
  function fill(text, vars) {
    if (!vars) return text;
    return text.replace(/\{(\w+)\}/g, (whole, name) => (name in vars ? String(vars[name]) : whole));
  }

  function t(key, vars) {
    const ar = lang === "ar" ? LSF_AR[key] : null;
    return fill(typeof ar === "string" ? ar : key, vars);
  }

  /* Counted text. English has two forms; Arabic has six (one, two, few,
     many, other, zero), so its entry may be an object of them, keyed by the
     English plural. An Arabic entry written as a plain string is one that
     reads right for any number ("العدد: {n}"). */
  const arabicRules = typeof Intl !== "undefined" && Intl.PluralRules ? new Intl.PluralRules("ar") : null;
  function tn(n, one, other, vars) {
    const all = { n, ...vars };
    if (lang !== "ar") return fill(Number(n) === 1 ? one : other, all);
    const ar = LSF_AR[other];
    if (ar && typeof ar === "object") {
      const form = arabicRules ? arabicRules.select(Number(n)) : "other";
      return fill(ar[form] ?? ar.other, all);
    }
    return fill(typeof ar === "string" ? ar : other, all);
  }

  /* The page itself: direction, language, and the few words in index.html. */
  function apply() {
    if (typeof document === "undefined") return;
    const root = document.documentElement;
    root.lang = lang;
    root.dir = lang === "ar" ? "rtl" : "ltr";
    document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
    document.querySelectorAll("[data-i18n-title]").forEach((el) => { el.title = t(el.dataset.i18nTitle); });
  }

  function setLang(next) {
    if (!LANGS.includes(next)) return;
    lang = next;
    try { localStorage.setItem("lang", next); } catch { /* still switches for this visit */ }
    apply();
  }

  return {
    t, tn, apply, setLang,
    lang: () => lang,
    // Dates and times: the day first and the month by name in both; Latin
    // digits in both, because codes, quantities and revisions are Latin and a
    // line mixing two sets of digits is harder to read on the floor.
    locale: () => (lang === "ar" ? "ar-u-nu-latn" : "en-GB"),
  };
})();

// Loaded at the end of <body>, after the elements it labels; the CSP allows
// no inline script to do this from the page.
LSF_I18N.apply();

const t = LSF_I18N.t;
const tn = LSF_I18N.tn;
// Marks a string kept in a table to be translated where it is shown, with
// t(). Returns it unchanged; it exists so tests/test_i18n.py can find it.
const N_ = (text) => text;
