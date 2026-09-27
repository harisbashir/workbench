/* Workbench front-end behaviour. Plain JavaScript, no libraries. */
(function () {
  "use strict";

  function csrf() {
    const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function ready(fn) {
    if (document.readyState !== "loading") fn(); else document.addEventListener("DOMContentLoaded", fn);
  }

  ready(function () {
    // Mobile menu
    const toggle = document.querySelector("[data-menu-toggle]");
    const sidebar = document.querySelector(".sidebar");
    if (toggle && sidebar) toggle.addEventListener("click", () => sidebar.classList.toggle("open"));

    // Confirm before destructive actions: <form data-confirm="Are you sure?">
    document.querySelectorAll("form[data-confirm]").forEach((f) => {
      f.addEventListener("submit", (e) => { if (!window.confirm(f.dataset.confirm)) e.preventDefault(); });
    });

    // Auto-submit filter forms when a select changes
    document.querySelectorAll("[data-autosubmit]").forEach((el) => {
      el.addEventListener("change", () => el.form.submit());
    });

    // Copy-to-clipboard buttons: <button data-copy="#input-id">
    document.querySelectorAll("[data-copy]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const target = document.querySelector(btn.dataset.copy);
        if (!target) return;
        target.select();
        const text = target.value;
        (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).catch(() => document.execCommand("copy"));
        const old = btn.textContent;
        btn.textContent = "Copied";
        setTimeout(() => (btn.textContent = old), 1500);
      });
    });

    // Show the chosen file name on file inputs
    document.querySelectorAll("input[type=file][data-autoupload]").forEach((inp) => {
      inp.addEventListener("change", () => { if (inp.files.length) inp.form.submit(); });
    });

    // "Compare with…" dropdown on BOM pages
    document.querySelectorAll("[data-compare-select]").forEach((sel) => {
      sel.addEventListener("change", () => { if (sel.value) window.location = sel.value; });
    });

    // Print buttons
    document.querySelectorAll("[data-print]").forEach((b) => b.addEventListener("click", () => window.print()));

    initShowWhen();
    initBuildSteps();
    initProviderHints();
    initStatusPoll();
    initDropzone();
    initTeamClock();
    initBoard();
    initChat();
    pollUnread();
  });

  // ------------------------------------------- Show parts of a form -------
  // <div data-show-when="kind:s3,other"> is shown only while the form field
  // named "kind" has one of those values.
  function fieldValue(form, name) {
    const els = form.querySelectorAll('[name="' + name + '"]');
    for (const el of els) {
      if (el.type === "radio" || el.type === "checkbox") { if (el.checked) return el.value; }
      else return el.value;
    }
    return "";
  }
  function initShowWhen() {
    document.querySelectorAll("form").forEach((form) => {
      const parts = form.querySelectorAll("[data-show-when]");
      if (!parts.length) return;
      const update = () => parts.forEach((p) => {
        const [name, values] = p.dataset.showWhen.split(":");
        p.hidden = values.split(",").indexOf(fieldValue(form, name)) === -1;
      });
      form.addEventListener("change", update);
      update();
    });
  }

  // ------------------------------------------------ Build finishing ------
  // +1 / +5 / All buttons update in place; hovering a step highlights its parts on the board.
  function initBuildSteps() {
    document.querySelectorAll("form[data-step-form]").forEach((form) => {
      form.addEventListener("submit", (e) => {
        const btn = e.submitter;
        if (!btn) return;
        e.preventDefault();
        const fd = new FormData(form);
        fd.append(btn.name, btn.value);
        form.querySelectorAll("button").forEach((b) => (b.disabled = true));
        fetch(form.action, { method: "POST", body: fd, credentials: "same-origin",
          headers: { "X-CSRFToken": csrf(), "X-Requested-With": "fetch" } })
          .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
          .then((d) => {
            const row = form.closest("[data-step-row]");
            row.querySelector("[data-step-done]").textContent = d.done;
            const bar = row.querySelector("[data-step-bar]");
            bar.className = ""; bar.style.width = d.percent + "%";
            row.classList.toggle("complete", d.complete);
            if (d.all_done) {
              const fin = document.querySelector("[data-finish-button]");
              if (fin) fin.classList.add("pulse");
              toast("Every step is done on every board.");
            }
          })
          .catch(() => toast("Couldn't save — check your connection.", true))
          .finally(() => form.querySelectorAll("button").forEach((b) => (b.disabled = false)));
      });
    });
    document.querySelectorAll("[data-step-row]").forEach((row) => {
      const id = row.dataset.stepRow;
      const marks = document.querySelectorAll('.board-markers [data-marker-step="' + id + '"]');
      if (!marks.length) return;
      row.addEventListener("mouseenter", () => {
        document.querySelectorAll(".board-markers .marker").forEach((m) => m.classList.add("dim"));
        marks.forEach((m) => { m.classList.remove("dim"); m.classList.add("hot"); });
      });
      row.addEventListener("mouseleave", () => {
        document.querySelectorAll(".board-markers .marker").forEach((m) => m.classList.remove("dim", "hot"));
      });
    });
  }

  // ------------------------------------------ Storage provider hints -------
  function initProviderHints() {
    const select = document.querySelector("[data-provider-select]");
    const dataEl = document.getElementById("provider-info");
    if (!select || !dataEl) return;
    const info = JSON.parse(dataEl.textContent);
    const form = select.form;
    const region = form.querySelector('[name="region"]');
    const endpoint = form.querySelector('[name="endpoint"]');
    const help = form.querySelector("[data-provider-help]");
    const wrap = (name) => form.querySelector('[data-provider-field="' + name + '"]');
    function update() {
      const p = info[select.value] || {};
      const tmpl = p.endpoint || "";
      wrap("region").hidden = !!p.region;               // fixed region ("auto")
      wrap("endpoint").hidden = select.value === "aws";
      region.placeholder = p.region_hint || "";
      if (tmpl.indexOf("{region}") !== -1) {
        endpoint.placeholder = tmpl.replace("{region}", region.value || "REGION");
      } else {
        endpoint.placeholder = tmpl || "https://s3.example.com";
      }
      help.textContent = p.help || "";
    }
    select.addEventListener("change", () => { endpoint.value = ""; update(); });
    region.addEventListener("input", update);
    update();
  }

  // ------------------------------------------------ Background jobs --------
  // <div data-poll-status="/url.json"> polls until the job finishes, then reloads.
  function initStatusPoll() {
    const box = document.querySelector("[data-poll-status]");
    if (!box) return;
    const bar = box.querySelector("[data-progress]");
    const text = box.querySelector("[data-progress-text]");
    function poll() {
      fetch(box.dataset.pollStatus, { credentials: "same-origin" }).then((r) => r.json()).then((s) => {
        if (bar) { bar.className = ""; bar.style.width = (s.percent || 0) + "%"; }
        if (text && s.total) text.textContent = s.done + " of " + s.total + " files" + (s.failed ? " · " + s.failed + " problems" : "");
        if (s.status === "done" || s.status === "failed" || s.status === "none") window.location.reload();
        else setTimeout(poll, 2000);
      }).catch(() => setTimeout(poll, 5000));
    }
    setTimeout(poll, 1500);
  }

  // -------------------------------------------------------------- Uploads --
  function initDropzone() {
    const zone = document.querySelector("[data-dropzone]");
    if (!zone) return;
    const bar = zone.querySelector("[data-bar]");
    const status = zone.querySelector("[data-status]");
    const box = zone.querySelector(".upload-progress");
    ["dragenter", "dragover"].forEach((ev) => document.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("over"); }));
    ["dragleave", "drop"].forEach((ev) => document.addEventListener(ev, (e) => {
      if (ev === "dragleave" && e.relatedTarget) return;
      e.preventDefault(); zone.classList.remove("over");
    }));
    document.addEventListener("drop", (e) => {
      const files = e.dataTransfer && e.dataTransfer.files;
      if (files && files.length) send(files);
    });
    // Replace the plain form submit with a progress-reporting upload
    const input = zone.querySelector("input[type=file]");
    if (input) {
      input.removeAttribute("data-autoupload");
      input.addEventListener("change", () => { if (input.files.length) send(input.files); });
    }
    function send(files) {
      const fd = new FormData();
      for (const f of files) fd.append("files", f);
      zone.querySelectorAll("[data-upload-extra]").forEach((el) => { if (el.value) fd.append(el.name, el.value); });
      const xhr = new XMLHttpRequest();
      xhr.open("POST", zone.dataset.uploadUrl);
      xhr.setRequestHeader("X-CSRFToken", csrf());
      xhr.setRequestHeader("X-Requested-With", "fetch");
      box.classList.remove("hidden");
      status.textContent = "Uploading " + files.length + " file" + (files.length > 1 ? "s" : "") + "…";
      xhr.upload.onprogress = (e) => {
        if (!e.lengthComputable) return;
        const pct = Math.round((e.loaded / e.total) * 20) * 5;
        bar.className = "w-" + pct;
        status.textContent = "Uploading… " + pct + "%";
      };
      xhr.onload = () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText); } catch (err) { /* ignore */ }
        if (xhr.status >= 400 || (data.errors && data.errors.length)) {
          status.textContent = (data.errors || ["Upload failed (" + xhr.status + ")."]).join(" ");
          status.classList.add("overdue");
          if (data.uploaded && data.uploaded.length) setTimeout(() => location.reload(), 2500);
        } else {
          status.textContent = "Done.";
          location.reload();
        }
      };
      xhr.onerror = () => { status.textContent = "Upload failed. Check your connection and try again."; };
      xhr.send(fd);
    }
  }

  // ----------------------------------------------------------- Team clock --
  function initTeamClock() {
    const rows = document.querySelectorAll(".clock-row[data-tz]");
    if (!rows.length) return;
    function tick() {
      rows.forEach((row) => {
        try {
          const now = new Date();
          const time = new Intl.DateTimeFormat([], { timeZone: row.dataset.tz, hour: "2-digit", minute: "2-digit", weekday: "short" }).format(now);
          const hour = parseInt(new Intl.DateTimeFormat("en-GB", { timeZone: row.dataset.tz, hour: "2-digit", hour12: false }).format(now), 10);
          row.querySelector(".clock-time").textContent = time;
          row.classList.toggle("working", hour >= 9 && hour < 18);
        } catch (e) { /* unknown zone */ }
      });
    }
    tick();
    setInterval(tick, 30000);
  }

  // ---------------------------------------------------------------- Board --
  function initBoard() {
    const board = document.querySelector("[data-board]");
    if (!board || board.dataset.editable !== "1") return;
    let dragged = null;

    board.querySelectorAll(".tcard").forEach((card) => {
      card.setAttribute("draggable", "true");
      card.addEventListener("dragstart", (e) => {
        dragged = card;
        card.classList.add("dragging");
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", card.dataset.moveUrl);
      });
      card.addEventListener("dragend", () => { card.classList.remove("dragging"); dragged = null; });
    });

    board.querySelectorAll(".column").forEach((col) => {
      col.addEventListener("dragover", (e) => { e.preventDefault(); col.classList.add("drop-target"); });
      col.addEventListener("dragleave", () => col.classList.remove("drop-target"));
      col.addEventListener("drop", (e) => {
        e.preventDefault();
        col.classList.remove("drop-target");
        if (!dragged) return;
        const card = dragged;
        const from = card.parentElement;
        const status = col.dataset.status;
        if (card.closest(".column") === col) return;
        col.querySelector(".cards").prepend(card);
        updateCounts();
        fetch(card.dataset.moveUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
          body: JSON.stringify({ status: status }),
          credentials: "same-origin",
        }).then((r) => {
          if (!r.ok) throw new Error();
          toast("Moved to " + col.dataset.label);
        }).catch(() => {
          from.prepend(card);
          updateCounts();
          toast("Couldn't move the task. Check your connection and try again.", true);
        });
      });
    });

    function updateCounts() {
      board.querySelectorAll(".column").forEach((c) => {
        const n = c.querySelectorAll(".tcard").length;
        const el = c.querySelector(".n");
        if (el) el.textContent = n;
      });
    }
  }

  function toast(text, bad) {
    let t = document.getElementById("toast");
    if (!t) {
      t = document.createElement("div");
      t.id = "toast";
      t.className = "toast";
      document.body.appendChild(t);
    }
    t.textContent = text;
    t.className = "toast show" + (bad ? " bad" : "");
    clearTimeout(t._h);
    t._h = setTimeout(() => (t.className = "toast"), 2600);
  }

  // ------------------------------------------------------------------ Chat --
  function initChat() {
    const root = document.querySelector("[data-chat]");
    if (!root) return;
    const log = root.querySelector(".chat-log");
    const form = root.querySelector("form.compose");
    const input = form ? form.querySelector("textarea") : null;
    const pollUrl = root.dataset.pollUrl;
    const initial = JSON.parse(document.getElementById("initial-messages").textContent);
    let lastId = 0, firstId = null, lastAuthor = null, lastDay = null, since = new Date().toISOString();
    let loadingOlder = false, noMoreOlder = initial.length < 60;

    function el(tag, cls, html) {
      const e = document.createElement(tag);
      if (cls) e.className = cls;
      if (html !== undefined) e.innerHTML = html;
      return e;
    }
    function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

    function build(m, prevAuthor, prevDay) {
      const frag = document.createDocumentFragment();
      if (m.day !== prevDay) frag.appendChild(el("div", "day-sep", esc(m.day)));
      const cont = prevAuthor === m.author + m.kind && m.day === prevDay;
      const row = el("div", "msg" + (cont ? " cont" : "") + (m.kind !== "user" ? " bot" : ""));
      row.dataset.id = m.id;
      const av = el("span", "avatar sm" + (m.kind === "github" ? " gh" : m.kind === "system" ? " wb" : ""), esc(m.initials));
      const body = el("div", "m-body");
      const head = el("div", "m-head");
      head.innerHTML = '<span class="m-name">' + esc(m.author) + '</span><span class="m-time">' + esc(m.time) + (m.edited && !m.deleted ? " · edited" : "") + "</span>";
      if (m.mine && !m.deleted) {
        const tools = el("span", "m-tools");
        tools.innerHTML = '<button class="link-btn" data-act="edit">Edit</button> · <button class="link-btn" data-act="delete">Delete</button>';
        head.appendChild(tools);
      }
      const text = el("div", "m-text", m.html);
      if (m.url && /^(https?:\/\/|\/)/.test(m.url)) text.innerHTML += ' <a href="' + encodeURI(m.url) + '" target="_blank" rel="noopener noreferrer">Open ↗</a>';
      body.appendChild(head);
      body.appendChild(text);
      row.appendChild(av);
      row.appendChild(body);
      frag.appendChild(row);
      return frag;
    }

    function append(list) {
      const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
      list.forEach((m) => {
        if (m.id <= lastId) return;
        log.appendChild(build(m, lastAuthor, lastDay));
        lastAuthor = m.author + m.kind; lastDay = m.day; lastId = m.id;
        if (firstId === null) firstId = m.id;
      });
      if (atBottom || list.some((m) => m.mine)) log.scrollTop = log.scrollHeight;
    }

    function prependOlder(list) {
      if (!list.length) { noMoreOlder = true; return; }
      const oldHeight = log.scrollHeight;
      const frag = document.createDocumentFragment();
      let pa = null, pd = null;
      list.forEach((m) => { frag.appendChild(build(m, pa, pd)); pa = m.author + m.kind; pd = m.day; });
      // Remove a now-duplicated day separator at the old top
      const firstSep = log.querySelector(".day-sep");
      if (firstSep && firstSep.textContent === pd) firstSep.remove();
      log.prepend(frag);
      firstId = list[0].id;
      log.scrollTop = log.scrollHeight - oldHeight;
    }

    function replace(m) {
      const row = log.querySelector('.msg[data-id="' + m.id + '"]');
      if (!row) return;
      row.querySelector(".m-text").innerHTML = m.html;
      if (m.deleted) { const t = row.querySelector(".m-tools"); if (t) t.remove(); }
    }

    if (!initial.length) {
      log.appendChild(el("div", "empty", "<h3>No messages yet</h3><p>Say hello, share a design question, or paste a task key like <code>PWR-12</code> to link it.</p>"));
    }
    append(initial);
    log.scrollTop = log.scrollHeight;

    function poll() {
      fetch(pollUrl + "?after=" + lastId + "&since=" + encodeURIComponent(since), { credentials: "same-origin" })
        .then((r) => r.json())
        .then((data) => {
          since = data.now;
          if (data.messages.length) { const e = log.querySelector(".empty"); if (e) e.remove(); }
          append(data.messages);
          (data.changed || []).forEach(replace);
          setBadge(data.unread_total);
        })
        .catch(() => {})
        .finally(() => setTimeout(poll, document.hidden ? 15000 : 3000));
    }
    setTimeout(poll, 3000);

    log.addEventListener("scroll", () => {
      if (log.scrollTop < 40 && !loadingOlder && !noMoreOlder && firstId) {
        loadingOlder = true;
        fetch(pollUrl + "?before=" + firstId, { credentials: "same-origin" })
          .then((r) => r.json()).then((d) => prependOlder(d.messages))
          .finally(() => (loadingOlder = false));
      }
    });

    log.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-act]");
      if (!btn) return;
      const row = btn.closest(".msg");
      const id = row.dataset.id;
      const fd = new FormData();
      if (btn.dataset.act === "delete") {
        if (!window.confirm("Delete this message?")) return;
        fd.append("action", "delete");
      } else {
        const current = row.querySelector(".m-text").innerText;
        const next = window.prompt("Edit message", current);
        if (next === null || !next.trim()) return;
        fd.append("body", next);
      }
      fetch(root.dataset.editBase.replace("0", id), { method: "POST", body: fd, headers: { "X-CSRFToken": csrf() }, credentials: "same-origin" })
        .then((r) => r.json()).then((d) => d.ok && replace(d.message));
    });

    if (form && input) {
      function autosize() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 200) + "px"; }
      input.addEventListener("input", autosize);
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
      });
      form.addEventListener("submit", (e) => {
        e.preventDefault();
        const body = input.value.trim();
        if (!body) return;
        const fd = new FormData(form);
        input.value = ""; autosize();
        fetch(form.action, { method: "POST", body: fd, headers: { "X-CSRFToken": csrf(), "X-Requested-With": "fetch" }, credentials: "same-origin" })
          .then((r) => r.json())
          .then((d) => {
            if (!d.ok) throw new Error(d.error);
            const e2 = log.querySelector(".empty"); if (e2) e2.remove();
            append([d.message]);
          })
          .catch(() => { input.value = body; toast("Message not sent. Check your connection and press Enter again.", true); });
      });
      input.focus();
    }
  }

  function setBadge(n) {
    const b = document.querySelector("[data-chat-badge]");
    if (!b) return;
    if (n > 0) { b.textContent = n > 99 ? "99+" : n; b.classList.remove("hidden"); }
    else b.classList.add("hidden");
  }

  function pollUnread() {
    const b = document.querySelector("[data-chat-badge]");
    if (!b || document.querySelector("[data-chat]")) return;
    setInterval(() => {
      if (document.hidden) return;
      fetch(b.dataset.url, { credentials: "same-origin" }).then((r) => r.json()).then((d) => setBadge(d.unread_total)).catch(() => {});
    }, 30000);
  }
})();
