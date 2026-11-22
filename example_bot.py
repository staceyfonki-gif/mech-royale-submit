"""Targeted survival test bot (kestrel-v6) for mechfight."""
import math
import mechfight as mf


def _wrap_angle(a: float) -> float:
    while a > math.pi:
        a -= 2.0 * math.pi
    while a < -math.pi:
        a += 2.0 * math.pi
    return a


def _lerp(a: float, b: float, u: float) -> float:
    return a + (b - a) * u


def _score_choice(choice: dict) -> float:
    text = f"{choice.get('id', '')} {choice.get('name', '')} {choice.get('desc', '')}".lower()
    score = 0.0
    for kw, pts in (
        ("armor", 7.0),
        ("hp", 6.5),
        ("health", 6.5),
        ("shield", 6.0),
        ("regen", 6.0),
        ("repair", 6.0),
        ("speed", 5.0),
        ("boost", 4.5),
        ("energy", 4.5),
        ("sensors", 4.0),
        ("cooldown", 3.5),
        ("reload", 3.0),
        ("damage", 3.0),
        ("rate", 2.5),
        ("range", 2.0),
    ):
        if kw in text:
            score += pts
    return score


def _active_zone(zone: dict, t: float):
    c0x, c0y = zone.get("center", [0.0, 0.0])
    r0 = float(zone.get("radius", 560.0))
    c1x, c1y = zone.get("next_center", [c0x, c0y])
    r1 = float(zone.get("next_radius", r0))
    t0 = float(zone.get("shrink_start", 0.0))
    t1 = float(zone.get("shrink_end", 0.0))

    if t1 > t0 and t > t0:
        u = max(0.0, min(1.0, (t - t0) / (t1 - t0)))
        return _lerp(c0x, c1x, u), _lerp(c0y, c1y, u), _lerp(r0, r1, u), c1x, c1y, r1
    return float(c0x), float(c0y), r0, float(c1x), float(c1y), r1


def _dodge_projectile_yaw(x: float, y: float, projectiles: list, strafe_dir: float):
    best_threat = None
    best_dist = 20.0
    for p in projectiles:
        ppos = p.get("pos", [0.0, 0.0, 0.0])
        pvel = p.get("vel", [0.0, 0.0, 0.0])
        px, py = float(ppos[0]), float(ppos[1])
        vx, vy = float(pvel[0]), float(pvel[1])
        dx, dy = x - px, y - py
        dist = math.hypot(dx, dy)
        if 1e-6 < dist < best_dist:
            speed = math.hypot(vx, vy)
            if speed > 2.0:
                dot = (dx * vx + dy * vy) / (dist * speed)
                if dot > 0.6:
                    best_dist = dist
                    best_threat = math.atan2(vy, vx)
    if best_threat is not None:
        return best_threat + strafe_dir * (math.pi * 0.5)
    return None


def _can_fire_slot(weapons: list, idx: int) -> bool:
    if idx >= len(weapons) or weapons[idx] is None:
        return False
    w = weapons[idx]
    if float(w.get("cooldown", 0.0)) > 0.0 or float(w.get("reload", 0.0)) > 0.0:
        return False
    ammo = w.get("ammo")
    if ammo is not None and float(ammo) <= 0.0:
        return False
    return True


class Bot:
    name = "kestrel-v6"

    def __init__(self, hello: mf.Hello):
        cfg = hello.get("config", {})
        self.pickup_reach = float(cfg.get("loot", {}).get("pickup_radius", 4.0))
        seed_val = int(hello.get("seed", 0))
        self.strafe_dir = 1.0 if seed_val % 2 == 0 else -1.0
        self.evade_until_tick = -1
        self.evade_yaw = 0.0

    def act(self, obs: mf.Observation) -> mf.Action:
        tick = int(obs["tick"])
        t = float(obs.get("t", 0.0))
        me = obs["self"]
        x, y, z = [float(v) for v in me["pos"]]
        hp = float(me.get("hp", 100.0))
        max_hp = max(1.0, float(me.get("max_hp", 100.0)))
        hp_ratio = hp / max_hp
        energy = float(me.get("energy", 0.0))
        in_zone = bool(me.get("in_zone", True))

        zone = obs.get("zone", {})
        cur_cx, cur_cy, cur_r, next_cx, next_cy, next_r = _active_zone(zone, t)
        zone_dps = float(zone.get("dps", 0.0))
        shrink_end = float(zone.get("shrink_end", 0.0))
        time_to_shrink_end = max(0.0, shrink_end - t)

        dist_cur = math.hypot(cur_cx - x, cur_cy - y)
        dist_next = math.hypot(next_cx - x, next_cy - y)

        events = obs.get("events", [])
        took_hit = any(e.get("type") == "damaged" for e in events)
        hit_bearings = [
            float(e["bearing"])
            for e in events
            if e.get("type") == "damaged" and e.get("bearing") is not None
        ]

        act: mf.Action = {"tick": obs["tick"]}

        # 1. Upgrade selection
        offer = obs.get("offer")
        if offer and offer.get("choices"):
            best_idx = max(
                range(len(offer["choices"])),
                key=lambda i: _score_choice(offer["choices"][i]),
            )
            act["pick"] = best_idx

        # 2. Weapon pickup into empty slot 1 (only when within pickup radius)
        weapons = me.get("weapons", [])
        held_kinds = {w.get("kind") for w in weapons if isinstance(w, dict)}
        if len(weapons) > 1 and weapons[1] is None:
            near_pickups = [
                p for p in obs.get("pickups", [])
                if math.hypot(float(p["pos"][0]) - x, float(p["pos"][1]) - y) <= self.pickup_reach
                and p.get("kind") not in held_kinds
            ]
            if near_pickups:
                preferred = [p for p in near_pickups if p.get("kind") in ("lightning", "laser", "mortar")]
                chosen = preferred[0] if preferred else near_pickups[0]
                act["take"] = chosen["id"]

        # Reload empty gun proactively
        if weapons and isinstance(weapons[0], dict):
            w0 = weapons[0]
            if w0.get("ammo") is not None and float(w0.get("ammo", 0)) <= 0.0 and float(w0.get("reload", 0.0)) <= 0.0:
                act["reload"] = True

        # 3. Longer committed evasion (14 ticks = 0.7s)
        if tick % 64 == 0:
            self.strafe_dir *= -1.0

        to_cur_yaw = math.atan2(cur_cy - y, cur_cx - x)
        to_next_yaw = math.atan2(next_cy - y, next_cx - x)

        outside_cur = (not in_zone) or (dist_cur > cur_r * 0.92)
        approaching_next_limit = (time_to_shrink_end < 12.0 and dist_next > next_r * 0.75) or (dist_next > next_r * 0.92)
        urgent_zone = outside_cur or approaching_next_limit

        def _safe_step(yaw: float, dist_check: float = 10.0) -> bool:
            nx = x + math.cos(yaw) * dist_check
            ny = y + math.sin(yaw) * dist_check
            if math.hypot(cur_cx - nx, cur_cy - ny) > cur_r * 0.90:
                return False
            if time_to_shrink_end < 10.0 and math.hypot(next_cx - nx, next_cy - ny) > next_r * 0.85:
                return False
            return True

        if took_hit and hit_bearings:
            cand = hit_bearings[0] + self.strafe_dir * (math.pi * 0.52)
            self.evade_yaw = cand if _safe_step(cand) else (to_next_yaw if urgent_zone else to_cur_yaw)
            self.evade_until_tick = tick + 14
            self.strafe_dir *= -1.0

        dodge_yaw = _dodge_projectile_yaw(x, y, obs.get("projectiles", []), self.strafe_dir)

        all_mechs = obs.get("mechs", [])
        player_mechs = [m for m in all_mechs if m.get("kind") == "mech"]
        player_mechs.sort(key=lambda m: math.hypot(float(m["pos"][0]) - x, float(m["pos"][1]) - y))
        nearest_enemy = player_mechs[0] if player_mechs else None
        nearest_dist = (
            math.hypot(float(nearest_enemy["pos"][0]) - x, float(nearest_enemy["pos"][1]) - y)
            if nearest_enemy is not None
            else 999.0
        )

        # 4. Movement decision (no long-range detours for cubes)
        want_boost = False
        if urgent_zone:
            move_yaw = to_next_yaw if (time_to_shrink_end < 18.0 or dist_next < dist_cur) else to_cur_yaw
            want_boost = ((not in_zone) or zone_dps >= 35.0) and energy > 22.0
        elif tick <= self.evade_until_tick:
            move_yaw = self.evade_yaw
            want_boost = hp_ratio < 0.75 and energy > 22.0
        elif dodge_yaw is not None and _safe_step(dodge_yaw, 8.0):
            move_yaw = dodge_yaw
            want_boost = energy > 24.0
        elif hp_ratio < 0.60 and nearest_enemy is not None and nearest_dist < 45.0:
            away_yaw = math.atan2(y - float(nearest_enemy["pos"][1]), x - float(nearest_enemy["pos"][0]))
            evade_yaw = away_yaw + self.strafe_dir * 0.45
            move_yaw = evade_yaw if _safe_step(evade_yaw) else (away_yaw if _safe_step(away_yaw) else to_next_yaw)
            want_boost = energy > 22.0
        elif dist_next > next_r * 0.68:
            move_yaw = to_next_yaw
            want_boost = False
        else:
            strafe_yaw = to_next_yaw + self.strafe_dir * (math.pi * 0.5)
            move_yaw = strafe_yaw if _safe_step(strafe_yaw, 8.0) else to_next_yaw
            want_boost = False

        act["move"] = [math.cos(move_yaw), math.sin(move_yaw)]
        act["boost"] = want_boost
        act["jump"] = (tick % 30 == 0 and energy > 20.0) or (dodge_yaw is not None and energy > 22.0)

        # 5. Aim & selective close-range combat
        cur_yaw = float(me.get("aim", [0.0, 0.0])[0])
        if nearest_enemy is not None:
            tx, ty, tz = [float(v) for v in nearest_enemy["pos"]]
            tvx, tvy, _ = [float(v) for v in nearest_enemy.get("vel", [0.0, 0.0, 0.0])]
            horiz_dist = math.hypot(tx - x, ty - y)
            lead_t = min(0.5, horiz_dist / 120.0)
            px, py = tx + tvx * lead_t, ty + tvy * lead_t
            aim_yaw = math.atan2(py - y, px - x)
            aim_pitch = math.atan2(tz - z - 1.5, max(1.0, math.hypot(px - x, py - y)))
            act["aim"] = [aim_yaw, aim_pitch]

            yaw_err = abs(_wrap_angle(aim_yaw - cur_yaw))
            can_trade = (hp_ratio >= 0.60 and horiz_dist < 45.0) or (horiz_dist < 16.0 and hp_ratio >= 0.30)
            if can_trade and yaw_err < 0.24:
                f0 = _can_fire_slot(weapons, 0)
                f1 = _can_fire_slot(weapons, 1)
                if f0 or f1:
                    act["fire"] = [f0, f1]
                    mf.draw.line([x, y, z + 6.0], [px, py, tz + 3.0], "#ff6040")
        else:
            scan_yaw = move_yaw if (tick % 40 < 20) else to_next_yaw
            act["aim"] = [scan_yaw, 0.0]

        mf.draw.label(f"hp {hp:.0f} | z {'IN' if in_zone else 'OUT'}")
        return act


mf.run(Bot)
