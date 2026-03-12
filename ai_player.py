"""
AI autoplayer for pacman.py.

Usage (minimal integration):
    from ai_player import AutoPlayer
    self.ai = AutoPlayer()
    # in Game.update(), before player.update():
    # self.player.next_dir = self.ai.choose_next_dir(self)
"""

from collections import deque

DIRS = {
    "LEFT": (-1, 0),
    "RIGHT": (1, 0),
    "UP": (0, -1),
    "DOWN": (0, 1),
}
OPPOSITE = {"LEFT": "RIGHT", "RIGHT": "LEFT", "UP": "DOWN", "DOWN": "UP"}


class AutoPlayer:
    def __init__(self):
        self._last_dir = "LEFT"

    def choose_next_dir(self, game):
        maze = game.maze
        player = game.player
        ghosts = game.ghosts

        px, py = int(player.x), int(player.y)
        start = (px, py)

        # Build a danger map: avoid tiles close to active ghosts.
        danger = self._danger_tiles(maze, ghosts)

        # If any ghost is frightened, chase nearest frightened ghost.
        target = self._nearest_frightened(maze, start, ghosts)
        if target is None:
            # Otherwise, go to nearest pellet or power pellet.
            target = self._nearest_pellet(maze, start, danger)

        # If no target found, keep current direction.
        if target is None:
            return player.next_dir

        next_dir = self._bfs_first_step(maze, start, target, danger, forbid=OPPOSITE[player.dir])
        if next_dir is None:
            return player.next_dir

        self._last_dir = next_dir
        return next_dir

    def _danger_tiles(self, maze, ghosts):
        danger = set()
        for g in ghosts:
            if g.state == "FRIGHTENED" or g.state == "EATEN":
                continue
            gx, gy = int(g.x), int(g.y)
            # Mark a small radius around ghosts as unsafe.
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    x, y = gx + dx, gy + dy
                    if maze.in_bounds(x, y):
                        danger.add((x, y))
        return danger

    def _nearest_frightened(self, maze, start, ghosts):
        targets = []
        for g in ghosts:
            if g.state == "FRIGHTENED":
                targets.append((int(g.x), int(g.y)))
        if not targets:
            return None
        return self._bfs_nearest_target(maze, start, set(targets))

    def _nearest_pellet(self, maze, start, danger):
        # BFS to find the closest pellet while avoiding danger.
        q = deque([start])
        seen = {start}
        while q:
            x, y = q.popleft()
            if (x, y) not in danger:
                c = maze.at(x, y)
                if c in (".", "o"):
                    return (x, y)
            for nx, ny, _ in maze.neighbors(x, y):
                if (nx, ny) in seen:
                    continue
                if (nx, ny) in danger:
                    continue
                seen.add((nx, ny))
                q.append((nx, ny))
        return None

    def _bfs_nearest_target(self, maze, start, targets):
        q = deque([start])
        seen = {start}
        while q:
            x, y = q.popleft()
            if (x, y) in targets:
                return (x, y)
            for nx, ny, _ in maze.neighbors(x, y):
                if (nx, ny) in seen:
                    continue
                seen.add((nx, ny))
                q.append((nx, ny))
        return None

    def _bfs_first_step(self, maze, start, goal, danger, forbid=None):
        if start == goal:
            return None
        q = deque([start])
        prev = {start: None}
        prev_dir = {start: None}
        while q:
            x, y = q.popleft()
            if (x, y) == goal:
                break
            for nx, ny, dname in maze.neighbors(x, y):
                if (nx, ny) in prev:
                    continue
                if (nx, ny) in danger and (nx, ny) != goal:
                    continue
                if (x, y) == start and forbid and dname == forbid:
                    continue
                prev[(nx, ny)] = (x, y)
                prev_dir[(nx, ny)] = dname
                q.append((nx, ny))
        if goal not in prev:
            return None
        cur = goal
        while prev[cur] is not None and prev[cur] != start:
            cur = prev[cur]
        if prev[cur] == start:
            return prev_dir[cur]
        return None
