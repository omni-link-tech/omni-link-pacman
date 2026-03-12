/**
 * OmniLink Pac-Man Agent  –  PERFECT Edition v4 (Lookahead + Trajectory)
 * ─────────────────────────────────────────────────────────────────────────────
 *
 * Core algorithm improvements over v3:
 *
 *  1. GHOST TRAJECTORY PREDICTION (4 steps ahead):
 *     Each ghost is simulated forward in time using BFS-optimal paths toward
 *     their chase target. The future threat map is the UNION of all ghost
 *     positions across all time-steps, weighted by recency.
 *
 *  2. CORRIDOR ESCAPE ANALYSIS:
 *     Before committing to any corridor, the agent checks "Given that a ghost
 *     will reach the corridor entrance in T steps, can I traverse the corridor
 *     and exit before T?" If not, that direction gets a "dead-end" penalty.
 *
 *  3. MULTI-STEP MINIMAX ROLLOUT (depth-3):
 *     For the top-2 candidate directions, performs a 3-step rollout where:
 *     - Agent picks the highest-scoring move each step
 *     - Ghosts advance 1 BFS tile toward Pac-Man each step
 *     - Evaluates the terminal board state
 *     The candidate with the better terminal score wins.
 *
 *  4. AGGRESSIVE POWER-PELLET SEEKIING:
 *     If ANY ghost is within POWER_SEEK_DIST, power pellets get W_POWER bonus
 *     and are placed first in the target list.
 *
 *  5. PANIC WALL:
 *     If the nearest ghost is ≤ PANIC_DIST tiles, the entire scoring is
 *     replaced by pure-flee: pick the neighbor that maximises min-ghost dist.
 *
 * Communication (identical to previous agents):
 *   GET  http://localhost:5000/data       ← game state + walkable map
 *   POST http://localhost:5000/callback   → chosen direction
 *   MQTT ws://localhost:9001  olink/commands  ← pause/resume subscribe+publish
 */

// ── Logging ───────────────────────────────────────────────────────────────────
const LOG_MOVE = true;
const LOG_IDLE = false;
const LOG_MQTT = true;
const LOG_ERRORS = true;

// ── Config ────────────────────────────────────────────────────────────────────
const API_URL = "http://localhost:5000";
const POLL_DELAY_MS = 10;
const MQTT_WS_URL = "ws://localhost:9001";
const CMD_TOPIC = "olink/commands";

// ── Danger / scoring parameters ───────────────────────────────────────────────
const PANIC_DIST = 3;   // ghost ≤ this many BFS tiles → pure flee, no pellet chasing
const FLEE_RADIUS = 10;  // ghost within this → apply exponential danger curve
const POWER_SEEK_DIST = 12;  // ghost within this → actively seek power pellets

const W_PELLET = 70;
const W_POWER = 180;   // strong: getting a power pellet when threatened is critical
const W_CHASE = 220;   // chasing frightened ghosts is very rewarding
const W_DANGER = 200;   // danger penalty (exponential)
const W_FUTURE = 40;    // penalty for tiles that become dangerous 2-4 steps ahead
const W_TRAP = 60;    // penalty for moves into dead-end corridors

const LOOKAHEAD_STEPS = 4;  // how many ghost steps to simulate ahead

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
    walkable: string[];
    tunnel_rows: number[];
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
    LEFT: [-1, 0], RIGHT: [1, 0], UP: [0, -1], DOWN: [0, 1],
};

const ALL_DIRS: Dir[] = ["LEFT", "RIGHT", "UP", "DOWN"];

// ── Agent state ───────────────────────────────────────────────────────────────
let lastVersion = -1;
let lastScore = 0;
let lastLives = 7;
let lastLevel = 1;
let isPaused = false;

// ── Maze helpers ──────────────────────────────────────────────────────────────
function isWalkable(x: number, y: number, state: GameState): boolean {
    const { grid_width: W, grid_height: H, walkable, tunnel_rows } = state;
    if (y < 0 || y >= H) return false;
    if (x < 0 || x >= W) {
        if (!tunnel_rows.includes(y)) return false;
        x = ((x % W) + W) % W;
    }
    return walkable[y]?.[x] === "1";
}

function wrapX(x: number, y: number, state: GameState): number {
    const { grid_width: W, tunnel_rows } = state;
    if (tunnel_rows.includes(y)) return ((x % W) + W) % W;
    return x;
}

// ── BFS ───────────────────────────────────────────────────────────────────────
function bfsFrom(
    sx: number, sy: number,
    state: GameState,
    maxDist = 9999
): Map<string, number> {
    const dist = new Map<string, number>();
    const queue: [number, number, number][] = [[sx, sy, 0]];
    dist.set(`${sx},${sy}`, 0);
    while (queue.length > 0) {
        const [x, y, d] = queue.shift()!;
        if (d >= maxDist) continue;
        for (const dir of ALL_DIRS) {
            const [dvx, dvy] = DIR_VEC[dir];
            const nx = wrapX(x + dvx, y + dvy, state);
            const ny = y + dvy;
            if (!isWalkable(nx, ny, state)) continue;
            const key = `${nx},${ny}`;
            if (!dist.has(key)) {
                dist.set(key, d + 1);
                queue.push([nx, ny, d + 1]);
            }
        }
    }
    return dist;
}

/** Single BFS step: return the walkable neighbor that minimises dist to goal. */
function bfsStep(
    x: number, y: number,
    targetX: number, targetY: number,
    state: GameState
): [number, number] {
    // Advance ghost one tile toward target using greedy BFS step
    const dist = bfsFrom(targetX, targetY, state, 40);
    let bestX = x, bestY = y, bestD = Infinity;
    for (const dir of ALL_DIRS) {
        const [dvx, dvy] = DIR_VEC[dir];
        const nx = wrapX(x + dvx, y + dvy, state);
        const ny = y + dvy;
        if (!isWalkable(nx, ny, state)) continue;
        const d = dist.get(`${nx},${ny}`) ?? Infinity;
        if (d < bestD) { bestD = d; bestX = nx; bestY = ny; }
    }
    return [bestX, bestY];
}

// ── Ghost trajectory prediction ───────────────────────────────────────────────
/**
 * Build a "future threat map": for each active ghost, simulate it advancing
 * LOOKAHEAD_STEPS tiles toward Pac-Man. Return a Map<"x,y", minStep> where
 * minStep is the earliest future step at which that tile is occupied by a ghost.
 * Lower minStep = more imminent danger.
 */
function buildFutureThreatMap(
    activeGhosts: GhostData[],
    playerX: number, playerY: number,
    state: GameState
): Map<string, number> {
    const future = new Map<string, number>();

    for (const ghost of activeGhosts) {
        let gx = Math.floor(ghost.x);
        let gy = Math.floor(ghost.y);

        for (let step = 1; step <= LOOKAHEAD_STEPS; step++) {
            // Ghost moves one tile toward player each step
            [gx, gy] = bfsStep(gx, gy, playerX, playerY, state);
            const key = `${gx},${gy}`;
            // Record the earliest step this tile is occupied
            const existing = future.get(key) ?? Infinity;
            if (step < existing) future.set(key, step);
        }
    }
    return future;
}

// ── Corridor escape check ─────────────────────────────────────────────────────
/**
 * Returns how many distinct exits a tile has (connectivity excluding walls).
 * If a tile has only 1 exit it is a dead-end corridor tip; 2 = passthrough;
 * 3+ = open junction. Used to penalise committing to dead-ends.
 */
function exitCount(x: number, y: number, state: GameState): number {
    let n = 0;
    for (const dir of ALL_DIRS) {
        const [dvx, dvy] = DIR_VEC[dir];
        const nx = wrapX(x + dvx, y + dvy, state);
        const ny = y + dvy;
        if (isWalkable(nx, ny, state)) n++;
    }
    return n;
}

// ── Minimax 3-step rollout ────────────────────────────────────────────────────
/**
 * Simulate 3 steps of movement starting from position (sx, sy).
 * Ghosts each advance 1 tile toward the player's simulated position per step.
 * Returns aggregate danger at final position.
 */
function rollout(
    sx: number, sy: number,
    activeGhosts: GhostData[],
    state: GameState,
    depth: number
): number {
    if (depth === 0) return 0;

    // Compute ghost BFS maps from current simulated position
    let minGhostDist = Infinity;
    const ghostPositions: [number, number][] = activeGhosts.map(g =>
        [Math.floor(g.x), Math.floor(g.y)]
    );

    for (const [gx, gy] of ghostPositions) {
        const d = bfsFrom(gx, gy, state, 20).get(`${sx},${sy}`) ?? 999;
        if (d < minGhostDist) minGhostDist = d;
    }

    // Danger at this state
    let danger = 0;
    if (minGhostDist <= PANIC_DIST) danger += 500;
    else if (minGhostDist <= FLEE_RADIUS) danger += Math.pow(FLEE_RADIUS - minGhostDist + 1, 2.5);

    // Pick best forward move for agent (greedy: maximise ghost distance)
    let bestNextScore = -Infinity;
    for (const dir of ALL_DIRS) {
        const [dvx, dvy] = DIR_VEC[dir];
        const nx = wrapX(sx + dvx, sy + dvy, state);
        const ny = sy + dvy;
        if (!isWalkable(nx, ny, state)) continue;
        // Simulate ghost advance
        const nextGhosts = activeGhosts.map((g, i) => {
            const [px, py] = ghostPositions[i];
            const [ngx, ngy] = bfsStep(px, py, nx, ny, state);
            return { ...g, x: ngx, y: ngy };
        });
        const childScore = -rollout(nx, ny, nextGhosts, state, depth - 1);
        if (childScore > bestNextScore) bestNextScore = childScore;
    }

    return danger - (bestNextScore === -Infinity ? 0 : bestNextScore);
}

// ── Core scoring for a single candidate direction ─────────────────────────────
function scoreDir(
    dir: Dir,
    px: number, py: number,
    state: GameState,
    ghostBfsMaps: Map<string, number>[],
    activeGhosts: GhostData[],
    frightenedGhosts: GhostData[],
    futureThreatMap: Map<string, number>,
    fromPlayer: Map<string, number>,
    forbidReverse: Dir
): number | null {

    const [dvx, dvy] = DIR_VEC[dir];
    const cx = wrapX(px + dvx, py + dvy, state);
    const cy = py + dvy;
    if (!isWalkable(cx, cy, state)) return null;

    const fromCandidate = bfsFrom(cx, cy, state);

    // ── 1. Immediate ghost danger ─────────────────────────────────────────────
    let dangerPenalty = 0;
    for (let i = 0; i < activeGhosts.length; i++) {
        const gx = Math.floor(activeGhosts[i].x);
        const gy = Math.floor(activeGhosts[i].y);
        const ghostToCand = ghostBfsMaps[i].get(`${cx},${cy}`) ?? 999;

        if (ghostToCand <= PANIC_DIST) {
            dangerPenalty += 800 / Math.max(0.5, ghostToCand);
        } else if (ghostToCand <= FLEE_RADIUS) {
            dangerPenalty += Math.pow(FLEE_RADIUS - ghostToCand + 1, 2.8);
        }

        // Extra penalty if ghost is heading straight at us (direction awareness)
        const ghostDirToPlayer = ghostBfsMaps[i].get(`${px},${py}`) ?? 999;
        if (ghostToCand < ghostDirToPlayer) {
            // Ghost gets CLOSER to the candidate than to current pos → converging
            dangerPenalty += 20;
        }
    }

    // ── 2. Future threat (trajectory prediction) ──────────────────────────────
    let futurePenalty = 0;
    const futureStep = futureThreatMap.get(`${cx},${cy}`);
    if (futureStep !== undefined) {
        // A ghost will be on this tile in `futureStep` steps
        futurePenalty += (LOOKAHEAD_STEPS - futureStep + 1) * 30;
    }
    // Also scan tiles reachable from candidate in future steps
    for (const [key, step] of futureThreatMap.entries()) {
        const dFromCand = fromCandidate.get(key) ?? 999;
        if (dFromCand <= step) {
            // Ghost will occupy this tile at step N, and we can reach it in dFromCand steps
            // → we might collide
            futurePenalty += Math.max(0, (step - dFromCand + 1)) * 8;
        }
    }

    // ── 3. Pellet density reward ──────────────────────────────────────────────
    const nearThreat = activeGhosts.some((_, i) => {
        const gx = Math.floor(activeGhosts[i].x);
        const gy = Math.floor(activeGhosts[i].y);
        return (fromCandidate.get(`${gx},${gy}`) ?? 999) <= POWER_SEEK_DIST;
    });

    const targetPellets: [number, number][] = nearThreat
        ? [...state.power_pellets, ...state.pellets]
        : [...state.pellets, ...state.power_pellets];

    let pelletScore = 0;
    let reachableCount = 0;
    for (const [tx, ty] of targetPellets) {
        const d = fromCandidate.get(`${tx},${ty}`);
        if (d !== undefined) {
            reachableCount++;
            pelletScore += 1 / Math.sqrt(d + 1);
        }
    }

    // ── 4. Power pellet bonus when threatened ─────────────────────────────────
    let powerBonus = 0;
    if (nearThreat) {
        for (const [tx, ty] of state.power_pellets) {
            const d = fromCandidate.get(`${tx},${ty}`);
            if (d !== undefined) powerBonus += 2 / (d + 1);
        }
    }

    // ── 5. Chase bonus (frightened ghosts) ───────────────────────────────────
    let chaseBonus = 0;
    for (const fg of frightenedGhosts) {
        const fgx = Math.floor(fg.x), fgy = Math.floor(fg.y);
        const d = fromCandidate.get(`${fgx},${fgy}`);
        if (d !== undefined) chaseBonus += 2 / (d + 1);
    }

    // ── 6. Trap / dead-end penalty ────────────────────────────────────────────
    let trapPenalty = 0;
    // Fewer reachable pellets than current position → heading into isolated pocket
    const reachableFromPlayer = state.pellets.filter(([tx, ty]) =>
        fromPlayer.has(`${tx},${ty}`)
    ).length;
    if (reachableFromPlayer > 0) {
        const ratio = reachableCount / reachableFromPlayer;
        if (ratio < 0.35) trapPenalty += (1 - ratio) * 4;
    }
    // Dead-end corridor tip (1 exit) + ghost within FLEE_RADIUS = very dangerous
    if (exitCount(cx, cy, state) === 1) {
        const closestGhostToCorridor = Math.min(
            ...activeGhosts.map((_, i) => ghostBfsMaps[i].get(`${cx},${cy}`) ?? 999)
        );
        if (closestGhostToCorridor <= FLEE_RADIUS) trapPenalty += 5;
    }

    // ── 7. Reverse avoidance ──────────────────────────────────────────────────
    const reversePenalty = (dir === forbidReverse) ? 10 : 0;

    // ── Composite score ───────────────────────────────────────────────────────
    return W_PELLET * pelletScore
        + W_POWER * powerBonus
        + W_CHASE * chaseBonus
        - W_DANGER * dangerPenalty
        - W_FUTURE * futurePenalty
        - W_TRAP * trapPenalty
        - reversePenalty;
}

// ── Main decision function ────────────────────────────────────────────────────
function chooseMove(state: GameState): Dir {
    const px = Math.floor(state.player.x);
    const py = Math.floor(state.player.y);
    const currentDir = state.player.dir as Dir;
    const forbidReverse = OPPOSITE[currentDir];

    const activeGhosts = state.ghosts.filter(g => g.state !== "FRIGHTENED" && g.state !== "EATEN");
    const frightenedGhosts = state.ghosts.filter(g => g.state === "FRIGHTENED");

    // ── BFS from each ghost ────────────────────────────────────────────────────
    const ghostBfsMaps = activeGhosts.map(g =>
        bfsFrom(Math.floor(g.x), Math.floor(g.y), state)
    );

    // ── Future trajectory map ──────────────────────────────────────────────────
    const futureThreat = buildFutureThreatMap(activeGhosts, px, py, state);

    // ── BFS from player (for trap detection) ──────────────────────────────────
    const fromPlayer = bfsFrom(px, py, state);

    // ── Check nearest ghost for PANIC mode ───────────────────────────────────
    let nearestGhostDist = Infinity;
    for (const gmap of ghostBfsMaps) {
        const d = gmap.get(`${px},${py}`) ?? Infinity;
        if (d < nearestGhostDist) nearestGhostDist = d;
    }

    // ── PANIC: ghost is critically close → pure flee ──────────────────────────
    if (nearestGhostDist <= PANIC_DIST && activeGhosts.length > 0) {
        if (LOG_MOVE) console.log(`[AI] 🚨 PANIC FLEE (ghost dist=${nearestGhostDist})`);

        let bestDir: Dir = currentDir;
        let maxDist = -1;

        for (const dir of ALL_DIRS) {
            const [dvx, dvy] = DIR_VEC[dir];
            const nx = wrapX(px + dvx, py + dvy, state);
            const ny = py + dvy;
            if (!isWalkable(nx, ny, state)) continue;

            // Pick direction that maximises minimum ghost distance from that tile
            let minGFromNext = Infinity;
            for (const gmap of ghostBfsMaps) {
                const d = gmap.get(`${nx},${ny}`) ?? Infinity;
                if (d < minGFromNext) minGFromNext = d;
            }
            if (minGFromNext > maxDist) {
                maxDist = minGFromNext;
                bestDir = dir;
            }
        }
        return bestDir;
    }

    // ── Score all valid directions ─────────────────────────────────────────────
    const results: { dir: Dir; score: number }[] = [];

    for (const dir of ALL_DIRS) {
        const s = scoreDir(
            dir, px, py, state,
            ghostBfsMaps, activeGhosts, frightenedGhosts,
            futureThreat, fromPlayer, forbidReverse,
        );
        if (s !== null) results.push({ dir, score: s });
    }

    if (results.length === 0) return currentDir;

    results.sort((a, b) => b.score - a.score);

    // ── Minimax rollout tiebreak on top-2 candidates ──────────────────────────
    const top = results.slice(0, Math.min(2, results.length));
    if (top.length > 1 && Math.abs(top[0].score - top[1].score) < 15) {
        // Close call: simulate 3 steps ahead for both and pick safer outcome
        const r0 = rollout(
            wrapX(px + DIR_VEC[top[0].dir][0], py + DIR_VEC[top[0].dir][1], state),
            py + DIR_VEC[top[0].dir][1],
            activeGhosts, state, 3
        );
        const r1 = rollout(
            wrapX(px + DIR_VEC[top[1].dir][0], py + DIR_VEC[top[1].dir][1], state),
            py + DIR_VEC[top[1].dir][1],
            activeGhosts, state, 3
        );
        // Lower rollout danger = better
        if (r1 < r0) {
            if (LOG_MOVE) console.log(`[AI] 🔍 Rollout tiebreak: ${top[1].dir} beats ${top[0].dir} (${r1.toFixed(0)} < ${r0.toFixed(0)})`);
            return top[1].dir;
        }
    }

    if (LOG_MOVE) {
        const best = results[0];
        const modeTag = frightenedGhosts.length > 0 ? "CHASE+" : nearestGhostDist <= FLEE_RADIUS ? "FLEE+" : "HUNT";
        console.log(`[AI] ${modeTag} → ${best.dir}  score=${best.score.toFixed(1)}  ghost=${nearestGhostDist}`);
    }

    return results[0].dir;
}

// ── Agent loop ────────────────────────────────────────────────────────────────
async function agentLoop(): Promise<void> {
    if (isPaused) return;

    try {
        const res = await fetch(`${API_URL}/data`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const wrapper = await res.json() as PythonState;

        if (wrapper.command === "ACTIVATE" && wrapper.version > lastVersion) {
            lastVersion = wrapper.version;
            const state: GameState = JSON.parse(wrapper.payload);

            if (state.score !== lastScore) {
                console.log(`[GAME] 🔶 Score: ${lastScore} → ${state.score} (+${state.score - lastScore})`);
                lastScore = state.score;
            }
            if (state.lives !== lastLives) {
                console.log(`[GAME] 💔 Lives: ${lastLives} → ${state.lives}`);
                lastLives = state.lives;
            }
            if (state.level !== lastLevel) {
                console.log(`[GAME] 🎉 Level ${state.level}!`);
                lastLevel = state.level;
            }

            if (!state.walkable || state.walkable.length === 0) return;

            const move = chooseMove(state);
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

// ── MQTT: pause / resume ──────────────────────────────────────────────────────
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

function handleInboundMqtt(raw: string): void {
    let cmd: string | null = null;
    try {
        const data = JSON.parse(raw);
        cmd = (typeof data === "object" && data !== null)
            ? (data["command"] ?? data["action"] ?? data["cmd"] ?? null)
            : typeof data === "string" ? data : null;
    } catch { cmd = raw.trim(); }

    if (!cmd) return;
    const lower = cmd.toString().toLowerCase().replace(/['"]/g, "").trim();

    if (lower === "pause" || lower === "pause_game") {
        isPaused = true;
        if (LOG_MQTT) console.log("[MQTT] ⏸  Agent PAUSED");
    } else if (lower === "resume" || lower === "resume_game") {
        isPaused = false;
        if (LOG_MQTT) console.log("[MQTT] ▶  Agent RESUMED");
    }
}

async function initMqtt(): Promise<void> {
    try {
        const mqttLib = _g["mqtt"] as any;
        if (!mqttLib) { console.warn("[MQTT] No global mqtt lib."); return; }
        const client = mqttLib.connect(MQTT_WS_URL, { clientId: `pacman-perfect-${Date.now()}` });
        client.on("connect", () => {
            if (LOG_MQTT) console.log(`[MQTT] ✅ Connected to ${MQTT_WS_URL}`);
            _g["mqttClient"] = client;
            client.subscribe(CMD_TOPIC);
        });
        client.on("message", (_topic: string, message: unknown) => {
            const raw = typeof message === "string" ? message : String(message);
            if (LOG_MQTT) console.log(`[MQTT] ← ${raw}`);
            handleInboundMqtt(raw);
        });
        client.on("error", (e: Error) => { if (LOG_ERRORS) console.error("[MQTT]", e.message); });
        client.on("close", () => { if (LOG_MQTT) console.log("[MQTT] Disconnected."); });
    } catch (err) {
        if (LOG_ERRORS) console.error("[MQTT] Init error:", err);
    }
}

// ── Bootstrap ─────────────────────────────────────────────────────────────────
console.log("╔═══════════════════════════════════════════════════════════╗");
console.log("║  🎮  OmniLink Perfect Agent v4  –  Lookahead + Trajectory  ║");
console.log("╚═══════════════════════════════════════════════════════════╝");
console.log(`[CONFIG] API           : ${API_URL}  (${POLL_DELAY_MS}ms poll)`);
console.log(`[CONFIG] MQTT          : ${MQTT_WS_URL}`);
console.log(`[CONFIG] PANIC_DIST    : ${PANIC_DIST} tiles`);
console.log(`[CONFIG] FLEE_RADIUS   : ${FLEE_RADIUS} tiles`);
console.log(`[CONFIG] LOOKAHEAD     : ${LOOKAHEAD_STEPS} ghost steps`);
console.log(`[CONFIG] Weights       : PELLET=${W_PELLET} POWER=${W_POWER} CHASE=${W_CHASE} DANGER=${W_DANGER}`);
console.log("[INFO]   globalThis.pauseGame() / resumeGame() available");

initMqtt();

async function runLoop(): Promise<void> {
    await agentLoop();
    setTimeout(runLoop, POLL_DELAY_MS);
}

runLoop();
