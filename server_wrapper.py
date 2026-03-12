"""
server_wrapper.py  –  OmniLink Pac-Man backend
================================================
• HTTP REST API on port 5000  →  agent polls /data, posts to /callback
• MQTT subscriber on olink/commands  →  handles pause / resume
• MQTT publisher on olink/context    →  broadcasts game state every 20 s
"""

import sys, re, threading, time, json
import pygame
from http.server import HTTPServer, BaseHTTPRequestHandler

import paho.mqtt.client as mqtt

# ── Configuration ─────────────────────────────────────────────────────────────
HTTP_PORT      = 5000
MQTT_BROKER    = "localhost"
MQTT_PORT      = 1883          # standard MQTT port  (change to 9001 for WS)
CMD_TOPIC      = "olink/commands"
CTX_TOPIC      = "olink/context"
PUBLISH_EVERY  = 20            # seconds between MQTT context publishes

# ── Shared state ──────────────────────────────────────────────────────────────
_GAME_INSTANCE = None
_SERVER_VERSION = 0
_LAST_COMMAND   = "STOP"

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def _build_game_state(game) -> dict:
    """Build a full game-state snapshot dictionary."""
    pellets, power_pellets = [], []
    for py in range(game.maze.h):
        for px in range(game.maze.w):
            c = game.maze.raw_lines[py][px]
            if   c == '.': pellets.append((px, py))
            elif c == 'o': power_pellets.append((px, py))

    # Compact walkable map: row-major flat string, '1' = can walk, '0' = wall
    # Lets the TS agent do real BFS without guessing about walls.
    walkable_rows = []
    for py in range(game.maze.h):
        row = ""
        for px in range(game.maze.w):
            row += "1" if game.maze.raw_lines[py][px] != "#" else "0"
        walkable_rows.append(row)
    walkable = walkable_rows  # list of strings, one per row

    return {
        "type":          "state",
        "player":        {"x": game.player.x, "y": game.player.y,
                          "dir": game.player.dir},
        "ghosts":        [{"name": g.name, "x": g.x, "y": g.y,
                           "state": g.state} for g in game.ghosts],
        "pellets":       pellets,
        "power_pellets": power_pellets,
        "walkable":      walkable,          # NEW – list of strings, index [row][col]
        "tunnel_rows":   sorted(game.maze.tunnel_rows),  # NEW – rows that wrap
        "score":         game.score,
        "lives":         game.lives,
        "level":         game.level,
        "mode":          game.mode,
        "game_state":    game.state,
        "pellets_left":  game.maze.pellets_total,
        "grid_width":    game.maze.w,
        "grid_height":   game.maze.h,
    }


def _apply_pause_resume(cmd: str):
    """Parse a free-form pause/resume command and act on the game."""
    game = _GAME_INSTANCE
    if game is None:
        print("[MQTT] Command received but game not ready yet.")
        return

    cmd_lower = cmd.strip().lower().strip('"\'')   # strip surrounding quotes too
    if cmd_lower in ("pause", "pause_game"):
        if game.state == "PLAY":
            game.toggle_pause()
            print(f"[MQTT] ⏸  Game PAUSED  (cmd='{cmd}')")
        else:
            print(f"[MQTT] Pause requested but game state is '{game.state}' – ignored.")
    elif cmd_lower in ("resume", "resume_game"):
        if game.state == "PAUSE":
            game.toggle_pause()
            print(f"[MQTT] ▶  Game RESUMED  (cmd='{cmd}')")
        else:
            print(f"[MQTT] Resume requested but game state is '{game.state}' – ignored.")
    else:
        print(f"[MQTT] Unknown command string: '{cmd}'")


def _parse_mqtt_command(raw: str):
    """
    Extract a command from a raw MQTT payload.
    Accepts:
      • plain text:            pause  / resume
      • JSON with 'command':   {"command": "pause"}  or  {"command":"resume"}
      • JSON with quotes:      {"command": pause}  (invalid JSON – we handle it)
    """
    raw = raw.strip()

    # ── Try valid JSON first ──────────────────────────────────────────────────
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            for key in ("command", "action", "cmd"):
                if key in data:
                    return str(data[key])
            # No recognised key
            print(f"[MQTT-PARSE] JSON has no 'command' key: {data}")
            return None
        if isinstance(data, str):
            return data
    except json.JSONDecodeError:
        pass    # Fall through to regex approach

    # ── Regex fallback for unquoted values: {"command": pause} ───────────────
    m = re.search(r'["\']?(?:command|action|cmd)["\']?\s*:\s*["\']?(\w+)["\']?', raw, re.I)
    if m:
        extracted = m.group(1)
        print(f"[MQTT-PARSE] Regex extracted command='{extracted}' from: {raw}")
        return extracted

    # ── Last resort: plain text ───────────────────────────────────────────────
    if raw.lower() in ("pause", "resume", "pause_game", "resume_game"):
        return raw

    print(f"[MQTT-PARSE] Could not parse command from: '{raw}'")
    return None


# ──────────────────────────────────────────────────────────────────────────────
# MQTT Client
# ──────────────────────────────────────────────────────────────────────────────
def _on_connect(client, userdata, flags, rc, props=None):
    if rc == 0:
        print(f"[MQTT] Connected to broker at {MQTT_BROKER}:{MQTT_PORT}")
        client.subscribe(CMD_TOPIC)
        print(f"[MQTT] Subscribed to '{CMD_TOPIC}'")
    else:
        print(f"[MQTT] Connection failed: rc={rc}")


def _on_message(client, userdata, msg):
    raw = msg.payload.decode("utf-8", errors="replace")
    print(f"[MQTT] ← Received on '{msg.topic}': {raw}")
    cmd = _parse_mqtt_command(raw)
    if cmd:
        _apply_pause_resume(cmd)


def _mqtt_publisher_loop(client):
    """Runs in its own daemon thread; publishes game state every PUBLISH_EVERY s."""
    last_publish = time.time()
    while True:
        time.sleep(1)
        now = time.time()
        if now - last_publish >= PUBLISH_EVERY and _GAME_INSTANCE is not None:
            last_publish = now
            game = _GAME_INSTANCE
            payload = {
                "topic":   "game_summary",
                "score":   game.score,
                "lives":   game.lives,
                "level":   game.level,
                "state":   game.state,
                "mode":    game.mode,
                "pellets_left": game.maze.pellets_total,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            client.publish(CTX_TOPIC, json.dumps(payload))
            print(f"[MQTT] → Published to '{CTX_TOPIC}': score={game.score} "
                  f"lives={game.lives} level={game.level} state={game.state}")


def start_mqtt():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = _on_connect
    client.on_message = _on_message
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        client.loop_start()
        print(f"[MQTT] Client started (broker={MQTT_BROKER}:{MQTT_PORT})")
    except Exception as e:
        print(f"[MQTT] WARNING: Could not connect to broker – {e}  (game will still run)")
        return

    pub_thread = threading.Thread(target=_mqtt_publisher_loop, args=(client,),
                                  daemon=True, name="mqtt-publisher")
    pub_thread.start()


# ──────────────────────────────────────────────────────────────────────────────
# HTTP API Server
# ──────────────────────────────────────────────────────────────────────────────
class PacmanAPIHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass   # silence per-request noise; important events logged manually

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        global _SERVER_VERSION
        if self.path != "/data":
            self.send_error(404)
            return

        if _GAME_INSTANCE is None:
            self.send_error(503, "Game not initialised yet")
            return

        _SERVER_VERSION += 1
        game   = _GAME_INSTANCE
        active = game.state in ("SCATTER", "CHASE", "FRIGHTENED", "PLAY")

        response_payload = {
            "command": "ACTIVATE" if game.state == "PLAY" else "IDLE",
            "payload": json.dumps(_build_game_state(game)),
            "version": _SERVER_VERSION,
        }

        data = json.dumps(response_payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        global _LAST_COMMAND
        if self.path != "/callback":
            self.send_error(404)
            return

        length   = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length).decode("utf-8", errors="replace")

        try:
            data   = json.loads(raw_body)
            action = data.get("action", "STOP").upper()
            if action in ("UP", "DOWN", "LEFT", "RIGHT") and _GAME_INSTANCE:
                _LAST_COMMAND = action
                _GAME_INSTANCE.player.next_dir = action
                # Uncomment for per-move debug:
                # print(f"[HTTP] Player next_dir = {action}")
        except Exception as e:
            print(f"[HTTP] /callback parse error: {e}  body={raw_body!r}")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')


def run_http(port=HTTP_PORT):
    server = HTTPServer(("", port), PacmanAPIHandler)
    print(f"[HTTP] API server running on port {port}")
    server.serve_forever()


# ──────────────────────────────────────────────────────────────────────────────
# Entry Point
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from pacman import Game, AI_ENABLED   # noqa: E402

    # 1. HTTP server
    http_thread = threading.Thread(target=run_http, daemon=True, name="http-api")
    http_thread.start()

    # 2. MQTT client + publisher
    start_mqtt()

    # 3. Pygame game
    print("[Game] Initialising Pac-Man…")
    game = Game()
    _GAME_INSTANCE = game
    game.ai = None   # disable built-in AI – the TS agent is in charge

    print("[Game] Ready. Waiting for agent commands…")
    try:
        game.run()
    except SystemExit:
        pass
    except Exception as exc:
        print(f"[Game] Crashed: {exc}")
    finally:
        print("[Game] Exiting.")
        pygame.quit()
        sys.exit(0)
