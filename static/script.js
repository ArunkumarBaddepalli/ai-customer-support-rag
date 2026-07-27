const form = document.getElementById("chat-form");
const input = document.getElementById("question-input");
const messages = document.getElementById("messages");

function addMessage(text, role, sources = []) {
  const el = document.createElement("div");
  el.className = `message ${role}`;
  el.textContent = text;

  if (sources.length) {
    const src = document.createElement("span");
    src.className = "source";
    src.textContent = `from: ${sources.join(", ")}`;
    el.appendChild(src);
  }

  messages.appendChild(el);
  messages.scrollTop = messages.scrollHeight;
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const question = input.value.trim();
  if (!question) return;

  addMessage(question, "user");
  input.value = "";
  input.disabled = true;

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const data = await res.json();

    if (res.ok) {
      addMessage(data.answer, "bot", data.sources || []);
    } else {
      addMessage(data.error || "Something went wrong.", "bot");
    }
  } catch (err) {
    addMessage("Could not reach the server. Is app.py running?", "bot");
  } finally {
    input.disabled = false;
    input.focus();
  }
});
