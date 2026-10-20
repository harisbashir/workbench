// Sources: check in a folder with its folder structure.
// Browsers only send file names in a normal form post, so when a folder is chosen we send
// each file's path inside the folder as a "paths" field, in the same order as the files.
(function () {
  "use strict";
  const form = document.querySelector("[data-checkin-form]");
  if (!form) return;
  const folder = form.querySelector("[data-folder-input]");
  // Only offered when this script runs: without it the browser would drop the folder structure.
  const folderField = form.querySelector("[data-folder-field]");
  if (folderField) folderField.classList.remove("hidden");
  const box = form.querySelector("[data-progress]");
  const bar = form.querySelector("[data-bar]");
  const status = form.querySelector("[data-status]");

  function csrf() {
    const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    const field = form.querySelector("input[name=csrfmiddlewaretoken]");
    return m ? decodeURIComponent(m[1]) : (field ? field.value : "");
  }

  const button = form.querySelector("button");
  let sending = false;

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (sending) return;  // a double click must not upload twice
    const fd = new FormData();
    form.querySelectorAll("input:not([type=file])").forEach((el) => {
      if ((el.type === "radio" || el.type === "checkbox") && !el.checked) return;
      fd.append(el.name, el.value);
    });
    let count = 0;
    form.querySelectorAll("input[type=file]").forEach((input) => {
      for (const f of input.files) {
        fd.append("files", f);
        fd.append("paths", input === folder && f.webkitRelativePath ? f.webkitRelativePath : f.name);
        count += 1;
      }
    });
    if (!count) { form.submit(); return; }
    sending = true;
    if (button) button.disabled = true;
    const done = () => { sending = false; if (button) button.disabled = false; };
    const xhr = new XMLHttpRequest();
    xhr.open("POST", form.action || window.location.href);
    xhr.setRequestHeader("X-CSRFToken", csrf());
    xhr.setRequestHeader("X-Requested-With", "fetch");
    box.classList.remove("hidden");
    status.textContent = "Uploading " + count + " file" + (count > 1 ? "s" : "") + "…";
    xhr.upload.onprogress = (ev) => {
      if (!ev.lengthComputable) return;
      const pct = Math.round((ev.loaded / ev.total) * 20) * 5;
      bar.className = "w-" + pct;
      status.textContent = "Uploading… " + pct + "%";
    };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch (err) { /* not JSON */ }
      if (xhr.status < 400 && data.redirect) { window.location.href = data.redirect; return; }
      done();
      status.textContent = xhr.status === 413 || xhr.status === 400
        ? "That's too many files or too large for one upload. Try a zip instead."
        : "The upload didn't work. Please try again.";
    };
    xhr.onerror = () => { done(); status.textContent = "The upload didn't work. Check your connection and try again."; };
    xhr.send(fd);
  });
})();
