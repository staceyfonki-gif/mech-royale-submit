"""Evasive survival bot for mechfight."""
import math
import mechfight as mf


def _wrap_angle(a: float) -> float:
    while a > math.pi:
        a -= 2.0 * math.pi
    while a < -math.pi:
        a += 2.0 * math.pi
    return a


def _score_choice(choice: dict) -> float:
    text = f"{choice.get('id', '')} {choice.get('name', '')} {choice.get('desc', '')}".lower()
    score = 0.0
    for kw, pts in (
        ("hp", 7.0),
        ("health", 7.0),
        ("shield", 6.5),
        ("armor", 6.0),
        ("regen", 6.0),
        ("repair", 6.0),
        ("speed", 5.0),
        ("move", 4.5),
        ("boost", 4.5),
        ("energy", 4.5),
        ("cooldown", 2.5),
        ("reload", 2.0),
        ("damage", 1.5),
        ("range", 1.0),
    ):
        if kw in text:
            score += pts
    return score


def _can_fire_slot(weapons: list, idx: int) -> bool:
    if idx >= len(weapons) or weapons[idx] is None:
        return False
    w = weapons[idx]
    if float(w.get("reload", 0.0)) > 0.0:
        return False
    ammo = w.get("ammo")
    if ammo is not None and float(ammo) <= 0.0:
        return False
    return True


def _dodge_projectile_yaw(x: float, y: float, projectiles: list, strafe_dir: float):
    """Return an evade yaw if a projectile is moving close to (x, y), else None."""
    best_threat = None
    best_dist = 22.0
    for p in projectiles:
        ppos = p.get("pos", [0.0, 0.0, 0.0])
        pvel = p.get("vel", [0.0, 0.0, 0.0])
        px, py = float(ppos[0]), float(ppos[1])
        vx, vy = float(pvel[0]), float(pvel[1])
        dx, dy = x - px, y - py
        dist = math.hypot(dx, dy)
        if dist < best_dist and dist > 1e-6:
            speed = math.hypot(vx, vy)
            if speed > 1.0:
                # Check if projectile is moving generally toward our position
                dot = (dx * vx + dy * vy) / (dist * speed)
                if dot > 0.55:
                    best_dist = dist
                    best_threat = math.atan2(vy, vx)
    if best_threat is not None:
        return best_threat + strafe_dir * (math.pi * 0.5)
    return None


class Bot:
    name = "kestrel-v4"

    def __init__(self, hello: mf.Hello):
        cfg = hello.get("config", {})
        self.reach = cfg.get("loot", {}).get("pickup_radius", 4.0)
        self.strafe_dir = 1.0 if int(hello.get("seed", 0)) % 2 == 0 else -1.0

    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        x, y, z = me["pos"]
        hp = float(me.get("hp", 100.0))
        max_hp = max(1.0, float(me.get("max_hp", 100.0)))
        hp_ratio = hp / max_hp
        energy = float(me.get("energy", 0.0))
        in_zone = bool(me.get("in_zone", True))

        zone = obs["zone"]
        next_cx, next_cy = zone["next_center"]
        next_r = max(1.0, float(zone.get("next_radius", 50.0)))
        dist_next = math.hypot(next_cx - x, next_cy - y)
        t = float(obs.get("t", 0.0))
        shrink_start = float(zone.get("shrink_start", 0.0))
        shrink_end = float(zone.get("shrink_end", 0.0))
        zone_dps = float(zone.get("dps", 0.0))
        shrinking = shrink_start <= t <= shrink_end

        events = obs.get("events", [])
        took_hit = any(e.get("type") == "damaged" for e in events)
        hit_bearings = [
            float(e["bearing"])
            for e in events
            if e.get("type") == "damaged" and e.get("bearing") is not None
        ]

        act: mf.Action = {"tick": obs["tick"]}

        # 1. Pick strongest survival / mobility / energy upgrade
        offer = obs.get("offer")
        if offer and offer.get("choices"):
            best_idx = max(
                range(len(offer["choices"])),
                key=lambda i: _score_choice(offer["choices"][i]),
            )
            act["pick"] = best_idx

        # 2. Grab a weapon into empty slot 1 when within pickup radius
        weapons = me.get("weapons", [])
        if len(weapons) > 1 and weapons[1] is None:
            near = [
                p for p in obs.get("pickups", [])
                if math.dist(p["pos"][:2], (x, y)) <= self.reach
            ]
            if near:
                non_gun = [p for p in near if p.get("kind") != "gun"]
                chosen = non_gun[0] if non_gun else near[0]
                act["take"] = chosen["id"]

        tick_int = int(obs["tick"])
        if took_hit or tick_int % 55 == 0:
            self.strafe_dir *= -1.0

        enemies = [m for m in obs.get("mechs", []) if m.get("kind") == "mech"]
        enemies.sort(key=lambda m: math.dist(m["pos"], me["pos"]))
        nearest_dist = math.hypot(enemies[0]["pos"][0] - x, enemies[0]["pos"][1] - y) if enemies else 999.0

        to_zone_yaw = math.atan2(next_cy - y, next_cx - x)
        urgent_zone = (not in_zone) or (shrinking and dist_next > next_r * 0.68) or (dist_next > next_r * 0.84)

        def _inside_safe_next(yaw: float, step: float = 10.0) -> bool:
            nx = x + math.cos(yaw) * step
            ny = y + math.sin(yaw) * step
            return math.hypot(next_cx - nx, next_cy - ny) < next_r * 0.84

        dodge_yaw = _dodge_projectile_yaw(x, y, obs.get("projectiles", []), self.strafe_dir)

        # 3. Movement: prioritize zone safety, incoming fire dodge, and staying off center aggro
        if urgent_zone:
            move_yaw = to_zone_yaw
            want_boost = (not in_zone or zone_dps >= 3.0) and energy > 20.0
        elif dodge_yaw is not None and _inside_safe_next(dodge_yaw, 8.0):
            move_yaw = dodge_yaw
            want_boost = energy > 25.0
        elif hp_ratio < 0.65 and enemies and nearest_dist < 38.0:
            away_yaw = math.atan2(y - enemies[0]["pos"][1], x - enemies[0]["pos"][0])
            evade_yaw = away_yaw + self.strafe_dir * 0.5
            if _inside_safe_next(evade_yaw):
                move_yaw = evade_yaw
            elif _inside_safe_next(away_yaw):
                move_yaw = away_yaw
            else:
                move_yaw = to_zone_yaw
            want_boost = energy > 24.0
        elif took_hit and hit_bearings:
            perp_yaw = hit_bearings[0] + self.strafe_dir * (math.pi * 0.5)
            move_yaw = perp_yaw if _inside_safe_next(perp_yaw) else to_zone_yaw
            want_boost = energy > 25.0
        elif dist_next > next_r * 0.65:
            move_yaw = to_zone_yaw
            want_boost = False
        else:
            # Orbit/weave gently without rushing straight into the middle
            weave_yaw = to_zone_yaw + self.strafe_dir * (math.pi * 0.55)
            move_yaw = weave_yaw if _inside_safe_next(weave_yaw, 8.0) else to_zone_yaw
            want_boost = False

        act["move"] = [math.cos(move_yaw), math.sin(move_yaw)]
        act["boost"] = want_boost
        act["jump"] = (tick_int % 28 == 0 and energy > 18.0) or (dodge_yaw is not None and energy > 20.0)

        # 4. Aiming & selective firing (avoid drawing attention when hurt)
        cur_yaw = float(me.get("aim", [0.0, 0.0])[0])
        act["aim"] = [to_zone_yaw, 0.0]

        if enemies:
            t_mech = enemies[0]
            tx, ty, tz = t_mech["pos"]
            tvx, tvy, _ = t_mech.get("vel", [0.0, 0.0, 0.0])
            horiz_dist = math.hypot(tx - x, ty - y)
            lead_t = min(0.6, horiz_dist / 90.0)
            px, py = tx + tvx * lead_t, ty + tvy * lead_t
            aim_yaw = math.atan2(py - y, px - x)
            aim_pitch = math.atan2(tz - z - 2.0, max(1.0, math.hypot(px - x, py - y)))
            act["aim"] = [aim_yaw, aim_pitch]

            yaw_err = abs(_wrap_angle(aim_yaw - cur_yaw))
            safe_to_engage = (hp_ratio >= 0.45) or (horiz_dist < 14.0)
            if safe_to_engage and horiz_dist < 68.0 and yaw_err < 0.28:
                f0 = _can_fire_slot(weapons, 0)
                f1 = _can_fire_slot(weapons, 1)
                if f0 or f1:
                    act["fire"] = [f0, f1]
                    mf.draw.line([x, y, z + 6.0], [px, py, tz + 3.5], "#ff6040")

        mf.draw.label(f"hp {hp:.0f} | zone {'IN' if in_zone else 'OUT'}")
        return act


mf.run(Bot)
