/* Regression Detective UI — renders the investigation event stream (SSE) into the tree, strip, feed and report. */
(function () {
  "use strict";
  var SVGNS = "http://www.w3.org/2000/svg";
  var $ = function (id) { return document.getElementById(id); };
  var BW = 128, BH = 48, GAP = 18, HY = 104, RY = 18, RW = 150, RH = 42, EVH = 30, EVG = 8, MAXEV = 4;

  var S = null;           // per-investigation state
  var es = null;          // EventSource
  var clockInt = null;

  function fresh() {
    return { id: null, hyps: {}, order: [], ev: {}, active: {}, commits: [], cells: [], track: null,
             rangeByTrack: {}, verdicts: {}, t0: Date.now(), lastT: 0, finished: false, feedN: 0,
             decisions: 0, root: null, budget: null };
  }

  // ------------------------------------------------------------------ helpers
  function el(n, a) { var e = document.createElementNS(SVGNS, n); for (var k in a) e.setAttribute(k, a[k]); return e; }
  function txt(x, y, s, cls, anchor) {
    var t = el("text", { x: x, y: y, "text-anchor": anchor || "middle" }); t.setAttribute("class", cls || "lbl");
    t.textContent = s; return t;
  }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function rich(s) {   // agent messages use <ok>/<no>/<hl>/<dim> tags; everything else is escaped
    return esc(s).replace(/&lt;(\/?)(ok|no|hl|dim)&gt;/g, function (_, close, tag) {
      return close ? "</span>" : '<span class="' + tag + '">'; });
  }
  function fmtClock(sec) { sec = Math.max(0, Math.floor(sec)); return Math.floor(sec / 60) + ":" + String(sec % 60).padStart(2, "0"); }
  function api(path, opts) {
    return fetch(path, opts).then(function (r) { if (!r.ok) throw new Error(r.status + " " + r.statusText); return r.json(); });
  }
  function trunc(s, n) { s = String(s || ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; }

  // ------------------------------------------------------------------ tree
  var COLORS = {
    open: { f: "var(--amber-soft)", s: "var(--amber)" },
    active: { f: "var(--accent-soft)", s: "var(--accent)" },
    eliminated: { f: "var(--grey-soft)", s: "var(--grey)" },
    unreachable: { f: "var(--grey-soft)", s: "var(--grey)" },
    confirmed: { f: "var(--red-soft)", s: "var(--red)" }
  };

  function layout() {
    var n = S.order.length, w = Math.max(800, 40 + n * (BW + GAP));
    var x0 = (w - (n * BW + (n - 1) * GAP)) / 2;
    S.order.forEach(function (id, i) { S.hyps[id].x = x0 + i * (BW + GAP); });
    var maxEv = 0;
    S.order.forEach(function (id) { maxEv = Math.max(maxEv, Math.min(MAXEV, S.hyps[id].evidence.length)); });
    var h = HY + BH + 26 + maxEv * (EVH + EVG) + 24;
    $("tree").setAttribute("viewBox", "0 0 " + w + " " + Math.max(220, h));
    return w;
  }

  function drawTree() {
    var tree = $("tree"); tree.innerHTML = "";
    if (!S.root) return;
    var w = layout(), rx = w / 2 - RW / 2;
    S.order.forEach(function (id) {
      var h = S.hyps[id], cx = h.x + BW / 2;
      var p = el("path", { d: "M" + (w / 2) + " " + (RY + RH) + " C " + (w / 2) + " " + (HY - 30) + ", " + cx + " " +
        (HY - 40) + ", " + cx + " " + HY, "class": "edge" });
      p.id = "e-" + id; tree.appendChild(p); h.edge = p;
    });
    var g = el("g", { "class": "node" });
    g.appendChild(el("rect", { x: rx, y: RY, width: RW, height: RH, rx: 8, "class": "bx", fill: "var(--red-soft)",
      stroke: "var(--red)", "stroke-width": 1.5 }));
    g.appendChild(txt(w / 2, RY + 18, trunc(S.root.label, 24)));
    var s = txt(w / 2, RY + 33, S.root.sub || "FAILS", "pct"); s.setAttribute("fill", "var(--red)"); g.appendChild(s);
    tree.appendChild(g);
    S.order.forEach(function (id) { drawHyp(id); });
  }

  function drawHyp(id) {
    var h = S.hyps[id], tree = $("tree"), x = h.x;
    var g = el("g", { "class": "node" }); g.id = "n-" + id;
    var title = el("title", {}); title.textContent = h.id + " " + h.title + "\n" + (h.criteria || "") +
      (h.rationale ? "\n" + h.rationale : ""); g.appendChild(title);
    h.rect = el("rect", { x: x, y: HY, width: BW, height: BH, rx: 8, "class": "bx", "stroke-width": 1.5 });
    g.appendChild(h.rect);
    var parts = (h.short || h.title).split("|");
    g.appendChild(txt(x + BW / 2, HY + 15, trunc(parts[0], 22)));
    if (parts[1]) g.appendChild(txt(x + BW / 2, HY + 27, trunc(parts[1], 22)));
    g.appendChild(txt(x + 8, HY + BH - 21, h.id, "pct", "start"));
    g.appendChild(el("rect", { x: x + 10, y: HY + BH - 13, width: BW - 44, height: 4, rx: 2, "class": "bar-bg" }));
    h.bar = el("rect", { x: x + 10, y: HY + BH - 13, width: 0, height: 4, rx: 2, "class": "bar-fg" }); g.appendChild(h.bar);
    h.pctEl = txt(x + BW - 8, HY + BH - 9, "", "pct", "end"); g.appendChild(h.pctEl);
    tree.appendChild(g); h.g = g;
    paintHyp(id);
    var evs = h.evidence.slice(-MAXEV);
    evs.forEach(function (e, k) { drawEv(h, e, k, false); });
  }

  function paintHyp(id) {
    var h = S.hyps[id]; if (!h.rect) return;
    var st = S.active[id] && (h.status === "open") ? "active" : h.status;
    var c = COLORS[st] || COLORS.open;
    h.rect.setAttribute("fill", c.f); h.rect.setAttribute("stroke", c.s);
    h.bar.setAttribute("fill", c.s);
    h.bar.setAttribute("width", Math.max(0, (BW - 44) * Math.min(1, h.p)));
    h.pctEl.textContent = (h.p * 100 < 1 && h.p > 0 ? "<1" : Math.round(h.p * 100)) + "%";
    var dead = h.status === "eliminated" || h.status === "unreachable";
    h.g.classList.toggle("state-dead", dead);
    h.g.classList.toggle("pulse", st === "active");
    if (h.edge) {
      h.edge.classList.toggle("dead", dead);
      h.edge.setAttribute("stroke", dead ? "var(--grey)" : (h.status === "confirmed" ? "var(--red)" : "var(--border)"));
    }
  }

  function drawEv(h, e, k, animate) {
    var tree = $("tree"), w = 138, cx = h.x + BW / 2, x = cx - w / 2;
    var y = HY + BH + 26 + k * (EVH + EVG);
    var col = { ok: "var(--green)", no: "var(--red)", dim: "var(--grey)" }[e.tone] || "var(--grey)";
    var fill = { ok: "var(--green-soft)", no: "var(--red-soft)", dim: "var(--grey-soft)" }[e.tone] || "var(--grey-soft)";
    var g = el("g", { "class": "ev" + (animate ? "" : " show") });
    var top = k === 0 ? HY + BH : y - EVG;
    g.appendChild(el("path", { d: "M" + cx + " " + top + " L " + cx + " " + y, "class": "edge", stroke: col }));
    g.appendChild(el("rect", { x: x, y: y, width: w, height: EVH, rx: 7, fill: fill, stroke: col, "stroke-width": 1 }));
    var parts = String(e.label || "").split("|");
    var t1 = txt(cx, y + 12.5, trunc(parts[0], 24), "pct"); t1.setAttribute("fill", col); g.appendChild(t1);
    if (parts[1]) { var t2 = txt(cx, y + 24, trunc(parts[1], 24), "pct"); t2.setAttribute("fill", "var(--muted)"); g.appendChild(t2); }
    if (e.evidence && e.evidence[0]) {
      var tt = el("title", {}); tt.textContent = "evidence " + e.evidence.join(", "); g.appendChild(tt);
      g.style.cursor = "pointer";
      g.addEventListener("click", function () { showEvidence(e.evidence[0]); });
    }
    tree.appendChild(g);
    if (animate) requestAnimationFrame(function () { g.classList.add("show"); });
  }

  function addHyp(h) {
    S.hyps[h.id] = { id: h.id, title: h.title, short: h.short, p: h.p, status: h.status || "open",
                     criteria: h.criteria, rationale: h.rationale, kind: h.kind, evidence: [] };
    S.order.push(h.id);
  }

  // ------------------------------------------------------------------ strip
  function buildStrip(commits) {
    var strip = $("strip"); strip.innerHTML = ""; S.cells = [];
    strip.classList.toggle("dense", commits.length > 40);
    commits.forEach(function (c, i) {
      var d = document.createElement("div"); d.className = "c" + (i === 0 ? " endpoint" : "") +
        (i > 0 && c.relevance < 0.3 ? " low" : "");
      d.title = "c" + String(i).padStart(2, "0") + " · " + c.short + " · " + c.subject + "\n" + c.author + " · " +
        c.date.slice(0, 10) + (i ? "\nrelevance " + c.relevance + " — " + c.why : "\n(last known good)");
      var s = document.createElement("span"); s.className = "n"; s.textContent = i; d.appendChild(s);
      strip.appendChild(d); S.cells.push(d);
    });
    $("s-left").textContent = commits[0].short + " · last known good";
    $("s-right").textContent = commits[commits.length - 1].short + " · HEAD";
    $("bistag").textContent = (commits.length - 1) + " commits · range unknown";
  }

  function setRange(lo, hi, remaining, track) {
    S.track = track;
    var n = S.cells.length;
    S.cells.forEach(function (c, i) {
      if (i === 0) return;
      var known = S.verdicts[i];
      c.classList.toggle("out", (i < lo || i > hi) && !known);
    });
    var bar = $("rangebar");
    bar.style.marginLeft = (lo / n * 100) + "%"; bar.style.width = (Math.max(1, hi - lo + 1) / n * 100) + "%";
    $("bistag").textContent = (n - 1) + " commits · " + remaining + " in range" + (track && track !== "H1" ? " (" + track + ")" : "");
  }

  function pmsg(html, spin) { $("probemsg").innerHTML = (spin ? '<span class="spin"></span>' : "") + html; }

  // ------------------------------------------------------------------ feed
  function say(ev) {
    var feed = $("feed");
    var r = document.createElement("div"); r.className = "row";
    var w = document.createElement("span"); w.className = "who w-" + ev.agent; w.textContent = ev.agent;
    var m = document.createElement("span"); m.className = "msg";
    m.innerHTML = rich(ev.title) + (ev.detail ? '<span class="o">' + rich(ev.detail) + "</span>" : "");
    if (ev.evidence && ev.evidence.length) {
      var chips = document.createElement("span"); chips.className = "chips";
      ev.evidence.slice(0, 5).forEach(function (eid) {
        var c = document.createElement("span"); c.className = "chip"; c.textContent = eid;
        c.title = "show raw tool output";
        c.addEventListener("click", function () { toggleRaw(m, eid); });
        chips.appendChild(c);
      });
      m.appendChild(chips);
    }
    r.appendChild(w); r.appendChild(m); feed.appendChild(r);
    feed.scrollTop = feed.scrollHeight;
    S.feedN++; $("feedtag").textContent = S.feedN + " events";
  }

  function toggleRaw(container, eid) {
    var existing = container.querySelector('pre.raw[data-eid="' + eid + '"]');
    if (existing) { existing.remove(); return; }
    var pre = document.createElement("pre"); pre.className = "raw"; pre.dataset.eid = eid; pre.textContent = "loading " + eid + "…";
    container.appendChild(pre);
    api("/api/investigations/" + S.id + "/evidence/" + eid).then(function (o) {
      pre.textContent = "[" + o.id + "] " + o.agent + " · " + o.tool + " · " + o.action + " (" + o.kind + ")\n" +
        o.summary + "\n\n" + (o.raw || "(no raw output)");
    }).catch(function (e) { pre.textContent = "could not load " + eid + ": " + e.message; });
  }

  function showEvidence(eid) {
    var rows = $("feed").querySelectorAll(".chip");
    for (var i = rows.length - 1; i >= 0; i--) {
      if (rows[i].textContent === eid) { rows[i].click(); rows[i].scrollIntoView({ block: "nearest", behavior: "smooth" }); return; }
    }
  }

  // ------------------------------------------------------------------ policy + budget
  function policy(ev) {
    var max = 0; (ev.candidates || []).forEach(function (c) { max = Math.max(max, c.score); });
    var chosen = {}; (ev.chosen || []).forEach(function (c) { chosen[c.name + c.label] = 1; });
    var html = "<table><tr><th>action</th><th>hyp</th><th class=num>EIG</th><th class=num>cost</th><th>score</th></tr>";
    (ev.candidates || []).slice(0, 6).forEach(function (c) {
      html += '<tr class="' + (chosen[c.name + c.label] ? "chosen" : "") + '"><td class="lbl">' + esc(c.label) +
        "</td><td>" + esc(c.hypothesis) + '</td><td class="num">' + c.eig_bits.toFixed(2) + ' b</td><td class="num">' +
        Math.round(c.cost_s) + ' s</td><td><span class="sbar" style="width:' + (max ? Math.max(2, 90 * c.score / max) : 2) +
        'px"></span></td></tr>';
    });
    html += "</table>";
    $("policy").innerHTML = html;
    $("poltag").textContent = "iteration " + ev.iteration + " · remaining uncertainty " + ev.entropy_bits.toFixed(2) + " bits";
    $("entropy").textContent = "H = " + ev.entropy_bits.toFixed(2) + " bits";
  }

  function budget(ev) {
    S.budget = ev;
    $("bud-probes").style.width = Math.min(100, 100 * ev.probes / ev.max_probes) + "%";
    $("bud-probes-t").textContent = ev.probes + " / " + ev.max_probes;
    $("bud-time").style.width = Math.min(100, 100 * ev.elapsed / ev.max_seconds) + "%";
    $("bud-time-t").textContent = Math.round(ev.elapsed) + "s";
    $("bud-llm").style.width = Math.min(100, 100 * ev.llm_calls / ev.max_llm_calls) + "%";
    $("bud-llm-t").textContent = ev.llm_calls + " / " + ev.max_llm_calls;
  }

  // ------------------------------------------------------------------ report
  function diffHtml(d) {
    return String(d || "").split("\n").map(function (l) {
      var c = l.startsWith("+++") || l.startsWith("---") || l.startsWith("diff ") || l.startsWith("index ") ? "dc" :
        l.startsWith("@@") ? "dh" : l.startsWith("+") ? "dp" : l.startsWith("-") ? "dm" : "";
      return c ? '<span class="' + c + '">' + esc(l) + "</span>" : esc(l);
    }).join("\n");
  }
  function kv(k, v, cls) { return '<div class="kv ' + (cls || "") + '"><div class="k">' + esc(k) + '</div><div class="v' +
    (cls && cls.indexOf("prose") >= 0 ? " prose" : "") + '">' + v + "</div></div>"; }

  function renderReport(r) {
    if (!r) return;
    var c = r.conclusion || {}, e = r.explanation || {}, v = r.verification || {}, p = r.patch || {};
    var st = r.status;
    var html = "";
    if (st === "error") {
      html += '<div class="warnbox">Investigation failed: ' + esc((r.notes || []).join("; ")) + "</div>";
    }
    if (v.downgraded) html += '<div class="warnbox">Confidence ' + v.confidence + " is below the threshold of " + v.threshold +
      ": the patch proposal is suppressed and this is a findings summary.</div>";
    html += '<div class="rgrid">';
    if ((r.culprits || []).length) {
      html += kv(r.culprits.length > 1 ? "Culprit commits (multi-cause)" : "Culprit commit",
        r.culprits.map(function (x) { return esc(x.short) + " · “" + esc(x.subject) + "”"; }).join("<br>"));
      html += kv("Author / date", r.culprits.map(function (x) { return esc(x.author) + " · " + esc(x.date.slice(0, 10)); }).join("<br>"));
    } else {
      html += kv("Conclusion", esc(st.replace(/_/g, " ")) + (c.hypothesis ? " · " + esc(c.hypothesis) + " p=" + c.p : ""));
      html += kv("Window", esc(r.last_good ? r.last_good.short : "?") + " → " + esc(r.head ? r.head.short : "?") + " · " +
        r.window.commits + " commits");
    }
    html += kv("Classification", esc((e.classification || "–").replace(/_/g, " ")) +
      (e.classification_rationale ? " — " + esc(e.classification_rationale) : ""));
    var cnt = v.counts || {};
    html += kv("Confidence", '<span class="' + (v.confidence >= v.threshold ? "ok" : "no") + '">' + v.confidence + "</span> · " +
      (cnt.supported || 0) + " of " + (v.claims || []).length + " claims evidence-backed" +
      (cnt.inferred ? " · " + cnt.inferred + " inferred" : "") + (cnt.unsupported ? " · " + cnt.unsupported + " unsupported" : ""));
    if (c.packages && c.packages.length) {
      var sus = (r.dependency || {}).suspects || {};
      html += kv("Package(s)", c.packages.map(function (k) { var s = sus[k] || {};
        return esc(k) + " " + esc(s.lg_version || "") + " → " + esc(s.head_version || ""); }).join("<br>"));
    }
    if (st === "budget_exhausted" || st === "inconclusive") {
      html += kv("Remaining candidates", (c.candidates || []).map(function (i) {
        var cm = S.commits[i]; return cm ? esc(cm.short) + " " + esc(trunc(cm.subject, 40)) : i; }).join("<br>") || "–");
    }
    if (r.flaky && r.flaky.runs && st === "flaky") {
      html += kv("Runs", "HEAD " + r.flaky.head.fails + "/" + r.flaky.runs + " fail · last-good " + r.flaky.lg.fails + "/" +
        r.flaky.runs + " fail");
    }
    html += "</div>";
    (r.culprit_diffs || []).forEach(function (d) {
      html += '<div class="kv full" style="margin-top:10px"><div class="k">Diff of ' + esc(d.short) + "</div>" +
        '<pre class="diff">' + diffHtml(trunc(d.diff, 6000)) + "</pre></div>";
    });
    html += '<div class="kv" style="margin-top:10px"><div class="k">' + (st === "resolved" || st === "multi_cause" ? "Why it breaks" : "Summary") +
      '</div><div class="v prose"><b>' + esc(e.summary || "") + "</b><br>" + esc(e.mechanism || "") +
      (e.why_assertion_breaks ? "<br><br>" + esc(e.why_assertion_breaks) : "") +
      (e.next_step ? "<br><br>Next step: " + esc(e.next_step) : "") +
      (e.suggested_fix ? "<br><br>Suggested fix: " + esc(e.suggested_fix) : "") +
      (e.source ? '<br><span class="muted">explanation: ' + esc(e.source) + "</span>" : "") + "</div></div>";
    if (p && p.diff) {
      html += '<div class="kv full" style="margin-top:10px"><div class="k">Proposed patch' +
        (v.patch_allowed ? "" : " — suppressed by verifier") + " · " + esc(p.source || "") + " · suite " +
        p.suite_passed + " passed / " + p.suite_failed + " failed · " + (p.collateral || []).length + " newly broken</div>" +
        '<pre class="diff">' + diffHtml(p.diff) + "</pre></div>";
    } else if (p && p.status && p.status !== "skipped") {
      html += kv("Patch", esc(p.status + ": " + (p.reason || "")), "full");
    }
    if ((v.missing_evidence || []).length) {
      html += '<div class="kv" style="margin-top:10px"><div class="k">Missing evidence</div><div class="v prose">' +
        v.missing_evidence.map(esc).join("<br>") + "</div></div>";
    }
    html += '<details class="more"><summary>Claims audited by the Evidence Verifier (' + (v.claims || []).length + ")</summary>" +
      '<table class="claims">' + (v.claims || []).map(function (cl) {
        return '<tr><td class="v v-' + cl.verdict + '">' + esc(cl.verdict) + "</td><td>" + esc(cl.text) +
          '<br><span class="muted">' + esc(cl.agent) + " · " + esc(cl.checked_by) + (cl.reason ? " · " + esc(cl.reason) : "") +
          '</span></td><td class="e">' + esc((cl.evidence || []).join(", ")) + "</td></tr>";
      }).join("") + "</table></details>";
    var pr = r.pr;
    html += '<div class="btns">';
    if (pr && pr.url) html += '<a class="btn pri" href="' + esc(pr.url) + '" target="_blank">Open draft PR</a>';
    else if (pr && v.patch_allowed) html += '<span class="btn pri" title="' + esc((pr.files || []).join("\n")) +
      '">Draft PR prepared (local)</span>';
    html += '<a class="btn" href="#" id="evlog">View evidence log (' + r.evidence_count + " observations)</a>";
    html += '<a class="btn" href="/api/investigations/' + esc(r.id) + '/report?format=md" target="_blank">Export report (.md)</a>';
    html += '<a class="btn" href="/api/investigations/' + esc(r.id) + '/report" target="_blank">JSON</a>';
    html += "</div>";
    html += '<div class="muted" style="margin-top:10px">sandbox: ' + esc((r.safety.sandbox || {}).kind) + " — " +
      esc((r.safety.sandbox || {}).isolation || "") + "<br>" + esc(r.safety.repository) + "<br>" + esc(r.safety.data) + "</div>";
    $("repbody").innerHTML = html;
    $("reptag").textContent = v.patch_allowed ? "verified" : (st || "");
    $("report").classList.add("show");
    var lg = $("evlog");
    if (lg) lg.addEventListener("click", function (ev) { ev.preventDefault(); evidenceLog(lg); });
  }

  function evidenceLog(anchor) {
    var box = document.getElementById("evbox");
    if (box) { box.remove(); return; }
    api("/api/investigations/" + S.id + "/evidence").then(function (list) {
      box = document.createElement("div"); box.id = "evbox"; box.className = "kv full"; box.style.marginTop = "10px";
      box.innerHTML = '<div class="k">Evidence log</div>' + list.map(function (o) {
        return '<div class="row" style="padding:4px 0"><span class="who w-' + esc(o.agent) + '">' + esc(o.id) + "</span>" +
          '<span class="msg">' + esc(o.action) + '<span class="o">' + esc(o.tool) + " · " + esc(o.kind) + " · " + esc(o.summary) +
          "</span></span></div>";
      }).join("");
      anchor.parentNode.after(box);
    });
  }

  // ------------------------------------------------------------------ event dispatch
  function handle(ev) {
    S.lastT = ev.t || S.lastT;
    switch (ev.type) {
      case "investigation.started":
        $("crumb").textContent = (ev.repo || "").split("/").slice(-3).join("/") + " · " + ev.test;
        $("banner").hidden = false; $("b-test").textContent = ev.test; $("b-sub").textContent = "reproducing in the sandbox…";
        setStatus("investigating", "run");
        var cfg = ev.config || {};
        $("llmpill").textContent = cfg.llm && cfg.llm.enabled ? "Gemini · " + (cfg.llm.model || "") : "LLM off (heuristics)";
        $("llmpill").className = "pill " + (cfg.llm && cfg.llm.enabled ? "on" : "");
        $("budtag").textContent = "history index " + (cfg.history_index ? "on" : "off") + " · forge " + (cfg.forge || "");
        break;
      case "agent.message":
        say(ev);
        if (ev.agent === "TRIAGE" && ev.title.indexOf("Parsing") === 0) $("b-sub").innerHTML = rich(ev.detail);
        break;
      case "error":
        say({ agent: "ERROR", title: ev.node + " failed", detail: ev.message, evidence: [] });
        break;
      case "strip.init":
        S.commits = ev.commits; buildStrip(ev.commits);
        pmsg("range set · " + (ev.commits.length - 1) + " candidate commits · last-good from " + esc(ev.source));
        break;
      case "strip.range":
        setRange(ev.lo, ev.hi, ev.remaining, ev.track); break;
      case "strip.probe":
        S.cells.forEach(function (c) { c.classList.remove("probing"); });
        if (S.cells[ev.index]) S.cells[ev.index].classList.add("probing");
        pmsg(esc(ev.label), true); break;
      case "strip.verdict":
        var c = S.cells[ev.index]; if (!c) break;
        c.classList.remove("probing", "out", "pass", "fail", "skip");
        c.classList.add(ev.verdict === "pass" ? "pass" : ev.verdict === "fail" ? "fail" : "skip");
        if (ev.repaired) c.classList.add("repaired");
        S.verdicts[ev.index] = ev.verdict;
        pmsg("c" + String(ev.index).padStart(2, "0") + " → " + ev.verdict.toUpperCase() + (ev.repaired ? " (dependency set repaired)" : "") +
          (ev.skip_kind ? " (" + ev.skip_kind + ")" : ""));
        break;
      case "strip.confirm":
        S.cells.forEach(function (c) { c.classList.remove("probing"); });
        pmsg("counterfactual: revert " + ev.indices.map(function (i) { return S.commits[i] ? S.commits[i].short : i; }).join(", ") +
          " at HEAD → " + (ev.verdict === "pass" ? '<span class="ok">PASS</span>' : '<span class="no">' + ev.verdict.toUpperCase() + "</span>"));
        break;
      case "strip.culprit":
        ev.indices.forEach(function (i) { if (S.cells[i]) { S.cells[i].classList.remove("probing", "fail"); S.cells[i].classList.add("culprit"); } });
        pmsg('<span style="color:var(--red);font-weight:600">culprit' + (ev.indices.length > 1 ? "s" : "") + ": " +
          ev.indices.map(function (i) { return S.commits[i].short + " — " + esc(trunc(S.commits[i].subject, 48)); }).join(" · ") + "</span>");
        break;
      case "tree.init":
        S.root = ev.root; ev.hypotheses.forEach(addHyp); drawTree();
        $("treetag").textContent = ev.hypotheses.length + " open"; break;
      case "tree.spawn":
        addHyp(ev.hypothesis); drawTree(); break;
      case "tree.update":
        ev.hypotheses.forEach(function (u) { var h = S.hyps[u.id]; if (h) { h.p = u.p; h.status = u.status; paintHyp(u.id); } });
        if (ev.entropy != null) $("entropy").textContent = "H = " + ev.entropy.toFixed(2) + " bits";
        break;
      case "tree.status":
        var hs = S.hyps[ev.hypothesis]; if (hs) { hs.status = ev.status; paintHyp(ev.hypothesis); } break;
      case "tree.active":
        S.active = {}; (ev.ids || []).forEach(function (i) { S.active[i] = true; });
        S.order.forEach(paintHyp); break;
      case "tree.evidence":
        var h = S.hyps[ev.hypothesis]; if (!h) break;
        h.evidence.push({ label: ev.label, tone: ev.tone, evidence: ev.evidence });
        var before = $("tree").getAttribute("viewBox");
        layout();
        if (h.evidence.length > MAXEV || $("tree").getAttribute("viewBox") !== before) drawTree();
        else drawEv(h, h.evidence[h.evidence.length - 1], h.evidence.length - 1, true);
        break;
      case "tree.tag":
        $("treetag").textContent = ev.text; break;
      case "orchestrator.decision":
        policy(ev); break;
      case "budget":
        budget(ev); break;
      case "conclusion":
        setStatus(ev.status.replace(/_/g, " "), ev.status === "resolved" || ev.status === "multi_cause" || ev.status === "dependency"
          || ev.status === "flaky" ? "done" : "bad");
        break;
      case "report":
        renderReport(ev.report); break;
      case "investigation.finished":
        S.finished = true; stopClock(ev.seconds);
        if (ev.status === "error") setStatus("error", "bad");
        $("start").disabled = false; loadPast();
        setTimeout(function () { $("report").scrollIntoView({ behavior: "smooth", block: "nearest" }); }, 300);
        break;
    }
  }

  function setStatus(text, cls) { $("status").textContent = text; $("status").className = "pill " + cls; }
  function stopClock(sec) { if (clockInt) clearInterval(clockInt); clockInt = null; if (sec != null) $("clock").textContent = fmtClock(sec); }

  function follow(id, speed) {
    if (es) es.close();
    S = fresh(); S.id = id;
    $("feed").innerHTML = ""; $("tree").innerHTML = ""; $("strip").innerHTML = ""; $("report").classList.remove("show");
    $("policy").innerHTML = '<div class="muted">no decision yet</div>'; $("entropy").textContent = "";
    pmsg("waiting for orchestrator…");
    if (clockInt) clearInterval(clockInt);
    clockInt = setInterval(function () { if (!S.finished) $("clock").textContent = fmtClock(speed ? (Date.now() - S.t0) / 1000 * speed : S.lastT); }, 250);
    es = new EventSource("/api/investigations/" + encodeURIComponent(id) + "/events" + (speed ? "?speed=" + speed : ""));
    es.onmessage = function (m) { try { handle(JSON.parse(m.data)); } catch (err) { console.error(err, m.data); } };
    es.onerror = function () { if (S.finished) { es.close(); } };
    $("launcher").open = false;
  }

  // ------------------------------------------------------------------ launcher
  var FIX = {};
  function loadFixtures() {
    api("/api/fixtures").then(function (list) {
      var sel = $("f-fixture");
      list.forEach(function (f) {
        FIX[f.name] = f;
        var o = document.createElement("option"); o.value = f.name; o.textContent = f.name + " — " + f.title; sel.appendChild(o);
      });
    });
  }
  function loadPast() {
    api("/api/investigations").then(function (list) {
      var sel = $("past"), keep = sel.value; sel.innerHTML = '<option value="">past investigations…</option>';
      list.forEach(function (x) {
        var o = document.createElement("option"); o.value = x.id;
        o.textContent = x.id + " · " + (x.label || x.test.split("::").pop()) + " · " + x.status + (x.live ? " (live)" : "");
        sel.appendChild(o);
      });
      sel.value = keep;
    });
  }
  $("f-fixture").addEventListener("change", function () {
    var f = FIX[this.value]; if (!f) return;
    $("f-repo").value = f.repo; $("f-test").value = f.test; $("f-lg").value = f.last_good.slice(0, 10);
    $("f-head").value = f.head.slice(0, 10); $("f-probes").value = (f.settings && f.settings.max_probes) || 16;
  });
  $("start").addEventListener("click", function () {
    var fx = $("f-fixture").value;
    var body = { fixture: fx || null, repo: $("f-repo").value.trim(), test: $("f-test").value.trim(),
      last_good: $("f-lg").value.trim() || null, head: $("f-head").value.trim() || "HEAD",
      max_probes: +$("f-probes").value || 16, flaky_runs: +$("f-runs").value || 10,
      offline: $("f-offline").checked, use_history: $("f-history").checked };
    if (fx && FIX[fx]) { body.last_good = FIX[fx].last_good; body.head = FIX[fx].head; }
    $("start").disabled = true; $("launchnote").textContent = "starting…";
    api("/api/investigations", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function (r) { $("launchnote").textContent = ""; follow(r.id, 0); })
      .catch(function (e) { $("start").disabled = false; $("launchnote").textContent = "failed: " + e.message; });
  });
  $("replay").addEventListener("click", function () {
    var id = $("past").value; if (!id) { $("launchnote").textContent = "pick an investigation to replay"; return; }
    follow(id, +$("speed").value);
  });

  api("/api/status").then(function (s) {
    $("llmpill").textContent = s.llm.enabled ? "Gemini · " + s.llm.models[0] : "LLM off (no key)";
    $("llmpill").className = "pill " + (s.llm.enabled ? "on" : "");
    $("sbpill").textContent = "sandbox " + s.sandbox + (s.docker ? " ✓" : " ✗");
    $("sbpill").className = "pill " + (s.docker ? "done" : "bad");
  }).catch(function () {});
  S = fresh();
  loadFixtures(); loadPast();
  var q = new URLSearchParams(location.search);
  if (q.get("replay")) follow(q.get("replay"), +(q.get("speed") || 3));
})();
