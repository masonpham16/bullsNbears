const elPrice = document.getElementById("price");
const elChange = document.getElementById("change");
const elChangePct = document.getElementById("changePct");
const elNote = document.getElementById("note");
const elTs = document.getElementById("ts");

function fmt(n, digits = 2) {
  if (n === null || n === undefined) return "—";
  if (typeof n !== "number") return String(n);
  return n.toFixed(digits);
}

function setSigned(el, n, digits = 2) {
  if (n === null || n === undefined) {
    el.textContent = "—";
    el.className = "value";
    return;
  }
  const sign = n > 0 ? "+" : "";
  el.textContent = `${sign}${fmt(n, digits)}`;
  el.className = "value " + (n > 0 ? "up" : (n < 0 ? "down" : "flat"));
}

const socket = io();

socket.on("tick", (data) => {
  elPrice.textContent = data.price != null ? fmt(data.price, 2) : "—";
  setSigned(elChange, data.change, 2);
  setSigned(elChangePct, data.change_pct, 2);
  elChangePct.textContent = elChangePct.textContent === "—" ? "—" : `${elChangePct.textContent}%`;

  elNote.textContent = data.note ?? "—";

  if (data.ts) {
    const dt = new Date(data.ts * 1000);
    elTs.textContent = dt.toLocaleString();
  } else {
    elTs.textContent = "—";
  }
});
