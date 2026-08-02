const form = document.getElementById("chat-form");
const input = document.getElementById("question-input");
const messages = document.getElementById("messages");

function clearEmptyState() {
  const empty = messages.querySelector(".empty-state");
  if (empty) empty.remove();
}

function addMessage(text, role, sources = []) {
  clearEmptyState();

  const row = document.createElement("div");
  row.className = `row ${role}`;

  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = role === "user" ? "🧑" : "🤖";

  const bubble = document.createElement("div");
  bubble.className = "message";
  bubble.textContent = text;

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

function showTyping() {
  const row = document.createElement("div");
  row.className = "row bot typing";
  row.innerHTML = `
    <div class="avatar">🤖</div>
    <div class="message"><span class="dot"></span><span class="dot"></span><span class="dot"></span></div>
  `;
  messages.appendChild(row);
  messages.scrollTop = messages.scrollHeight;
  return row;
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
    const res = await fetch("/api/chat", {
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
    addMessage("Could not reach the server. Is app.py running?", "bot");
  } finally {
    input.disabled = false;
    input.focus();
  }
});
