// static/app.js
// Two-mode dots:
// - Market CLOSED: calm ambient dots (neutral color, constant count)
// - Market OPEN: dots scale with daily volume (shares) using ONE global scale (1..100),
//                and color reflects day change (up/down/flat).
// Dots always spawn from the center of the canvas and migrate into each card's dot-region.

const socket = io();

socket.on("connect", () => console.log("✅ connected", socket.id));
socket.on("disconnect", (r) => console.log("❌ disconnected", r));
socket.onAny((event, ...args) => console.log("📨", event, args));
socket.on("connect_error", (err) => console.log("⚠️ connect_error", err.message));

/* ---------------------- formatting helpers ---------------------- */
function fmt(n, digits = 2) {
	if (n === null || n === undefined) return "—";
	if (typeof n !== "number") return String(n);
	return n.toFixed(digits);
}

function setSigned(el, n, digits = 2) {
	if (!el) return;
	if (n === null || n === undefined) {
		el.textContent = "—";
		el.className = "value";
		return;
	}
	const sign = n > 0 ? "+" : "";
	el.textContent = `${sign}${fmt(n, digits)}`;
	el.className = "value " + (n > 0 ? "up" : (n < 0 ? "down" : "flat"));
}

/* ---------------------- MARKET STATUS + MODE ---------------------- */
const banner = document.getElementById("marketBanner");
let marketOpen = false;

// last-known per symbol from socket
const symbolState = {}; // symbol -> { volume: number|null, avgVolume: number|null, change: number|null }

function isMarketOpenNY() {
	const now = new Date();
	const parts = new Intl.DateTimeFormat("en-US", {
		timeZone: "America/New_York",
		weekday: "short",
		hour: "2-digit",
		minute: "2-digit",
		hour12: false
	}).formatToParts(now);

	const get = (type) => parts.find(p => p.type === type)?.value;

	const weekday = get("weekday");
	const hour = Number(get("hour"));
	const minute = Number(get("minute"));

	// weekend
	if (weekday === "Sat" || weekday === "Sun") return false;

	// minutes since midnight ET
	const t = hour * 60 + minute;

	// regular session 9:30–16:00 ET
	return t >= (9 * 60 + 30) && t < (16 * 60);
}

// forward decls (defined later)
	let syncDotsForSymbol, targetDotsForSymbol, seedAmbientDots;

function updateMarketMode() {
	const openNow = isMarketOpenNY();
	marketOpen = openNow;

	if (banner) banner.classList.toggle("hidden", openNow);

	// Resync dots for all known symbols on mode change/refresh
	const symbols = Object.keys(symbolState);
	for (const sym of symbols) {
		if (marketOpen) {
			syncDotsForSymbol(sym, targetDotsForSymbol(sym));
		} else {
			syncDotsForSymbol(sym, AMBIENT_DOTS_CLOSED);
		}
	}
}

/* ---------------------- DOT SYSTEM (in per-card dot-region) ---------------------- */
const wrap = document.getElementById("gridWrap");
const canvas = document.getElementById("dots");
const ctx = canvas ? canvas.getContext("2d") : null;

let W = 0, H = 0;
let targets = {}; // symbol -> {x,y,rRegion}
let dots = [];

// CLOSED mode
const AMBIENT_DOTS_CLOSED = 18; // calm amount of dots shown per symbol while closed

// OPEN mode: map each symbol to dots by blending:
// - relative activity vs that symbol's own normal volume
// - cross-symbol activity rank (which symbol is most active now)
const MIN_OPEN_DOTS = 2;
const MAX_OPEN_DOTS = 100;

// dot geometry
const DOT_R = 2.2;
const COLLISION_R = 3.0;

// orbit feel (inside region)
const RING_STRENGTH = 0.045;
const SWIRL_STRENGTH = 0.060;

// motion
const DAMP = 0.92;
const MAX_SPEED = 6.0;

// collision
const MIN_DIST = COLLISION_R * 2;

// targets from DOM
function recomputeTargets() {
	if (!wrap) return;
	targets = {};
	const wrapRect = wrap.getBoundingClientRect();

	wrap.querySelectorAll(".stock-card").forEach(card => {
		const symbol = card.id.replace("card-", "");
		const region = document.getElementById(`region-${symbol}`);
		if (!region) return;

		const rr = region.getBoundingClientRect();
		const left = rr.left - wrapRect.left;
		const top = rr.top - wrapRect.top;
		const w = rr.width;
		const h = rr.height;

		// treat dot-region as a circle container
		const rRegion = Math.min(w, h) * 0.48;

		targets[symbol] = {
			x: left + w / 2,
			y: top + h / 2,
			rRegion
		};
	});
}

function resizeCanvas() {
	if (!wrap || !canvas || !ctx) return;

	const r = wrap.getBoundingClientRect();
	W = Math.floor(r.width);
	H = Math.floor(r.height);

	canvas.width = W * devicePixelRatio;
	canvas.height = H * devicePixelRatio;
	canvas.style.width = W + "px";
	canvas.style.height = H + "px";

	ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
	recomputeTargets();
}

function centerSpawn() {
	return { x: W / 2, y: H / 2 };
}

function maxVolumeAcrossSymbols() {
	const vals = Object.values(symbolState)
		.map(s => Number(s?.volume))
		.filter(v => Number.isFinite(v) && v > 0);
	return vals.length ? Math.max(...vals) : 0;
}

targetDotsForSymbol = function targetDotsForSymbol(symbol) {
	const s = symbolState[symbol] || {};
	const vol = Number(s.volume);
	const avg = Number(s.avgVolume);
	if (!Number.isFinite(vol) || vol <= 0) return MIN_OPEN_DOTS;

	// Relative activity to "normal" for this symbol (0.1x..10x mapped to 0..1).
	const rel = Number.isFinite(avg) && avg > 0 ? vol / avg : 1;
	const relClamped = Math.max(0.1, Math.min(10, rel));
	const relScore = (Math.log10(relClamped) + 1) / 2;

	// Cross-symbol rank so you can compare which stock is getting more activity now.
	const maxVol = maxVolumeAcrossSymbols();
	const rankRaw = maxVol > 0 ? Math.max(0, Math.min(1, vol / maxVol)) : 0.5;
	// Exponent >1 increases contrast: low volume gets much fewer dots.
	const rankScore = Math.pow(rankRaw, 1.8);

	// Emphasize cross-symbol volume differences.
	const score = 0.2 * relScore + 0.8 * rankScore;
	const dots = Math.round(MIN_OPEN_DOTS + score * (MAX_OPEN_DOTS - MIN_OPEN_DOTS));
	return Math.max(MIN_OPEN_DOTS, Math.min(MAX_OPEN_DOTS, dots));
};

syncDotsForSymbol = function syncDotsForSymbol(symbol, desiredCount) {
	const current = dots.filter(d => d.symbol === symbol).length;
	const diff = desiredCount - current;

	if (diff > 0) {
		const spawn = centerSpawn();
		const t = targets[symbol];
		const maxR = t ? Math.max(10, t.rRegion - 6) : 18;

		for (let i = 0; i < diff; i++) {
			dots.push({
				symbol,
				x: spawn.x,
				y: spawn.y,
				vx: (Math.random() - 0.5) * 3,
				vy: (Math.random() - 0.5) * 3,
				orbitR: 8 + Math.random() * Math.max(1, (maxR - 8)),
				orbitDir: Math.random() < 0.5 ? -1 : 1
			});
		}
	} else if (diff < 0) {
		let remove = -diff;
		for (let i = dots.length - 1; i >= 0 && remove > 0; i--) {
			if (dots[i].symbol === symbol) {
				dots.splice(i, 1);
				remove--;
			}
		}
	}
};

function applyOrbitForces(d) {
	const t = targets[d.symbol];
	if (!t) return;

	const dx = d.x - t.x;
	const dy = d.y - t.y;
	const dist = Math.hypot(dx, dy) || 0.0001;

	const nx = dx / dist;
	const ny = dy / dist;

	const desiredR = Math.max(
		6,
		Math.min(d.orbitR, t.rRegion - (COLLISION_R + 2))
	);

	const ringError = dist - desiredR;

	d.vx += -nx * ringError * RING_STRENGTH;
	d.vy += -ny * ringError * RING_STRENGTH;

	const tx = -ny * d.orbitDir;
	const ty = nx * d.orbitDir;

	d.vx += tx * SWIRL_STRENGTH;
	d.vy += ty * SWIRL_STRENGTH;
}

function collideWithRegion(d) {
	const t = targets[d.symbol];
	if (!t) return;

	const boundaryR = t.rRegion - (COLLISION_R + 1);
	if (boundaryR <= 6) return;

	const dx = d.x - t.x;
	const dy = d.y - t.y;
	const dist = Math.hypot(dx, dy) || 0.0001;

	// keep dot inside boundary
	if (dist > boundaryR) {
		const nx = dx / dist;
		const ny = dy / dist;

		d.x = t.x + nx * boundaryR;
		d.y = t.y + ny * boundaryR;

		// remove outward velocity component
		const vn = d.vx * nx + d.vy * ny;
		if (vn > 0) {
			d.vx -= vn * nx;
			d.vy -= vn * ny;
		}
	}
}

function clampSpeed(d) {
	const sp = Math.hypot(d.vx, d.vy);
	if (sp > MAX_SPEED) {
		d.vx = (d.vx / sp) * MAX_SPEED;
		d.vy = (d.vy / sp) * MAX_SPEED;
	}
}

function resolveDotCollisions() {
	// O(n^2) is fine here (<= 4*100 = 400 dots worst-case, still okay)
	for (let i = 0; i < dots.length; i++) {
		for (let j = i + 1; j < dots.length; j++) {
			const a = dots[i];
			const b = dots[j];

			const dx = b.x - a.x;
			const dy = b.y - a.y;
			const dist = Math.hypot(dx, dy);

			if (dist > 0 && dist < MIN_DIST) {
				const overlap = (MIN_DIST - dist) / 2;
				const nx = dx / dist;
				const ny = dy / dist;

				a.x -= nx * overlap;
				a.y -= ny * overlap;
				b.x += nx * overlap;
				b.y += ny * overlap;
			}
		}
	}
}

function colorForSymbol(symbol) {
	if (!marketOpen) return "rgba(255,255,255,0.85)"; // calm mode

	const ch = symbolState[symbol]?.change;
	if (ch == null) return "rgba(255,255,255,0.85)";

	if (ch > 0) return "rgba(56,217,150,0.95)";  // green
	if (ch < 0) return "rgba(255,92,122,0.95)";  // red
	return "rgba(246,201,69,0.95)";              // yellow
}

function step() {
	if (!ctx) return;

	ctx.clearRect(0, 0, W, H);

	for (const d of dots) {
		applyOrbitForces(d);

		d.vx *= DAMP;
		d.vy *= DAMP;

		clampSpeed(d);

		d.x += d.vx;
		d.y += d.vy;

		collideWithRegion(d);
	}

	resolveDotCollisions();

	// draw grouped by symbol color
	const bySymbol = new Map();
	for (const d of dots) {
		if (!bySymbol.has(d.symbol)) bySymbol.set(d.symbol, []);
		bySymbol.get(d.symbol).push(d);
	}

	for (const [sym, arr] of bySymbol.entries()) {
		ctx.fillStyle = colorForSymbol(sym);
		for (const d of arr) {
			ctx.beginPath();
			ctx.arc(d.x, d.y, DOT_R, 0, Math.PI * 2);
			ctx.fill();
		}
	}

	requestAnimationFrame(step);
}

seedAmbientDots = function seedAmbientDots() {
	document.querySelectorAll(".stock-card").forEach(card => {
		const sym = card.id.replace("card-", "");
		if (!symbolState[sym]) symbolState[sym] = { volume: null, avgVolume: null, change: null };
		syncDotsForSymbol(sym, marketOpen ? targetDotsForSymbol(sym) : AMBIENT_DOTS_CLOSED);
	});
};

/* ---------------------- INIT: wait for layout, then start dots ---------------------- */
if (ctx) {
	window.addEventListener("load", () => {
		resizeCanvas();
		setTimeout(resizeCanvas, 150);

		updateMarketMode();
		setInterval(updateMarketMode, 20000);

		seedAmbientDots();

		requestAnimationFrame(step);

		setInterval(recomputeTargets, 1000);
	});
} else {
	console.warn("Dots canvas/context missing. Check #dots exists in HTML.");
}

window.addEventListener("resize", () => {
	resizeCanvas();
});

/* ---------------------- STOCK UPDATES ---------------------- */
socket.on("server_test", (data) => {
	console.log("🎯 server_test", data);
});

socket.on("tick", (data) => {
	const symbol = data.symbol;
	if (!symbol) return;

	if (!symbolState[symbol]) symbolState[symbol] = { volume: null, avgVolume: null, change: null };

	if (data.volume != null) symbolState[symbol].volume = Number(data.volume);
	if (data.avg_volume != null) symbolState[symbol].avgVolume = Number(data.avg_volume);
	if (data.change != null) symbolState[symbol].change = Number(data.change);

	const elPrice = document.getElementById(`price-${symbol}`);
	const elChange = document.getElementById(`change-${symbol}`);
	const elVolume = document.getElementById(`volume-${symbol}`);
	const elTs = document.getElementById(`ts-${symbol}`);

	if (elPrice) elPrice.textContent = data.price != null ? fmt(Number(data.price), 2) : "—";
	if (elChange) setSigned(elChange, data.change != null ? Number(data.change) : null, 2);

	// Display volume in millions
	if (elVolume) {
		if (data.volume != null) {
			const volM = Number(data.volume) / 1_000_000;
			if (data.avg_volume != null && Number(data.avg_volume) > 0) {
				const rel = Number(data.volume) / Number(data.avg_volume);
				elVolume.textContent = `${volM.toFixed(2)}M (${rel.toFixed(2)}x)`;
			} else {
				elVolume.textContent = `${volM.toFixed(2)}M`;
			}
		} else {
			elVolume.textContent = "—";
		}
	}

	if (elTs) {
		elTs.textContent = data.ts ? new Date(Number(data.ts) * 1000).toLocaleString() : "—";
	}

	if (marketOpen) {
		for (const sym of Object.keys(symbolState)) {
			syncDotsForSymbol(sym, targetDotsForSymbol(sym));
		}
	} else {
		syncDotsForSymbol(symbol, AMBIENT_DOTS_CLOSED);
	}
});
