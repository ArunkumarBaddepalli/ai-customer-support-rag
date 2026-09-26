const form = document.getElementById("chat-form");
const input = document.getElementById("question-input");
const messages = document.getElementById("messages");
const chips = document.getElementById("chips");

function addMessage(text, role, sources = []) {
  if (chips) chips.remove();  // openers stop being useful once talking

  const row = document.createElement("div");
  row.className = `row ${role}`;

  const avatar = document.createElement("div");
  avatar.className = "avatar";
  if (role === "user") {
    avatar.textContent = "🧑";
  } else {
    avatar.classList.add("bot-avatar");
    avatar.appendChild(botAvatar());
  }

  const bubble = document.createElement("div");
  bubble.className = "message";
  renderAnswer(bubble, text);

  if (sources.length) {
    const src = document.createElement("span");
    src.className = "source";
    src.textContent = `from: ${sources.join(", ")}`;
    bubble.appendChild(src);
  }

  if (role === "user") {
    row.appendChild(bubble);
    row.appendChild(avatar);
  } else {
    row.appendChild(avatar);
    row.appendChild(bubble);
  }

  messages.appendChild(row);
  messages.scrollTop = messages.scrollHeight;
  return row;
}

/* The model writes plain text with line breaks and "- " bullets. Setting it
   as textContent collapsed all of that into one run-on paragraph, so a list
   of prices or opening hours arrived as a single line. Built from DOM nodes,
   never innerHTML: the text is the model's, not ours to trust as markup. */
function renderAnswer(bubble, text) {
  const lines = String(text).split(/\r?\n/);
  let list = null;
  lines.forEach((line) => {
    const item = line.match(/^\s*(?:[-*\u2022]|\d+[.)])\s+(.*)$/);
    if (item) {
      if (!list) { list = document.createElement("ul"); bubble.appendChild(list); }
      const li = document.createElement("li");
      li.textContent = item[1];
      list.appendChild(li);
      return;
    }
    list = null;
    if (!line.trim()) return;
    const p = document.createElement("p");
    p.textContent = line;
    bubble.appendChild(p);
  });
  if (!bubble.childNodes.length) bubble.textContent = text;
}

/* The business's own logo or initial, same as the header — a generic robot
   next to a branded header looked like two different products. */
function botAvatar() {
  const tpl = document.getElementById("bot-avatar");
  if (tpl && tpl.content.firstElementChild) return tpl.content.firstElementChild.cloneNode(true);
  const fallback = document.createElement("span");
  fallback.textContent = "🤖";
  return fallback;
}

function showTyping() {
  const row = document.createElement("div");
  row.className = "row bot typing";
  const avatar = document.createElement("div");
  avatar.className = "avatar bot-avatar";
  avatar.appendChild(botAvatar());
  const message = document.createElement("div");
  message.className = "message";
  message.innerHTML = '<span class="dot"></span><span class="dot"></span><span class="dot"></span>';
  row.append(avatar, message);
  messages.appendChild(row);
  messages.scrollTop = messages.scrollHeight;
  return row;
}

if (chips) {
  chips.addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    input.value = chip.textContent.trim();
    form.requestSubmit();
  });
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const question = input.value.trim();
  if (!question) return;

  addMessage(question, "user");
  input.value = "";
  input.disabled = true;

  const typingRow = showTyping();

  try {
    const res = await fetch(window.CHAT_ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const data = await res.json();

    typingRow.remove();

    if (res.ok) {
      addMessage(data.answer, "bot", data.sources || []);
    } else {
      addMessage(data.error || "Something went wrong.", "bot");
    }
  } catch (err) {
    typingRow.remove();
    addMessage("Couldn't reach the assistant just now — please try again in a moment.", "bot");
  } finally {
    input.disabled = false;
    input.focus();
  }
});
