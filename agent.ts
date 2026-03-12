/**
 * OmniLink Pac-Man Agent  –  BFS Pathfinding Edition
 * ─────────────────────────────────────────────────────────────
 * Target : Browser / OmniLink Tool environment (ESM / isolated Worker)
 *
 * Core improvement over previous version:
 *   Uses real BFS on the actual maze walkable map received from the server.
 *   Manhattan-distance heuristics are gone – every decision is based on
 *   actual reachable tile distances respecting walls and tunnels.
 *
 * Strategy:
 *   FLEE  – ghost within FLEE_RADIUS tiles (BFS dist) → run toward the
 *            tile that maximises BFS distance from the ghost.
 *   CHASE – a ghost is FRIGHTENED → BFS toward it to eat it.
 *   HUNT  – eat the closest reachable pellet / power-pellet (BFS).
 *
 * Communication:
 *   GET  http://localhost:5000/data       ← game state + walkable map
 *   POST http://localhost:5000/callback   → chosen direction
 *   MQTT ws://localhost:9001  olink/commands  ← pause/resume
 */

// ── Logging flags ─────────────────────────────────────────────────────────────
const LOG_MOVE = true;
const LOG_DANGER = false;
const LOG_IDLE = false;
const LOG_MQTT = true;
const LOG_ERRORS = true;

// ── Config ────────────────────────────────────────────────────────────────────
const API_URL = "http://localhost:5000";
const POLL_DELAY_MS = 60;
const MQTT_WS_URL = "ws://localhost:9001";
const CMD_TOPIC = "olink/commands";

const FLEE_RADIUS = 6;   // BFS tiles – enter FLEE mode when ghost is this close

// ── Interfaces ────────────────────────────────────────────────────────────────
interface PythonState {
    command: "IDLE" | "ACTIVATE";
    payload: string;
    version: number;
}

interface GhostData {
    name: string;
    x: number;
    y: number;
    state: "SCATTER" | "CHASE" | "FRIGHTENED" | "EATEN";
}

interface GameState {
    type: "state";
    player: { x: number; y: number; dir: string };
    ghosts: GhostData[];
    pellets: [number, number][];
    power_pellets: [number, number][];
    walkable: string[];      // row-major, walkable[row][col] === '1' means open
    tunnel_rows: number[];      // rows that wrap horizontally
    score: number;
    lives: number;
    level: number;
    mode: string;
    game_state: string;
    pellets_left: number;
    grid_width: number;
    grid_height: number;
}

interface AgentAction {
    action: "UP" | "DOWN" | "LEFT" | "RIGHT" | "STOP";
    version: number;
    timestamp: string;
}

// ── Direction helpers ─────────────────────────────────────────────────────────
type Dir = "UP" | "DOWN" | "LEFT" | "RIGHT";

const OPPOSITE: Record<Dir, Dir> = {
    LEFT: "RIGHT", RIGHT: "LEFT", UP: "DOWN", DOWN: "UP",
};

const DIR_VEC: Record<Dir, [number, number]> = {
    LEFT: [-1, 0],
    RIGHT: [1, 0],
    UP: [0, -1],
    DOWN: [0, 1],
};

const ALL_DIRS: Dir[] = ["LEFT", "RIGHT", "UP", "DOWN"];

// ── Shared agent state ────────────────────────────────────────────────────────
let lastVersion = -1;
let totalMoves = 0;
let lastScore = 0;
let lastLives = 3;
let lastLevel = 1;
let committedDir: Dir | null = null;
let commitLeft = 0;
const COMMIT_FRAMES = 5;

// ── Maze helpers ──────────────────────────────────────────────────────────────

/** Return true if tile (x,y) is walkable, honoring tunnel wrap. */
function isWalkable(x: number, y: number, state: GameState): boolean {
    const { grid_width: W, grid_height: H, walkable, tunnel_rows } = state;
    if (y < 0 || y >= H) return false;
    // Horizontal wrap for tunnel rows
    if (x < 0 || x >= W) {
        if (!tunnel_rows.includes(y)) return false;
        x = ((x % W) + W) % W;
    }
    return walkable[y]?.[x] === "1";
}

/** Wrap an x-coordinate in a tunnel row, or return it unchanged. */
function wrapX(x: number, y: number, state: GameState): number {
    const { grid_width: W, tunnel_rows } = state;
    if (tunnel_rows.includes(y)) return ((x % W) + W) % W;
    return x;
}

/**
 * BFS from (sx, sy).
 * Returns a Map<"x,y", distance> for all reachable tiles.
 * Optionally stops early once `stopAt` set is fully found.
 */
function bfsDistances(
    sx: number, sy: number,
    state: GameState,
    stopAt?: Set<string>
): Map<string, number> {
    const dist = new Map<string, number>();
    const queue: [number, number][] = [[sx, sy]];
    dist.set(`${sx},${sy}`, 0);
    let found = 0;
    const needed = stopAt ? stopAt.size : Infinity;

    while (queue.length > 0) {
        if (found >= needed) break;
        const [x, y] = queue.shift()!;
        const d = dist.get(`${x},${y}`)!;

        for (const dir of ALL_DIRS) {
            const [dvx, dvy] = DIR_VEC[dir];
            let nx = x + dvx, ny = y + dvy;
            nx = wrapX(nx, ny, state);
            if (!isWalkable(nx, ny, state)) continue;
            const key = `${nx},${ny}`;
            if (dist.has(key)) continue;
            dist.set(key, d + 1);
            if (stopAt?.has(key)) found++;
            queue.push([nx, ny]);
        }
    }
    return dist;
}

/**
 * Return the first-step direction toward `goal` from `start` using BFS.
 * Returns null if goal is unreachable.
 */
function bfsFirstStep(
    sx: number, sy: number,
    gx: number, gy: number,
    state: GameState,
    forbidReverse?: Dir
): Dir | null {
    if (sx === gx && sy === gy) return null;

    const prev = new Map<string, [number, number] | null>();
    const dirOf = new Map<string, Dir>();
    const queue: [number, number][] = [[sx, sy]];
    const startKey = `${sx},${sy}`;
    const goalKey = `${gx},${gy}`;
    prev.set(startKey, null);

    while (queue.length > 0) {
        const [x, y] = queue.shift()!;
        if (`${x},${y}` === goalKey) break;

        for (const dir of ALL_DIRS) {
            const [dvx, dvy] = DIR_VEC[dir];
            let nx = x + dvx, ny = y + dvy;
            nx = wrapX(nx, ny, state);
            if (!isWalkable(nx, ny, state)) continue;
            const key = `${nx},${ny}`;
            if (prev.has(key)) continue;
            // Don't allow going backwards from start
            if (`${x},${y}` === startKey && forbidReverse && dir === forbidReverse) continue;
            prev.set(key, [x, y]);
            dirOf.set(key, dir);
            queue.push([nx, ny]);
        }
    }

    if (!prev.has(goalKey)) return null;

    // Trace back to find the first step from start
    let cur = goalKey;
    while (true) {
        const p = prev.get(cur)!;
        if (p === null) return null; // shouldn't happen
        const pk = `${p[0]},${p[1]}`;
        if (pk === startKey) return dirOf.get(cur)!;
        cur = pk;
    }
}

// ── Main decision function ────────────────────────────────────────────────────
function chooseMove(state: GameState): Dir {
    const px = Math.floor(state.player.x);
    const py = Math.floor(state.player.y);
    const currentDir = state.player.dir as Dir;
    const forbidReverse = OPPOSITE[currentDir];

    // ── Direction commitment: keep going straight unless forced to reconsider ─
    if (committedDir !== null && commitLeft > 0) {
        const [vx, vy] = DIR_VEC[committedDir];
        const nx = wrapX(px + vx, py + vy, state);
        const ny = py + vy;
        if (isWalkable(nx, ny, state)) {
            commitLeft--;
            return committedDir;
        }
        // Wall ahead – reconsider now
        commitLeft = 0;
        committedDir = null;
    }

    // ── Compute BFS distances from player ─────────────────────────────────────
    const fromPlayer = bfsDistances(px, py, state);

    // ── Check ghost threats ───────────────────────────────────────────────────
    const activeGhosts = state.ghosts.filter(g => g.state !== "FRIGHTENED" && g.state !== "EATEN");
    const frightenedGhosts = state.ghosts.filter(g => g.state === "FRIGHTENED");

    let closestThreatDist = Infinity;
    let closestThreat: GhostData | null = null;
    for (const g of activeGhosts) {
        const d = fromPlayer.get(`${Math.floor(g.x)},${Math.floor(g.y)}`) ?? Infinity;
        if (d < closestThreatDist) { closestThreatDist = d; closestThreat = g; }
    }

    let mode: "FLEE" | "CHASE" | "HUNT" = "HUNT";
    if (closestThreatDist <= FLEE_RADIUS) mode = "FLEE";
    else if (frightenedGhosts.length > 0) mode = "CHASE";

    // ── FLEE: pick direction that maximises min-ghost BFS distance ────────────
    if (mode === "FLEE") {
        if (LOG_MOVE) console.log(`[AI] 🚨 FLEE – ghost ${closestThreat?.name} dist=${closestThreatDist}`);

        let bestDir: Dir | null = null;
        let bestScore = -Infinity;

        for (const dir of ALL_DIRS) {
            if (dir === forbidReverse) continue;  // avoid reversing unless forced
            const [vx, vy] = DIR_VEC[dir];
            const nx = wrapX(px + vx, py + vy, state);
            const ny = py + vy;
            if (!isWalkable(nx, ny, state)) continue;

            // BFS from this next tile to evaluate safety
            const fromNext = bfsDistances(nx, ny, state);
            let minGhostDist = Infinity;
            for (const g of activeGhosts) {
                const d = fromNext.get(`${Math.floor(g.x)},${Math.floor(g.y)}`) ?? Infinity;
                if (d < minGhostDist) minGhostDist = d;
            }

            // Tiebreak: prefer directions that also have pellets nearby
            const pelletBonus = state.pellets.some(([tx, ty]) =>
                (fromNext.get(`${tx},${ty}`) ?? Infinity) <= 3) ? 0.5 : 0;

            const score = minGhostDist + pelletBonus;
            if (LOG_DANGER) console.log(`  [FLEE] ${dir} → ghostDist=${minGhostDist.toFixed(0)} bonus=${pelletBonus}`);

            if (score > bestScore) { bestScore = score; bestDir = dir; }
        }

        // If all non-reverse dirs are walls, allow reverse
        if (bestDir === null) {
            for (const dir of ALL_DIRS) {
                const [vx, vy] = DIR_VEC[dir];
                if (isWalkable(wrapX(px + vx, py + vy, state), py + vy, state)) {
                    bestDir = dir;
                    break;
                }
            }
        }

        const chosen = bestDir ?? currentDir;
        committedDir = chosen; commitLeft = 2;  // short commitment during flee
        return chosen;
    }

    // ── CHASE: BFS toward nearest frightened ghost ────────────────────────────
    if (mode === "CHASE") {
        let bestDir: Dir | null = null;
        let minDist = Infinity;

        for (const g of frightenedGhosts) {
            const gx = Math.floor(g.x), gy = Math.floor(g.y);
            const d = fromPlayer.get(`${gx},${gy}`) ?? Infinity;
            if (d < minDist) {
                const dir = bfsFirstStep(px, py, gx, gy, state, forbidReverse);
                if (dir) { minDist = d; bestDir = dir; }
            }
        }

        if (bestDir) {
            if (LOG_MOVE) console.log(`[AI] 👻 CHASE frightened ghost → ${bestDir}  dist=${minDist}`);
            committedDir = bestDir; commitLeft = COMMIT_FRAMES;
            return bestDir;
        }
        // Fall through to HUNT if BFS failed
    }

    // ── HUNT: BFS toward closest reachable pellet ─────────────────────────────
    // Prefer power pellets if a ghost is even remotely near (within 12 tiles)
    const wantPower = closestThreatDist <= 12;
    const targetList: [number, number][] = wantPower
        ? [...state.power_pellets, ...state.pellets]
        : [...state.pellets, ...state.power_pellets];

    let bestHuntDir: Dir | null = null;
    let bestHuntDist = Infinity;

    for (const [tx, ty] of targetList) {
        const d = fromPlayer.get(`${tx},${ty}`) ?? Infinity;
        if (d < bestHuntDist) {
            const dir = bfsFirstStep(px, py, tx, ty, state, forbidReverse);
            if (dir) { bestHuntDist = d; bestHuntDir = dir; }
        }
    }

    // If all pellets are blocked by reverse only, try with reverse allowed
    if (!bestHuntDir) {
        for (const [tx, ty] of targetList) {
            const dir = bfsFirstStep(px, py, tx, ty, state);
            if (dir) { bestHuntDir = dir; break; }
        }
    }

    if (bestHuntDir) {
        if (LOG_MOVE) console.log(`[AI] 🔵 HUNT pellet @ dist=${bestHuntDist} → ${bestHuntDir}  (pellets_left=${state.pellets_left})`);
        committedDir = bestHuntDir; commitLeft = COMMIT_FRAMES;
        return bestHuntDir;
    }

    // Absolute fallback: keep going, or try any walkable direction
    if (LOG_MOVE) console.log("[AI] ⚠️  No target found – continuing current direction");
    for (const dir of ALL_DIRS) {
        const [vx, vy] = DIR_VEC[dir];
        if (isWalkable(wrapX(px + vx, py + vy, state), py + vy, state)) return dir;
    }
    return currentDir;
}

// ── Main agent loop ───────────────────────────────────────────────────────────
async function agentLoop(): Promise<void> {
    try {
        const res = await fetch(`${API_URL}/data`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const wrapper: PythonState = await res.json();

        if (wrapper.command === "ACTIVATE" && wrapper.version > lastVersion) {
            lastVersion = wrapper.version;
            const state: GameState = JSON.parse(wrapper.payload);

            // ── Game event logging ─────────────────────────────────────────────
            if (state.score !== lastScore) {
                console.log(`[GAME] 🔶 Score: ${lastScore} → ${state.score} (+${state.score - lastScore})`);
                lastScore = state.score;
            }
            if (state.lives !== lastLives) {
                console.log(`[GAME] 💔 Lives: ${lastLives} → ${state.lives}`);
                lastLives = state.lives;
                // Reset commitment on death
                committedDir = null; commitLeft = 0;
            }
            if (state.level !== lastLevel) {
                console.log(`[GAME] 🎉 Level up! ${lastLevel} → ${state.level}`);
                lastLevel = state.level;
                committedDir = null; commitLeft = 0;
            }

            // ── No walkable map yet (first poll before level starts) ───────────
            if (!state.walkable || state.walkable.length === 0) return;

            const move = chooseMove(state);
            totalMoves++;

            const action: AgentAction = {
                action: move,
                version: wrapper.version,
                timestamp: new Date().toISOString(),
            };

            await fetch(`${API_URL}/callback`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(action),
            });

        } else if (wrapper.command === "IDLE") {
            if (LOG_IDLE) console.log(`[AGENT] IDLE (v=${wrapper.version})`);
        }

    } catch (err: unknown) {
        if (LOG_ERRORS) {
            const msg = err instanceof Error ? `${err.name}: ${err.message}` : String(err);
            console.error(`[AGENT] Error: ${msg}`);
        }
    }
}

// ── MQTT Pause/Resume (globalThis-safe for Workers) ──────────────────────────
const _g = globalThis as Record<string, unknown>;

function sendMqttCommand(cmd: "pause" | "resume"): void {
    const client = _g["mqttClient"] as any;
    if (!client) { console.warn("[MQTT] Client not ready."); return; }
    const payload = JSON.stringify({ command: cmd });
    client.publish(CMD_TOPIC, payload);
    if (LOG_MQTT) console.log(`[MQTT] → '${CMD_TOPIC}': ${payload}`);
}

_g["pauseGame"] = () => sendMqttCommand("pause");
_g["resumeGame"] = () => sendMqttCommand("resume");

async function initMqtt(): Promise<void> {
    try {
        const mqttLib = _g["mqtt"] as any;
        if (!mqttLib) {
            console.warn("[MQTT] No global mqtt lib – pause/resume unavailable.");
            return;
        }
        const client = mqttLib.connect(MQTT_WS_URL, { clientId: `pacman-bfs-${Date.now()}` });
        client.on("connect", () => {
            if (LOG_MQTT) console.log(`[MQTT] ✅ Connected to ${MQTT_WS_URL}`);
            _g["mqttClient"] = client;
        });
        client.on("error", (e: Error) => { if (LOG_ERRORS) console.error("[MQTT]", e.message); });
        client.on("close", () => { if (LOG_MQTT) console.log("[MQTT] Disconnected."); });
    } catch (err) {
        if (LOG_ERRORS) console.error("[MQTT] Init error:", err);
    }
}

// ── Bootstrap ─────────────────────────────────────────────────────────────────
console.log("╔═══════════════════════════════════════════════╗");
console.log("║  🎮  OmniLink Pac-Man Agent  –  BFS Edition   ║");
console.log("╚═══════════════════════════════════════════════╝");
console.log(`[CONFIG] API      : ${API_URL}  (poll every ${POLL_DELAY_MS}ms)`);
console.log(`[CONFIG] MQTT     : ${MQTT_WS_URL}  topic='${CMD_TOPIC}'`);
console.log(`[CONFIG] Flee at  : ${FLEE_RADIUS} BFS tiles from ghost`);
console.log("[INFO]   globalThis.pauseGame() / resumeGame() available");

initMqtt();

async function runLoop(): Promise<void> {
    await agentLoop();
    setTimeout(runLoop, POLL_DELAY_MS);
}

runLoop();
