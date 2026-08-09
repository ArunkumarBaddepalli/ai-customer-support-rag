/* Staged file picker.
 *
 * A native <input type="file" multiple> *replaces* its selection every time the
 * picker is opened, so choosing one file and then choosing a second silently
 * drops the first — and nothing on the page says so. Users read the input's
 * "1 file" label as a list of what will be uploaded, which it isn't.
 *
 * This keeps an accumulated list in a DataTransfer, writes it back to the
 * input (so a plain form POST still carries every file, no fetch involved),
 * and renders what is staged with a remove button per row. Turning JS off
 * leaves the input working exactly as before, one picker-load at a time.
 */
(function () {
  const input = document.querySelector('input[type="file"][data-staged]');
  if (!input || typeof DataTransfer === "undefined") return;

  const list = document.getElementById(input.dataset.staged);
  if (!list) return;

  const accepted = (input.accept || "").split(",").map(s => s.trim()).filter(Boolean);
  let staged = [];

  const keyOf = f => `${f.name}:${f.size}:${f.lastModified}`;

  function isAccepted(file) {
    if (!accepted.length) return true;
    return accepted.some(a => a.startsWith(".")
      ? file.name.toLowerCase().endsWith(a.toLowerCase())
      : file.type === a);
  }

  function humanSize(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 / 1024).toFixed(1)} MB`;
  }

  /* The input is the thing that actually gets submitted, so it stays the
     single source of truth — `staged` only exists to rebuild it. */
  function syncInput() {
    const dt = new DataTransfer();
    staged.forEach(f => dt.items.add(f));
    input.files = dt.files;
  }

  function render() {
    list.textContent = "";
    if (!staged.length) {
      list.hidden = true;
      return;
    }
    list.hidden = false;

    staged.forEach(file => {
      const li = document.createElement("li");
      if (!isAccepted(file)) li.className = "rejected";

      const name = document.createElement("span");
      name.className = "staged-name";
      name.textContent = file.name;

      const meta = document.createElement("span");
      meta.className = "staged-meta";
      meta.textContent = isAccepted(file)
        ? humanSize(file.size)
        : "not a .txt file";

      const remove = document.createElement("button");
      remove.type = "button";          // never submits the form
      remove.className = "link-danger";
      remove.textContent = "Remove";
      remove.setAttribute("aria-label", `Remove ${file.name}`);
      remove.addEventListener("click", () => {
        staged = staged.filter(f => keyOf(f) !== keyOf(file));
        syncInput();
        render();
      });

      const left = document.createElement("div");
      left.className = "staged-left";
      left.append(name, meta);
      li.append(left, remove);
      list.appendChild(li);
    });
  }

  input.addEventListener("change", () => {
    /* Merge rather than replace — that is the whole point. Same file picked
       twice is one entry, keyed on name+size+mtime so two genuinely different
       files that share a name still both survive. */
    const incoming = Array.from(input.files);
    const seen = new Set(staged.map(keyOf));
    incoming.forEach(f => {
      if (!seen.has(keyOf(f))) {
        staged.push(f);
        seen.add(keyOf(f));
      }
    });
    syncInput();
    render();
  });

  /* Dropping files onto the list is the gesture people try once they can see
     a list at all. */
  const dropZone = list.closest("form") || list;
  ["dragover", "drop"].forEach(evt => {
    dropZone.addEventListener(evt, e => {
      if (!e.dataTransfer || !e.dataTransfer.types.includes("Files")) return;
      e.preventDefault();
      dropZone.classList.toggle("dragging", evt === "dragover");
      if (evt !== "drop") return;
      const seen = new Set(staged.map(keyOf));
      Array.from(e.dataTransfer.files).forEach(f => {
        if (!seen.has(keyOf(f))) {
          staged.push(f);
          seen.add(keyOf(f));
        }
      });
      syncInput();
      render();
    });
  });
  dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragging"));
})();
