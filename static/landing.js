/* Landing-page motion. Everything here is decorative — the page reads fine
   with JS off, and all of it is skipped when the visitor prefers less motion. */

const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ------------------------------------------------- reveal on scroll ----- */

const revealables = document.querySelectorAll("[data-reveal]");
const revealAll = () => revealables.forEach((el) => el.classList.add("shown"));

if (reduceMotion || !("IntersectionObserver" in window)) {
  revealAll();
} else {
  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        const el = entry.target;
        const delay = Number(el.dataset.delay || 0);
        setTimeout(() => el.classList.add("shown"), delay);
        observer.unobserve(el);
      });
    },
    { threshold: 0.15, rootMargin: "0px 0px -60px 0px" }
  );
  revealables.forEach((el) => observer.observe(el));

  // Safety net: content must never stay invisible because the observer didn't
  // fire (odd viewports, prerendering, headless capture). Animation is a nicety;
  // being able to read the page is not.
  setTimeout(revealAll, 4000);
}

/* ------------------------------------- the hero chat types itself out --- */

const stage = document.getElementById("demo-stage");

const SCRIPT = [
  { who: "bot", text: "Hi! Welcome to Pizza Palace. How can I help?" },
  { who: "me", text: "how much is a large pepperoni" },
  { who: "bot", text: "A large Pepperoni pizza costs ₹459.", cite: "faq.txt" },
  { who: "me", text: "do you sell laptops" },
  { who: "bot", text: "I can only help with Pizza Palace questions." },
];

function bubble({ who, text, cite }) {
  const el = document.createElement("div");
  el.className = `b ${who}`;
  el.textContent = text;
  if (cite) {
    const tag = document.createElement("span");
    tag.className = "cite";
    tag.textContent = `📄 ${cite}`;
    el.appendChild(tag);
  }
  return el;
}

function typingBubble() {
  const el = document.createElement("div");
  el.className = "b bot typing-b";
  el.innerHTML = "<i></i><i></i><i></i>";
  return el;
}

const wait = (ms) => new Promise((res) => setTimeout(res, ms));

async function playConversation() {
  if (!stage) return;

  // With reduced motion, just show the finished conversation.
  if (reduceMotion) {
    SCRIPT.forEach((line) => stage.appendChild(bubble(line)));
    return;
  }

  while (true) {
    stage.innerHTML = "";
    await wait(600);

    for (const line of SCRIPT) {
      if (line.who === "bot") {
        const dots = typingBubble();
        stage.appendChild(dots);
        await wait(900);
        dots.remove();
      }
      stage.appendChild(bubble(line));
      await wait(line.who === "me" ? 700 : 1500);
    }

    await wait(3200);
  }
}

playConversation();
