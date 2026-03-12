# pacman_clone.py
# A full Pac-Man style clone using pygame (no external assets).
# Features: pellets, power pellets, frightened mode, 4 ghosts, scatter/chase cycles,
# tunnel wrap, scoring, lives, levels, pause, title/game over, local high score.

import math
import os
import random
from collections import deque

import pygame
from ai_player import AutoPlayer

# -----------------------------
# Config
# -----------------------------
TILE = 20
FPS = 60
AI_ENABLED = False  # Set to True only when running standalone (without server_wrapper.py)

# Colors
BLACK = (0, 0, 0)
WHITE = (245, 245, 245)
BLUE = (30, 70, 255)
YELLOW = (255, 230, 0)
PINK = (255, 105, 180)
CYAN = (0, 220, 220)
ORANGE = (255, 165, 0)
RED = (255, 60, 60)
FRIGHT_BLUE = (30, 30, 200)
FRIGHT_WHITE = (245, 245, 245)

# Directions: (dx, dy)
DIRS = {
    "LEFT": (-1, 0),
    "RIGHT": (1, 0),
    "UP": (0, -1),
    "DOWN": (0, 1),
}
DIR_LIST = ["UP", "LEFT", "DOWN", "RIGHT"]
OPPOSITE = {"LEFT": "RIGHT", "RIGHT": "LEFT", "UP": "DOWN", "DOWN": "UP"}

# High score file
HISCORE_FILE = "pacman_hiscore.txt"

# -----------------------------
# Maze Layout
# -----------------------------
# Legend:
# # wall
# . pellet
# o power pellet
# ' ' empty
# P pacman start
# B blinky start
# I inky start
# N piNky start
# C clyde start
#
# This is a custom maze inspired by classic proportions.
MAZE_STR = [
    "############################",
    "#............##............#",
    "#.####.#####.##.#####.####.#",
    "#o####.#####.##.#####.####o#",
    "#.####.#####.##.#####.####.#",
    "#..........................#",
    "#.####.##.########.##.####.#",
    "#.####.##.########.##.####.#",
    "#......##....##....##......#",
    "######.##### ## #####.######",
    "     #.##### ## #####.#     ",
    "     #.##          ##.#     ",
    "     #.## ###--### ##.#     ",
    "######.## #      # ##.######",
    "      .   #      #   .      ",
    "######.## #      # ##.######",
    "     #.## ######## ##.#     ",
    "     #.##          ##.#     ",
    "     #.## ######## ##.#     ",
    "######.## ######## ##.######",
    "#............##............#",
    "#.####.#####.##.#####.####.#",
    "#o..##................##..o#",
    "###.##.##.########.##.##.###",
    "#......##....##....##......#",
    "#.##########.##.##########.#",
    "#.##########.##.##########.#",
    "#..........................#",
    "############################",
]

# Replace ghost-house door markers '-' with empty traversable (but treated as door for ghosts if you want)
# We'll treat '-' as empty.
# The spaces on lines 10-12 etc create tunnels outside bounds; we handle wrap.

# -----------------------------
# Utility helpers
# -----------------------------
def load_hiscore() -> int:
    try:
        with open(HISCORE_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip() or "0")
    except Exception:
        return 0

def save_hiscore(score: int) -> None:
    try:
        with open(HISCORE_FILE, "w", encoding="utf-8") as f:
            f.write(str(score))
    except Exception:
        pass

def clamp(v, a, b):
    return max(a, min(b, v))

def dist2(a, b):
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2

def sign(x):
    return -1 if x < 0 else (1 if x > 0 else 0)

# -----------------------------
# Maze
# -----------------------------
class Maze:
    def __init__(self, lines):
        self.raw_lines = [list(row) for row in lines]
        self.h = len(lines)
        self.w = max(len(r) for r in lines)
        # Normalize rows to same width
        for r in self.raw_lines:
            while len(r) < self.w:
                r.append(" ")
        # Convert '-' to space
        for y in range(self.h):
            for x in range(self.w):
                if self.raw_lines[y][x] == "-":
                    self.raw_lines[y][x] = " "

        self.pellets_total = 0
        self._count_pellets()

        # Find wrap tunnel rows: any row with leading/trailing spaces where path exists.
        self.tunnel_rows = set()
        for y in range(self.h):
            if self.is_walkable(0, y) or self.is_walkable(self.w - 1, y):
                self.tunnel_rows.add(y)

    def copy(self):
        return Maze(["".join(r) for r in self.raw_lines])

    def _count_pellets(self):
        self.pellets_total = 0
        for y in range(self.h):
            for x in range(self.w):
                if self.raw_lines[y][x] in (".", "o"):
                    self.pellets_total += 1

    def in_bounds(self, x, y):
        return 0 <= x < self.w and 0 <= y < self.h

    def at(self, x, y):
        if not self.in_bounds(x, y):
            return " "
        return self.raw_lines[y][x]

    def is_wall(self, x, y):
        return self.at(x, y) == "#"

    def is_walkable(self, x, y):
        c = self.at(x, y)
        return c != "#"

    def wrap(self, x, y):
        # Tunnel wrap horizontally for designated tunnel rows
        if y in self.tunnel_rows:
            if x < 0:
                return self.w - 1, y
            if x >= self.w:
                return 0, y
        return x, y

    def neighbors(self, x, y):
        res = []
        for dname, (dx, dy) in DIRS.items():
            nx, ny = x + dx, y + dy
            nx, ny = self.wrap(nx, ny)
            if self.in_bounds(nx, ny) and self.is_walkable(nx, ny):
                res.append((nx, ny, dname))
        return res

    def eat_at(self, x, y):
        c = self.at(x, y)
        if c in (".", "o"):
            self.raw_lines[y][x] = " "
            self.pellets_total -= 1
            return c
        return None

    def draw(self, screen, offset=(0, 0)):
        ox, oy = offset
        # Draw walls
        for y in range(self.h):
            for x in range(self.w):
                c = self.raw_lines[y][x]
                px = ox + x * TILE
                py = oy + y * TILE
                if c == "#":
                    pygame.draw.rect(screen, BLUE, (px, py, TILE, TILE), border_radius=4)
                elif c == ".":
                    pygame.draw.circle(screen, WHITE, (px + TILE // 2, py + TILE // 2), 2)
                elif c == "o":
                    pygame.draw.circle(screen, WHITE, (px + TILE // 2, py + TILE // 2), 5)

    def find_markers(self):
        starts = {}
        for y in range(self.h):
            for x in range(self.w):
                c = self.raw_lines[y][x]
                if c in ("P", "B", "N", "I", "C"):
                    starts[c] = (x, y)
                    self.raw_lines[y][x] = " "
        return starts

# -----------------------------
# Pathfinding (BFS on grid)
# -----------------------------
def bfs_next_step(maze: Maze, start, goal, forbid_reverse_dir=None):
    """
    Return a direction name for the first step from start to goal using BFS shortest path.
    If goal unreachable, returns None.
    forbid_reverse_dir: direction name that cannot be chosen as first step (optional).
    """
    sx, sy = start
    gx, gy = goal

    if (sx, sy) == (gx, gy):
        return None

    q = deque()
    q.append((sx, sy))
    prev = { (sx, sy): None }
    prev_dir = { (sx, sy): None }

    while q:
        x, y = q.popleft()
        if (x, y) == (gx, gy):
            break
        for nx, ny, dname in maze.neighbors(x, y):
            if (nx, ny) not in prev:
                prev[(nx, ny)] = (x, y)
                prev_dir[(nx, ny)] = dname
                q.append((nx, ny))

    if (gx, gy) not in prev:
        return None

    # Reconstruct backwards until we reach start
    cur = (gx, gy)
    while prev[cur] != (sx, sy):
        cur = prev[cur]
        if cur is None:
            return None

    first_dir = prev_dir[(gx, gy)]
    # The above gives direction into goal from its predecessor, not the first step.
    # So reconstruct properly:
    cur = (gx, gy)
    while prev[cur] is not None and prev[cur] != (sx, sy):
        cur = prev[cur]
    if prev[cur] == (sx, sy):
        first_dir = prev_dir[cur]
    else:
        # goal adjacent
        first_dir = prev_dir[(gx, gy)]

    if forbid_reverse_dir and first_dir == forbid_reverse_dir:
        # Try alternative neighbors: pick shortest among allowed first moves
        best = None
        best_len = None
        for nx, ny, dname in maze.neighbors(sx, sy):
            if dname == forbid_reverse_dir:
                continue
            # BFS from neighbor to goal length
            length = bfs_distance(maze, (nx, ny), goal)
            if length is None:
                continue
            if best_len is None or length < best_len:
                best_len = length
                best = dname
        return best

    return first_dir

def bfs_distance(maze: Maze, start, goal):
    sx, sy = start
    gx, gy = goal
    q = deque([(sx, sy, 0)])
    seen = {(sx, sy)}
    while q:
        x, y, d = q.popleft()
        if (x, y) == (gx, gy):
            return d
        for nx, ny, _ in maze.neighbors(x, y):
            if (nx, ny) not in seen:
                seen.add((nx, ny))
                q.append((nx, ny, d + 1))
    return None

# -----------------------------
# Entities
# -----------------------------
class Entity:
    def __init__(self, tile_pos, speed_tiles_per_sec=6.0):
        self.tx, self.ty = tile_pos
        self.x = self.tx + 0.5
        self.y = self.ty + 0.5
        self.dir = "LEFT"
        self.speed = speed_tiles_per_sec
        self.radius = TILE * 0.45

    def tile(self):
        return (int(self.x), int(self.y))

    def at_tile_center(self):
        return abs((self.x % 1.0) - 0.5) < 0.05 and abs((self.y % 1.0) - 0.5) < 0.05

    def set_tile_center(self):
        self.x = int(self.x) + 0.5
        self.y = int(self.y) + 0.5

    def move(self, maze: Maze, dt):
        dx, dy = DIRS[self.dir]
        step = self.speed * dt
        nx = self.x + dx * step
        ny = self.y + dy * step

        # Wrap in tunnel rows
        tix, tiy = int(nx), int(ny)
        if tiy in maze.tunnel_rows:
            if nx < 0:
                nx += maze.w
            elif nx >= maze.w:
                nx -= maze.w

        # Collision with walls: allow movement if destination is walkable
        # Check ahead by small epsilon in direction
        eps = 0.25
        probe_x = nx + dx * eps
        probe_y = ny + dy * eps
        ptx, pty = int(probe_x), int(probe_y)
        ptx, pty = maze.wrap(ptx, pty)
        if maze.in_bounds(ptx, pty) and maze.is_walkable(ptx, pty):
            self.x, self.y = nx, ny
        else:
            # Snap to center to avoid jitter
            self.set_tile_center()

class Player(Entity):
    def __init__(self, tile_pos):
        super().__init__(tile_pos, speed_tiles_per_sec=7.5)
        self.dir = "LEFT"
        self.next_dir = "LEFT"
        self.mouth_phase = 0.0

    def update(self, maze: Maze, dt):
        self.mouth_phase = (self.mouth_phase + dt * 10.0) % (math.pi * 2)

        # Direction buffering: at tile centers, try to turn into next_dir
        if self.at_tile_center():
            cx, cy = int(self.x), int(self.y)
            ndx, ndy = DIRS[self.next_dir]
            tx, ty = maze.wrap(cx + ndx, cy + ndy)
            if maze.in_bounds(tx, ty) and maze.is_walkable(tx, ty):
                self.dir = self.next_dir

            # If current direction blocked, stop (by snapping)
            dx, dy = DIRS[self.dir]
            tx2, ty2 = maze.wrap(cx + dx, cy + dy)
            if not (maze.in_bounds(tx2, ty2) and maze.is_walkable(tx2, ty2)):
                self.set_tile_center()
                return

        self.move(maze, dt)

    def draw(self, screen, offset=(0, 0)):
        ox, oy = offset
        px = ox + int(self.x * TILE)
        py = oy + int(self.y * TILE)
        r = int(self.radius)

        # Mouth angle oscillation
        mouth = 0.35 + 0.25 * (0.5 * (1 + math.sin(self.mouth_phase)))
        base_angle = {
            "RIGHT": 0.0,
            "LEFT": math.pi,
            "UP": -math.pi / 2,
            "DOWN": math.pi / 2,
        }[self.dir]

        # Draw a "pac" by drawing a circle and cutting a wedge with a polygon
        pygame.draw.circle(screen, YELLOW, (px, py), r)

        a1 = base_angle + mouth
        a2 = base_angle - mouth
        # Wedge polygon
        p1 = (px, py)
        p2 = (px + int(math.cos(a1) * r * 1.2), py + int(math.sin(a1) * r * 1.2))
        p3 = (px + int(math.cos(a2) * r * 1.2), py + int(math.sin(a2) * r * 1.2))
        pygame.draw.polygon(screen, BLACK, [p1, p2, p3])

class Ghost(Entity):
    def __init__(self, name, color, tile_pos, home_corner):
        super().__init__(tile_pos, speed_tiles_per_sec=4.0)  # base speed (Pac-Man is 7.5)
        self.name = name
        self.base_color = color
        self.color = color
        self.state = "SCATTER"  # SCATTER, CHASE, FRIGHTENED, EATEN
        self.fright_timer = 0.0
        self.eaten_return_target = tile_pos
        self.home_corner = home_corner
        self.dir = "LEFT"
        self._last_choice = None

    def set_frightened(self, duration):
        if self.state != "EATEN":
            self.state = "FRIGHTENED"
            self.fright_timer = duration
            self.color = FRIGHT_BLUE
            # reverse direction
            self.dir = OPPOSITE[self.dir]

    def set_eaten(self):
        self.state = "EATEN"
        self.color = WHITE
        self.speed = 9.0

    def set_mode(self, mode):
        if self.state in ("FRIGHTENED", "EATEN"):
            return
        self.state = mode

    def update(self, maze: Maze, dt, player, ghosts, mode, frightened_blink=False):
        # update fright timer
        if self.state == "FRIGHTENED":
            self.fright_timer -= dt
            if self.fright_timer <= 0:
                self.state = mode
                self.color = self.base_color
            else:
                if frightened_blink and self.fright_timer < 2.0:
                    # blink near end
                    self.color = FRIGHT_WHITE if int(self.fright_timer * 8) % 2 == 0 else FRIGHT_BLUE
                else:
                    self.color = FRIGHT_BLUE
                self.speed = 3.0   # EASY: slow when frightened so agent can eat them
        elif self.state == "EATEN":
            self.color = WHITE
        else:
            self.color = self.base_color
            self.speed = 4.0   # EASY: base chase/scatter speed (Pac-Man is 7.5)

        # Choose direction at tile centers
        if self.at_tile_center():
            cx, cy = int(self.x), int(self.y)

            # Determine target tile
            if self.state == "EATEN":
                target = self.eaten_return_target
                # if reached, revive
                if (cx, cy) == target:
                    self.state = mode
                    self.color = self.base_color
                    self.speed = 4.0   # revive at same reduced base speed
            elif self.state == "FRIGHTENED":
                # random target (or random direction without reversing)
                target = None
            else:
                if self.state == "SCATTER":
                    target = self.home_corner
                else:
                    target = self.compute_chase_target(player, ghosts)

            # List possible moves excluding reverse (unless forced)
            possible = []
            for nx, ny, dname in maze.neighbors(cx, cy):
                possible.append(dname)

            forbid = OPPOSITE[self.dir] if len(possible) > 1 else None

            if self.state == "FRIGHTENED":
                # pick random allowed direction
                choices = [d for d in possible if d != forbid] if forbid else possible[:]
                if not choices:
                    choices = possible[:]
                self.dir = random.choice(choices)
            else:
                # pathfind toward target, fallback to greedy if needed
                if target is None:
                    # shouldn't happen
                    pass
                else:
                    nd = bfs_next_step(maze, (cx, cy), target, forbid_reverse_dir=forbid)
                    if nd is None:
                        # Greedy fallback: minimize distance to target among allowed
                        best = None
                        bestd = None
                        for dname in possible:
                            if forbid and dname == forbid:
                                continue
                            dx, dy = DIRS[dname]
                            tx, ty = maze.wrap(cx + dx, cy + dy)
                            d2 = dist2((tx, ty), target)
                            if bestd is None or d2 < bestd:
                                bestd = d2
                                best = dname
                        if best is None:
                            best = random.choice(possible) if possible else self.dir
                        self.dir = best
                    else:
                        self.dir = nd

        self.move(maze, dt)

    def compute_chase_target(self, player: Player, ghosts):
        px, py = int(player.x), int(player.y)
        pdir = player.dir
        fdx, fdy = DIRS[pdir]

        # Classic-ish targeting differences
        if self.name == "BLINKY":
            # direct chase
            return (px, py)

        if self.name == "PINKY":
            # 4 tiles ahead of player
            tx = px + 4 * fdx
            ty = py + 4 * fdy
            return (tx, ty)

        if self.name == "INKY":
            # vector from blinky to 2-ahead point doubled
            blinky = None
            for g in ghosts:
                if g.name == "BLINKY":
                    blinky = g
                    break
            ax = px + 2 * fdx
            ay = py + 2 * fdy
            if blinky is None:
                return (ax, ay)
            bx, by = int(blinky.x), int(blinky.y)
            vx = ax - bx
            vy = ay - by
            return (bx + 2 * vx, by + 2 * vy)

        if self.name == "CLYDE":
            # chase when far, otherwise scatter corner
            cx, cy = int(self.x), int(self.y)
            if dist2((cx, cy), (px, py)) > (8 * 8):
                return (px, py)
            return self.home_corner

        return (px, py)

    def draw(self, screen, offset=(0, 0)):
        ox, oy = offset
        px = ox + int(self.x * TILE)
        py = oy + int(self.y * TILE)
        r = int(self.radius)

        # body
        pygame.draw.circle(screen, self.color, (px, py), r)

        # eyes (simple)
        eye_off = r // 3
        eye_r = max(2, r // 5)
        pygame.draw.circle(screen, WHITE, (px - eye_off, py - eye_off // 2), eye_r + 2)
        pygame.draw.circle(screen, WHITE, (px + eye_off, py - eye_off // 2), eye_r + 2)
        # pupils in dir
        dx, dy = DIRS[self.dir]
        pup_off = 2
        pygame.draw.circle(screen, BLACK, (px - eye_off + dx * pup_off, py - eye_off // 2 + dy * pup_off), eye_r)
        pygame.draw.circle(screen, BLACK, (px + eye_off + dx * pup_off, py - eye_off // 2 + dy * pup_off), eye_r)

# -----------------------------
# Game
# -----------------------------
class Game:
    def __init__(self):
        pygame.init()
        pygame.display.set_caption("Pac-Man Clone (Python / pygame)")

        self.base_maze = Maze(MAZE_STR)
        self.maze = self.base_maze.copy()

        self.starts = self.maze.find_markers()
        # If markers aren't placed in string, provide defaults
        if "P" not in self.starts:
            self.starts["P"] = (13, 22)
        if "B" not in self.starts:
            self.starts["B"] = (13, 14)
        if "N" not in self.starts:
            self.starts["N"] = (14, 14)
        if "I" not in self.starts:
            self.starts["I"] = (12, 14)
        if "C" not in self.starts:
            self.starts["C"] = (15, 14)

        self.base_w = self.maze.w * TILE
        self.base_h = self.maze.h * TILE + 80
        self.screen = pygame.display.set_mode((self.base_w, self.base_h), pygame.RESIZABLE)
        self.window_w = self.base_w
        self.window_h = self.base_h
        self.base_surface = pygame.Surface((self.base_w, self.base_h))
        self.clock = pygame.time.Clock()

        self.font = pygame.font.SysFont("consolas", 22)
        self.big = pygame.font.SysFont("consolas", 40, bold=True)

        self.hiscore = load_hiscore()

        self.ai = AutoPlayer() if AI_ENABLED else None
        self.reset_all()

    def update_window_size(self, w, h):
        self.window_w = max(200, int(w))
        self.window_h = max(200, int(h))
        self.screen = pygame.display.set_mode((self.window_w, self.window_h), pygame.RESIZABLE)

    def reset_all(self):
        self.level = 1
        self.score = 0
        self.lives = 7
        self.state = "TITLE"  # TITLE, PLAY, PAUSE, GAMEOVER
        self._reset_level(full=True)

    def _reset_level(self, full=False):
        self.maze = self.base_maze.copy()
        self.maze.find_markers()  # clear any accidental markers (none now)

        self.player = Player(self.starts["P"])
        self.player.dir = "LEFT"
        self.player.next_dir = "LEFT"

        # Ghost home corners (scatter targets)
        w, h = self.maze.w, self.maze.h
        corners = {
            "BLINKY": (w - 2, 1),
            "PINKY": (1, 1),
            "INKY": (w - 2, h - 2),
            "CLYDE": (1, h - 2),
        }

        # Level-based ghost spawning
        available_ghosts = [
            Ghost("BLINKY", RED,  self.starts["B"], corners["BLINKY"]),
            Ghost("PINKY", PINK, self.starts["N"], corners["PINKY"]),
            Ghost("INKY", CYAN,  self.starts["I"], corners["INKY"]),
            Ghost("CLYDE", ORANGE, self.starts["C"], corners["CLYDE"])
        ]
        
        # Determine number of ghosts based on level (max 4)
        num_ghosts = min(4, self.level)
        self.ghosts = available_ghosts[:num_ghosts]

        # Mode timing: scatter/chase cycles (classic-ish)
        self.mode = "SCATTER"
        self.mode_timer = 0.0
        self.mode_schedule = deque([
            ("SCATTER", 20.0),   # EASY: long scatter
            ("CHASE",    8.0),
            ("SCATTER", 20.0),
            ("CHASE",    8.0),
            ("SCATTER", 20.0),
            ("CHASE",   9999.0),
        ])
        self.current_mode_duration = self.mode_schedule[0][1]

        self.fright_chain = 0  # ghost-eat multiplier chain
        self.respawn_timer = 0.0
        self.dead = False

        # Slightly increase speed each level – player always faster than ghosts
        self.player.speed = 7.5 + 0.1 * (self.level - 1)
        for g in self.ghosts:
            # Ghosts start at 4.0 and scale very slowly; player stays comfortably faster
            g.speed = 4.0 + 0.05 * max(0, self.level - 1)

    def start_play(self):
        self.state = "PLAY"

    def toggle_pause(self):
        if self.state == "PLAY":
            self.state = "PAUSE"
        elif self.state == "PAUSE":
            self.state = "PLAY"

    def add_score(self, pts):
        self.score += pts
        if self.score > self.hiscore:
            self.hiscore = self.score
            save_hiscore(self.hiscore)

    def handle_input(self):
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                raise SystemExit
            if event.type == pygame.VIDEORESIZE:
                self.update_window_size(event.w, event.h)

            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    raise SystemExit

                if self.state in ("TITLE", "GAMEOVER"):
                    if event.key in (pygame.K_RETURN, pygame.K_SPACE):
                        if self.state == "TITLE":
                            self.reset_all()
                            self.start_play()
                        else:
                            self.reset_all()
                            self.start_play()

                if self.state in ("PLAY", "PAUSE"):
                    if event.key == pygame.K_p:
                        self.toggle_pause()

                    if event.key == pygame.K_LEFT:
                        self.player.next_dir = "LEFT"
                    elif event.key == pygame.K_RIGHT:
                        self.player.next_dir = "RIGHT"
                    elif event.key == pygame.K_UP:
                        self.player.next_dir = "UP"
                    elif event.key == pygame.K_DOWN:
                        self.player.next_dir = "DOWN"

    def update_modes(self, dt):
        # Only change modes if no ghost is frightened/eaten overriding it.
        self.mode_timer += dt
        if self.mode_timer >= self.current_mode_duration:
            self.mode_timer = 0.0
            self.mode_schedule.popleft()
            if not self.mode_schedule:
                self.mode_schedule.append(("CHASE", 9999.0))
            self.mode, self.current_mode_duration = self.mode_schedule[0]
            # Reverse all ghosts when switching modes (classic behavior)
            for g in self.ghosts:
                if g.state not in ("FRIGHTENED", "EATEN"):
                    g.dir = OPPOSITE[g.dir]

    def respawn_positions(self):
        self.player = Player(self.starts["P"])
        self.player.dir = "LEFT"
        self.player.next_dir = "LEFT"
        for g in self.ghosts:
            if g.name == "BLINKY":
                g.x, g.y = self.starts["B"][0] + 0.5, self.starts["B"][1] + 0.5
            elif g.name == "PINKY":
                g.x, g.y = self.starts["N"][0] + 0.5, self.starts["N"][1] + 0.5
            elif g.name == "INKY":
                g.x, g.y = self.starts["I"][0] + 0.5, self.starts["I"][1] + 0.5
            elif g.name == "CLYDE":
                g.x, g.y = self.starts["C"][0] + 0.5, self.starts["C"][1] + 0.5
            g.dir = "LEFT"
            g.state = self.mode
            g.color = g.base_color
            g.fright_timer = 0.0
            # Match the same level-scaled speed as _reset_level; player always faster
            g.speed = 4.0 + 0.05 * max(0, self.level - 1)
        self.fright_chain = 0

    def update(self, dt):
        if self.state != "PLAY":
            return

        if self.ai is not None:
            self.player.next_dir = self.ai.choose_next_dir(self)

        if self.dead:
            self.respawn_timer -= dt
            if self.respawn_timer <= 0:
                self.dead = False
                self.respawn_positions()
            return

        self.update_modes(dt)

        self.player.update(self.maze, dt)

        # Eat pellets
        ptx, pty = int(self.player.x), int(self.player.y)
        eaten = self.maze.eat_at(ptx, pty)
        if eaten == ".":
            self.add_score(10)
        elif eaten == "o":
            self.add_score(50)
            self.fright_chain = 0
            for g in self.ghosts:
                # Dynamic frightened duration based on level 
                fright_duration = max(3.0, 20.0 - (self.level - 1) * 2.5)
                g.set_frightened(duration=fright_duration)

        # Update ghosts
        frightened_blink = True
        for g in self.ghosts:
            g.update(self.maze, dt, self.player, self.ghosts, self.mode, frightened_blink=frightened_blink)

        # Collisions: player vs ghosts
        for g in self.ghosts:
            # circle-ish collision
            dx = (g.x - self.player.x)
            dy = (g.y - self.player.y)
            if dx * dx + dy * dy < (0.55 * 0.55):
                if g.state == "FRIGHTENED":
                    g.set_eaten()
                    # score: 200, 400, 800, 1600...
                    pts = 200 * (2 ** self.fright_chain)
                    self.fright_chain = min(self.fright_chain + 1, 3)
                    self.add_score(pts)
                elif g.state != "EATEN":
                    # player dies
                    self.lives -= 1
                    self.dead = True
                    self.respawn_timer = 1.2
                    if self.lives <= 0:
                        self.state = "GAMEOVER"
                    break

        # Level complete
        if self.maze.pellets_total <= 0:
            self.level += 1
            self._reset_level()

    def draw_hud(self):
        hud_y = self.maze.h * TILE
        pygame.draw.rect(self.base_surface, BLACK, (0, hud_y, self.base_w, 80))
        txt = self.font.render(f"SCORE  {self.score}   HIGH  {self.hiscore}   LEVEL  {self.level}", True, WHITE)
        self.base_surface.blit(txt, (20, hud_y + 10))

        # lives as small pac icons
        for i in range(self.lives):
            cx = 20 + i * 26
            cy = hud_y + 50
            pygame.draw.circle(self.base_surface, YELLOW, (cx, cy), 10)
            pygame.draw.polygon(self.base_surface, BLACK, [(cx, cy), (cx + 12, cy - 6), (cx + 12, cy + 6)])

        hint = self.font.render("Arrows: move   P: pause   Enter: start", True, (180, 180, 180))
        self.base_surface.blit(hint, (self.base_w - hint.get_width() - 20, hud_y + 46))

    def draw_center_text(self, title, subtitle=None):
        overlay = pygame.Surface((self.base_w, self.base_h), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 180))
        self.base_surface.blit(overlay, (0, 0))

        t = self.big.render(title, True, WHITE)
        self.base_surface.blit(t, ((self.base_w - t.get_width()) // 2, (self.base_h - 120) // 2))
        if subtitle:
            s = self.font.render(subtitle, True, (210, 210, 210))
            self.base_surface.blit(s, ((self.base_w - s.get_width()) // 2, (self.base_h - 120) // 2 + 60))

    def render(self):
        self.base_surface.fill(BLACK)

        # Maze
        self.maze.draw(self.base_surface, offset=(0, 0))

        # Entities
        if self.state != "TITLE":
            for g in self.ghosts:
                g.draw(self.base_surface)
            self.player.draw(self.base_surface)

        self.draw_hud()

        if self.state == "TITLE":
            self.draw_center_text("PAC-MAN (PYTHON)", "Press Enter / Space to start")
        elif self.state == "PAUSE":
            self.draw_center_text("PAUSED", "Press P to resume")
        elif self.state == "GAMEOVER":
            self.draw_center_text("GAME OVER", "Press Enter / Space to restart")

        self.screen.fill(BLACK)
        scale = min(self.window_w / self.base_w, self.window_h / self.base_h)
        scaled_w = int(self.base_w * scale)
        scaled_h = int(self.base_h * scale)
        scaled = pygame.transform.smoothscale(self.base_surface, (scaled_w, scaled_h))
        offset_x = (self.window_w - scaled_w) // 2
        offset_y = (self.window_h - scaled_h) // 2
        self.screen.blit(scaled, (offset_x, offset_y))
        pygame.display.flip()

    def run(self):
        while True:
            dt = self.clock.tick(FPS) / 1000.0
            dt = clamp(dt, 0.0, 0.05)

            self.handle_input()
            self.update(dt)
            self.render()

def main():
    g = Game()
    g.run()

if __name__ == "__main__":
    main()
