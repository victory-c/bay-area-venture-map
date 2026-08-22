// Vercel Web Analytics + Speed Insights bootstrap queues.
//
// These live in a file rather than inline in index.html so the Content
// Security Policy can forbid inline script outright. The alternative —
// pinning a sha256 hash per inline block — breaks silently on any
// whitespace edit, and silently-broken analytics is hard to notice.
window.va = window.va || function () {
  (window.vaq = window.vaq || []).push(arguments);
};
window.si = window.si || function () {
  (window.siq = window.siq || []).push(arguments);
};
